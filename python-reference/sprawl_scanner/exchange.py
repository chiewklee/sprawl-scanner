"""Anypoint Exchange source: list an org's assets, download each asset's main spec,
and normalise it into Capability records.

Auth (environment only, never sources.yaml):
  ANYPOINT_CLIENT_ID + ANYPOINT_CLIENT_SECRET   Connected App, client_credentials
                                                (scope: Exchange Viewer), or
  ANYPOINT_TOKEN                                an existing bearer token
  ANYPOINT_ORG_ID                               optional, derived from /accounts/api/me
  ANYPOINT_BASE_URL                             optional, default US control plane

Spec handling per asset: OAS (yaml/json, plain or zipped) -> one capability per
operation; RAML -> one per resource method; MCP descriptor -> one per tool;
A2A agent card -> one per skill; anything else -> the asset's own description.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from .ingest import HTTP_METHODS, SkipSource
from .models import Capability, operation_name

DEFAULT_BASE = "https://anypoint.mulesoft.com"
DEFAULT_TYPES = ["rest-api", "http-api", "mcp", "agent", "agent-network"]
# Prefer the richest spec when an asset publishes several. Exchange labels MCP and
# agent descriptors "*-metadata"; agent-network YAML carries every broker/agent card.
CLASSIFIER_ORDER = ["fat-oas", "oas", "fat-raml", "raml",
                    "mcp-metadata", "original-mcp-metadata", "fat-mcp-metadata",
                    "agent-network", "original-agent-network",
                    "agent-metadata", "original-agent-metadata"]
PACKAGING_ORDER = ["json", "yaml", "yml", "raml", "zip"]
TYPE_TO_ASSET = {"mcp": "mcp", "agent": "agent"}  # everything else is an api


# --------------------------------------------------------------------------- auth


CREDS_FILE = Path.home() / ".config" / "sprawl-scanner" / "anypoint_connected_app"


def _token(client: httpx.Client, base: str) -> str:
    if tok := os.environ.get("ANYPOINT_TOKEN"):
        return tok
    cid, secret = os.environ.get("ANYPOINT_CLIENT_ID"), os.environ.get("ANYPOINT_CLIENT_SECRET")
    if not (cid and secret) and CREDS_FILE.exists():  # user-only file: client id, then secret
        cid, secret = (CREDS_FILE.read_text().split() + ["", ""])[:2]
    if not (cid and secret):
        raise SkipSource("set ANYPOINT_CLIENT_ID + ANYPOINT_CLIENT_SECRET (or ANYPOINT_TOKEN)")
    r = client.post(f"{base}/accounts/api/v2/oauth2/token",
                    json={"grant_type": "client_credentials", "client_id": cid,
                          "client_secret": secret})
    if r.status_code != 200:
        raise RuntimeError(f"Anypoint token HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["access_token"]


def _org_id(client: httpx.Client, base: str, headers: dict) -> str:
    if org := os.environ.get("ANYPOINT_ORG_ID"):
        return org
    me = client.get(f"{base}/accounts/api/me", headers=headers)
    me.raise_for_status()
    d = me.json()
    if "user" in d:
        return d["user"]["organization"]["id"]
    return d["client"]["org_id"]  # connected-app tokens


# ---------------------------------------------------------------------- parsers


class _LenientLoader(yaml.SafeLoader):
    """RAML is YAML plus custom tags (!include etc.): keep the structure, drop the tags."""


_LenientLoader.add_multi_constructor(
    "!", lambda loader, suffix, node: None)


def parse_raml(text: str, asset: str, owner: str, source: str) -> list[Capability]:
    doc = yaml.load(text, Loader=_LenientLoader) or {}
    caps: list[Capability] = []

    def walk(node: dict, prefix: str) -> None:
        for key, child in (node or {}).items():
            if not (isinstance(key, str) and key.startswith("/")) or not isinstance(child, dict):
                continue
            path = prefix + key
            for method in HTTP_METHODS:
                if method in child:
                    op = child[method] or {}
                    desc = " ".join(str(x) for x in (op.get("displayName"), op.get("description"))
                                    if x)
                    caps.append(Capability(asset, "api", f"{method.upper()} {path}",
                                           desc or f"{method.upper()} {path}", source, owner))
            walk(child, path)

    walk(doc, "")
    return caps


def parse_openapi(spec: dict, asset: str, owner: str, source: str) -> list[Capability]:
    caps = []
    for route, item in (spec.get("paths") or {}).items():
        for method in HTTP_METHODS:
            if op := (item or {}).get(method):
                desc = " ".join(filter(None, [op.get("summary"), op.get("description")]))
                caps.append(Capability(asset, "api",
                                       operation_name(op, method, route),
                                       desc.strip() or f"{method.upper()} {route}", source, owner))
    return caps


def parse_document(text: str, asset: str, asset_type: str, owner: str,
                   source: str) -> list[Capability]:
    """Detect the spec flavour from content rather than trusting the classifier."""
    if text.lstrip().startswith("#%RAML"):
        return parse_raml(text, asset, owner, source)
    doc = yaml.load(text, Loader=_LenientLoader)  # YAML is a superset of JSON
    if not isinstance(doc, dict):
        return []
    if "openapi" in doc or "swagger" in doc:
        return parse_openapi(doc, asset, owner, source)
    if isinstance(doc.get("tools"), list):  # Exchange MCP asset descriptor
        return [Capability(asset, "mcp", t["name"], (t.get("description") or "").strip(),
                           source, owner) for t in doc["tools"] if t.get("name")]
    if isinstance(doc.get("skills"), list):  # A2A agent card
        return _skills(doc, asset, owner, source)
    # Agent network: every card under brokers/agents is its own agent asset.
    return [c for card in _cards(doc)
            for c in _skills(card, card.get("name") or asset, owner, source)]


def _skills(card: dict, asset: str, owner: str, source: str) -> list[Capability]:
    out = []
    for s in card.get("skills") or []:
        desc = s.get("description", "")
        if s.get("examples"):
            desc += " Examples: " + "; ".join(str(e) for e in s["examples"][:5])
        out.append(Capability(asset, "agent", s.get("id") or s.get("name", ""),
                              desc.strip(), source, owner))
    return out


def _cards(node) -> list[dict]:
    """All A2A cards (dicts under a 'card' key that list skills) anywhere in the doc."""
    found = []
    if isinstance(node, dict):
        card = node.get("card")
        if isinstance(card, dict) and isinstance(card.get("skills"), list):
            found.append(card)
        for v in node.values():
            found += _cards(v)
    elif isinstance(node, list):
        for v in node:
            found += _cards(v)
    return found


def _main_text(blob: bytes, packaging: str, main_file: str | None) -> str:
    if packaging == "zip" or blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = z.namelist()
            # Exchange often leaves mainFile empty; the archive's exchange.json names the
            # root spec. Never fall back to exchange.json itself or to a nested fragment.
            if main_file not in names and "exchange.json" in names:
                main_file = json.loads(z.read("exchange.json")).get("main")
            name = main_file if main_file in names else next(
                (n for n in names if n.endswith((".yaml", ".yml", ".json", ".raml"))
                 and n != "exchange.json" and "exchange_modules" not in n
                 and "/" not in n), None)
            if name is None:
                raise ValueError("no spec file in archive")
            return z.read(name).decode("utf-8", "replace")
    return blob.decode("utf-8", "replace")


# ------------------------------------------------------------------------ source


def exchange_assets(types: list[str] | None = None, limit: int | None = None,
                    search: str | None = None, client: httpx.Client | None = None,
                    live_mcp: bool = True,
                    ) -> tuple[list[Capability], list[dict]]:
    """Returns (capabilities, per-asset notes). Notes record assets that fell back to
    their description or failed to download, so the report can show them."""
    base = os.environ.get("ANYPOINT_BASE_URL", DEFAULT_BASE).rstrip("/")
    own = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        auth = {"Authorization": f"Bearer {_token(client, base)}"}
        org = _org_id(client, base, auth)

        assets: list[dict] = []
        for t in types or DEFAULT_TYPES:
            offset = 0
            while True:
                params = {"organizationId": org, "types": t, "limit": 100, "offset": offset}
                if search:
                    params["search"] = search
                r = client.get(f"{base}/exchange/api/v2/assets/search", headers=auth,
                               params=params)
                if r.status_code == 400:  # unknown type in this region/version: skip it
                    break
                r.raise_for_status()
                page = r.json()
                assets += page
                if len(page) < 100:
                    break
                offset += 100
        # one entry per asset (search may return several versions)
        latest = {(a["groupId"], a["assetId"]): a for a in assets}
        if limit:
            latest = dict(list(latest.items())[:limit])

        caps, notes = [], []
        for (group, asset_id), a in latest.items():
            name = a.get("name") or asset_id
            atype = TYPE_TO_ASSET.get(a.get("type", ""), "api")
            owner = (a.get("createdBy") or {}).get("userName", "") if isinstance(
                a.get("createdBy"), dict) else ""
            src = f"exchange:{group}/{asset_id}/{a.get('version')}"
            live_reason = ""
            try:
                found, detail = _asset_capabilities(client, base, auth, a, name, atype, owner, src)
            except Exception as e:
                found, detail = [], {}
                notes.append({"asset": name, "note": f"spec download failed: {type(e).__name__}"})
            if not found and atype == "mcp" and live_mcp and detail:
                found, live_reason = _live_mcp_tools(client, base, auth, detail, name, owner)
                if found:
                    notes.append({"asset": name, "type": a.get("type"),
                                  "note": f"no tool metadata in Exchange; read tools/list live "
                                          f"from {live_reason}"})
                    live_reason = ""
            if not found:
                desc = (a.get("description") or "").strip()
                if desc:
                    found = [Capability(name, atype, name, desc, src, owner)]
                    notes.append({"asset": name, "type": a.get("type"),
                                  "note": "no parsable spec; used asset description"})
                else:
                    notes.append({"asset": name, "type": a.get("type"),
                                  "note": "no spec and no description; skipped"
                                          + (f" (live: {live_reason})" if live_reason else "")})
            caps += found
        # The same agent card can appear in several agent networks: count it once.
        unique = list({(c.asset, c.name): c for c in caps}.values())
        return unique, notes
    finally:
        if own:
            client.close()


def _live_mcp_tools(client: httpx.Client, base: str, auth: dict, detail: dict, name: str,
                    owner: str) -> tuple[list[Capability], str]:
    """Fallback for MCP assets published without tool metadata: look up each instance's
    upstream URL in API Manager and read tools/list from the running server."""
    from .ingest import mcp_tools  # local import: ingest imports this module lazily too

    org = detail.get("organizationId") or detail.get("groupId")
    reasons = []
    for inst in detail.get("instances") or []:
        env, api_id = inst.get("environmentId"), inst.get("id")
        if not (env and api_id):
            continue
        r = client.get(f"{base}/apimanager/api/v1/organizations/{org}/environments/{env}"
                       f"/apis/{api_id}", headers=auth)
        if r.status_code != 200:
            reasons.append(f"API Manager HTTP {r.status_code}")
            continue
        upstream = ((r.json().get("endpoint") or {}).get("uri") or "").rstrip("/")
        if not upstream:
            reasons.append("no upstream URL")
            continue
        url = upstream if upstream.endswith("/mcp") else f"{upstream}/mcp"
        try:
            caps = mcp_tools(name, url, owner=owner, timeout=15)
        except Exception as e:
            reasons.append(f"tools/list failed: {str(e)[:80]}")
            continue
        if caps:
            return caps, url
        reasons.append("server lists no tools")
    return [], "; ".join(reasons) or "no running instance"


def _asset_capabilities(client: httpx.Client, base: str, auth: dict, a: dict, name: str,
                        atype: str, owner: str, src: str) -> tuple[list[Capability], dict]:
    r = client.get(f"{base}/exchange/api/v2/assets/{a['groupId']}/{a['assetId']}/{a['version']}",
                   headers=auth)
    r.raise_for_status()
    detail = r.json()
    files = [f for f in detail.get("files", []) if f.get("externalLink") or f.get("downloadURL")]
    def rank(f: dict) -> tuple[int, int]:
        cls, pkg = f.get("classifier"), f.get("packaging", "")
        return (CLASSIFIER_ORDER.index(cls) if cls in CLASSIFIER_ORDER else len(CLASSIFIER_ORDER),
                PACKAGING_ORDER.index(pkg) if pkg in PACKAGING_ORDER else len(PACKAGING_ORDER))
    files.sort(key=rank)
    for f in files:
        if f.get("classifier") not in CLASSIFIER_ORDER:
            continue
        url = f.get("externalLink") or f.get("downloadURL")
        # presigned storage links reject extra auth headers; Anypoint URLs need them
        hdrs = auth if urlparse(url).netloc == urlparse(base).netloc else {}
        blob = client.get(url, headers=hdrs)
        blob.raise_for_status()
        text = _main_text(blob.content, f.get("packaging", ""), f.get("mainFile"))
        if caps := parse_document(text, name, atype, owner, src):
            return caps, detail
    return [], detail
