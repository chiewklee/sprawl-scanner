"""Ingestion: turn MCP servers, A2A agent cards, OpenAPI specs and seed files into
Capability records.

Sources are declared in a YAML file (see sources.yaml). Secrets never live in that
file: headers support ${ENV_VAR} expansion and `bearer_file` reads a token from disk.
A source whose required env var is unset is skipped, not failed.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from .models import ASSET_TYPES, Capability, operation_name

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
MCP_PROTOCOL_VERSION = "2025-06-18"
_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


class SkipSource(Exception):
    """Source is intentionally not scanned (e.g. credentials not configured)."""


@dataclass
class IngestResult:
    capabilities: list[Capability] = field(default_factory=list)
    scanned: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    preferred: set[str] = field(default_factory=set)  # assets that win consolidation ties
    notes: list[dict] = field(default_factory=list)  # per-asset caveats (e.g. Exchange fallbacks)


# --------------------------------------------------------------------------- MCP


def _parse_jsonrpc(resp: httpx.Response) -> dict:
    """Streamable HTTP servers answer with plain JSON or an SSE stream."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                msg = json.loads(line[5:].strip())
                if "result" in msg or "error" in msg:
                    return msg
        raise ValueError("no JSON-RPC response in SSE stream")
    return resp.json()


def mcp_tools(asset: str, url: str, headers: dict[str, str] | None = None,
              owner: str = "", timeout: float = 30.0) -> list[Capability]:
    """initialize -> notifications/initialized -> tools/list (paginated)."""
    base = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream", **(headers or {})}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        init = client.post(url, headers=base, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "sprawl-scanner", "version": "0.1.0"}}})
        if init.status_code >= 400:
            raise RuntimeError(f"initialize HTTP {init.status_code}: {init.text[:200]}")
        _parse_jsonrpc(init)
        session = {**base}
        if sid := init.headers.get("mcp-session-id"):
            session["mcp-session-id"] = sid
        client.post(url, headers=session,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools, cursor, req_id = [], None, 2
        while True:
            params = {"cursor": cursor} if cursor else {}
            resp = client.post(url, headers=session, json={
                "jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": params})
            if resp.status_code >= 400:
                raise RuntimeError(f"tools/list HTTP {resp.status_code}: {resp.text[:200]}")
            msg = _parse_jsonrpc(resp)
            if "error" in msg:
                raise RuntimeError(f"tools/list error: {msg['error']}")
            tools += msg["result"].get("tools", [])
            cursor = msg["result"].get("nextCursor")
            req_id += 1
            if not cursor:
                break

    return [Capability(asset=asset, asset_type="mcp", name=t["name"],
                       description=(t.get("description") or "").strip(),
                       source=url, owner=owner) for t in tools]


# ------------------------------------------------------------------ Agent cards


def _load_json_or_url(location: str, headers: dict[str, str] | None = None) -> dict:
    if location.startswith(("http://", "https://")):
        resp = httpx.get(location, headers=headers or {}, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        return resp.json()
    return json.loads(Path(location).expanduser().read_text())


def agent_card(location: str, asset: str | None = None, owner: str = "",
               headers: dict[str, str] | None = None) -> list[Capability]:
    """A2A agent card: each skill becomes a capability (description + examples)."""
    card = _load_json_or_url(location, headers)
    name = asset or card.get("name", Path(location).stem)
    owner = owner or (card.get("provider") or {}).get("organization", "")
    caps = []
    for skill in card.get("skills", []):
        desc = skill.get("description", "")
        if examples := skill.get("examples"):
            desc += " Examples: " + "; ".join(examples[:5])
        caps.append(Capability(asset=name, asset_type="agent",
                               name=skill.get("id") or skill.get("name", ""),
                               description=desc.strip(), source=location, owner=owner))
    return caps


# ---------------------------------------------------------------------- OpenAPI


def openapi(location: str, asset: str | None = None, owner: str = "") -> list[Capability]:
    """OpenAPI 3.x / Swagger 2: each path+method operation becomes a capability."""
    path = Path(location).expanduser()
    spec = yaml.safe_load(path.read_text())
    info = spec.get("info", {})
    name = asset or info.get("title") or path.stem
    caps = []
    for route, item in (spec.get("paths") or {}).items():
        for method in HTTP_METHODS:
            op = (item or {}).get(method)
            if not op:
                continue
            desc = " ".join(filter(None, [op.get("summary"), op.get("description")]))
            caps.append(Capability(asset=name, asset_type="api",
                                   name=operation_name(op, method, route),
                                   description=desc.strip() or f"{method.upper()} {route}",
                                   source=str(path), owner=owner))
    return caps


# ------------------------------------------------------------------------- Seed


def seed(location: str) -> list[Capability]:
    """Seed inventory: assets: [{name, type, owner, capabilities: [{name, description}]}]."""
    path = Path(location).expanduser()
    data = yaml.safe_load(path.read_text())
    caps = []
    for a in data.get("assets", []):
        if a["type"] not in ASSET_TYPES:
            raise ValueError(f"{a['name']}: type must be one of {ASSET_TYPES}")
        for c in a.get("capabilities", []):
            caps.append(Capability(asset=a["name"], asset_type=a["type"], name=c["name"],
                                   description=c.get("description", ""),
                                   source=f"seed:{path.name}", owner=a.get("owner", "")))
    return caps


# ---------------------------------------------------------------------- Sources


def _expand_env(value: str) -> str:
    def sub(m: re.Match) -> str:
        if (v := os.environ.get(m.group(1))) is None:
            raise SkipSource(f"env var {m.group(1)} not set")
        return v
    return _ENV_REF.sub(sub, value)


def _headers(src: dict) -> dict[str, str]:
    headers = {k: _expand_env(str(v)) for k, v in (src.get("headers") or {}).items()}
    if bearer_file := src.get("bearer_file"):
        p = Path(_expand_env(bearer_file)).expanduser()
        if not p.exists():
            raise SkipSource(f"bearer_file {p} not found")
        headers["Authorization"] = f"Bearer {p.read_text().strip()}"
    return headers


def load_sources(config_path: str | Path, include_live: bool = True) -> IngestResult:
    config_path = Path(config_path).expanduser()
    cfg = yaml.safe_load(config_path.read_text())
    base_dir = config_path.parent
    result = IngestResult(preferred=set(cfg.get("preferred_assets") or []))

    def resolve(p: str) -> str:
        if p.startswith(("http://", "https://")):
            return p
        q = Path(p).expanduser()
        return str(q if q.is_absolute() else base_dir / q)

    for src in cfg.get("sources", []):
        kind = src["kind"]
        label = (src.get("name") or src.get("path") or src.get("url")
                 or ("anypoint-exchange" if kind == "exchange" else kind))
        if src.get("enabled", True) is False:
            result.skipped.append({"source": label, "reason": "disabled"})
            continue
        if kind in ("mcp", "exchange") and not include_live:
            result.skipped.append({"source": label, "reason": "live sources disabled"})
            continue
        try:
            if kind == "seed":
                caps = seed(resolve(src["path"]))
            elif kind == "openapi":
                caps = openapi(resolve(src["path"]), src.get("name"), src.get("owner", ""))
            elif kind == "agent_card":
                caps = agent_card(resolve(src.get("url") or src["path"]), src.get("name"),
                                  src.get("owner", ""), _headers(src))
            elif kind == "mcp":
                caps = mcp_tools(src["name"], src["url"], _headers(src), src.get("owner", ""))
            elif kind == "exchange":
                from .exchange import exchange_assets
                caps, notes = exchange_assets(src.get("types"), src.get("limit"), src.get("search"),
                                              live_mcp=src.get("live_mcp", True))
                result.notes += [{"source": label, **n} for n in notes]
            else:
                raise ValueError(f"unknown source kind '{kind}'")
        except SkipSource as e:
            result.skipped.append({"source": label, "reason": str(e)})
            continue
        except Exception as e:  # one bad source must not sink the scan
            result.errors.append({"source": label, "error": f"{type(e).__name__}: {e}"})
            continue
        result.capabilities += caps
        result.scanned.append(f"{label} ({len(caps)})")
    return result
