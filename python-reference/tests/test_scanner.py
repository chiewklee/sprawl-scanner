import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from sprawl_scanner.analyze import analyze
from sprawl_scanner.embed import LocalEmbedder, Thresholds
from sprawl_scanner.ingest import _parse_jsonrpc, agent_card, load_sources, openapi, seed
from sprawl_scanner.models import Capability, humanize
from sprawl_scanner.report import to_html

ROOT = Path(__file__).resolve().parents[1]
T = Thresholds(redundant=0.9, gray=0.7)


def cap(asset, name, desc="does a meaningful business task", t="mcp"):
    return Capability(asset=asset, asset_type=t, name=name, description=desc, source="test")


def unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def test_humanize():
    assert humanize("getAccountById") == "get account by id"
    assert humanize("GET /customers/{id}/opportunities") == "get customers id opportunities"
    assert humanize("create_incident") == "create incident"


def test_analyze_classifies_and_clusters():
    caps = [cap("A", "x1"), cap("B", "x2"), cap("C", "x3"),  # same capability, 3 assets
            cap("A", "y1"), cap("D", "y2"),                  # gray pair
            cap("E", "z")]                                   # unique
    emb = np.stack([unit(1, 0, 0), unit(1, 0.01, 0), unit(1, 0, 0.01),
                    unit(0, 1, 0), unit(0, 1, 0.8), unit(0, 0, 1)])
    a = analyze(caps, emb, "fake", T)
    assert a.status == ["redundant"] * 3 + ["gray", "gray", "unique"]
    assert len(a.clusters) == 1 and sorted(a.clusters[0].members) == [0, 1, 2]
    assert any("implemented 3 times" in r for r in a.recommendations)


def test_same_asset_pairs_ignored_by_default():
    caps = [cap("A", "x1"), cap("A", "x2")]
    emb = np.stack([unit(1, 0), unit(1, 0)])
    assert analyze(caps, emb, "fake", T).status == ["unique", "unique"]
    assert analyze(caps, emb, "fake", T, cross_asset_only=False).status == ["redundant"] * 2


def test_asset_consolidation_recommendation():
    caps = [cap("small", "a"), cap("small", "b"),
            cap("big", "a2"), cap("big", "b2"), cap("big", "c")]
    emb = np.stack([unit(1, 0, 0), unit(0, 1, 0), unit(1, 0, 0), unit(0, 1, 0), unit(0, 0, 1)])
    recs = analyze(caps, emb, "fake", T).recommendations
    assert any("'small' into 'big'" in r for r in recs)


def test_local_embedder_ranks_paraphrase_above_unrelated():
    texts = ["Create a new ServiceNow incident with urgency and assignment group",
             "Open a ServiceNow incident ticket with urgency and assignment group",
             "Return the current rate card by grade and region"]
    e = LocalEmbedder().embed(texts)
    assert e[0] @ e[1] > e[0] @ e[2] + 0.3


def test_openapi_and_agent_card(tmp_path):
    spec = tmp_path / "s.yaml"
    spec.write_text("openapi: 3.0.3\ninfo: {title: Demo API}\npaths:\n  /orders:\n"
                    "    get: {operationId: listOrders, summary: List orders}\n"
                    "    post: {summary: Create order}\n")
    caps = openapi(str(spec))
    assert [(c.asset, c.name) for c in caps] == [("Demo API", "listOrders"),
                                                 ("Demo API", "POST /orders")]
    card = tmp_path / "c.json"
    card.write_text(json.dumps({"name": "Agent", "skills": [
        {"id": "s1", "description": "Does X", "examples": ["do x"]}]}))
    (c,) = agent_card(str(card))
    assert c.asset_type == "agent" and "Examples: do x" in c.description


def test_parse_sse_response():
    resp = httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":1}}\n\n')
    assert _parse_jsonrpc(resp)["result"] == {"ok": 1}


def test_sources_skip_missing_env(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    cfg = tmp_path / "s.yaml"
    cfg.write_text(f"sources:\n  - kind: seed\n    path: {ROOT / 'seed/inventory.yaml'}\n"
                   "  - kind: mcp\n    name: x\n    url: http://127.0.0.1:9/mcp\n"
                   "    headers: {Authorization: 'Bearer ${NOPE_TOKEN}'}\n")
    r = load_sources(cfg)
    assert r.capabilities and not r.errors
    assert r.skipped == [{"source": "x", "reason": "env var NOPE_TOKEN not set"}]


def test_seed_finds_planted_sprawl_end_to_end():
    caps = seed(str(ROOT / "seed/inventory.yaml"))
    emb = LocalEmbedder()
    a = analyze(caps, emb.embed([c.text() for c in caps]), emb.name, emb.thresholds)

    def status(asset, name):
        return a.status[next(i for i, c in enumerate(caps) if (c.asset, c.name) == (asset, name))]

    assert status("itsm-sys-api", "createIncident") == "redundant"
    assert status("servicenow-mcp", "create_incident") == "redundant"
    assert status("pricing-api", "getRateCard") == "redundant"
    assert status("risk-onboarding-api", "screenSanctions") == "unique"
    assert status("partner-comp-agent", "comp-what-if") == "unique"
    html = to_html(a, type("R", (), {"scanned": [], "skipped": [], "errors": []})())
    assert "Sprawl Scanner" in html and "<script" in html


def test_preferred_asset_wins_and_is_never_retired():
    caps = [cap("direct", "a"), cap("direct", "b"), cap("gateway", "a2"), cap("gateway", "b2")]
    emb = np.stack([unit(1, 0), unit(0, 1), unit(1, 0), unit(0, 1)])
    a = analyze(caps, emb, "fake", T, preferred={"gateway"})
    assert any("near-duplicate" in r and "Consolidate into 'gateway'" in r
               for r in a.recommendations)
    assert all(caps[c.canonical].asset == "gateway" for c in a.clusters)


def test_cluster_never_holds_two_capabilities_of_one_asset():
    # read/write tools mirrored on two servers: two clusters, not one blob
    caps = [cap("gw", "read"), cap("gw", "write"), cap("direct", "read"), cap("direct", "write")]
    emb = np.stack([unit(1, 0.3), unit(1, 0.35), unit(1, 0.3), unit(1, 0.35)])
    a = analyze(caps, emb, "fake", T)
    assert sorted(sorted(c.members) for c in a.clusters) == [[0, 2], [1, 3]]


def test_exchange_documents():
    from sprawl_scanner.exchange import parse_document
    mcp = json.dumps({"transport": {}, "tools": [{"name": "get_ping", "description": "Ping SAP"}]})
    (c,) = parse_document(mcp, "sap-order-mcp-server", "mcp", "", "x")
    assert (c.asset_type, c.name) == ("mcp", "get_ping")
    network = ("schemaVersion: 1.0.0\nbrokers:\n  orch:\n    card:\n      name: Orchestrator\n"
               "      skills:\n        - id: route\n          description: Route requests\n"
               "agents:\n  billing:\n    card:\n      name: Billing Agent\n      skills:\n"
               "        - id: invoice\n          description: Explain an invoice\n"
               "          examples: [why is my bill high]\n")
    caps = parse_document(network, "net", "agent", "", "x")
    assert {(c.asset, c.name) for c in caps} == {("Orchestrator", "route"), ("Billing Agent", "invoice")}
    raml = "#%RAML 1.0\ntitle: T\n/orders:\n  get:\n    description: List orders\n  /{id}:\n    get: !include x.raml\n"
    assert [c.name for c in parse_document(raml, "T", "api", "", "x")] == ["GET /orders", "GET /orders/{id}"]
    stub = json.dumps({"protocol": "a2a", "kind": "agent", "ref": {}})
    assert parse_document(stub, "a", "agent", "", "x") == []


def test_thin_capabilities_are_never_redundant():
    caps = [cap("logger", "POST /entries", "POST /entries", "api"),
            cap("flights", "POST /flights", "POST /flights", "api")]
    emb = np.stack([unit(1, 0), unit(1, 0)])
    a = analyze(caps, emb, "fake", T)
    assert a.status == ["gray", "gray"]
    assert not any(o.redundant for o in a.overlaps) and not a.recommendations


def test_intentional_patterns_are_not_retire_recommendations():
    caps = [cap("sys-orders-api", "a", t="api"), cap("exp-orders-api", "a2", t="api"),
            cap("orders-mcp", "b", t="mcp"), cap("orders-api", "b2", t="api")]
    emb = np.stack([unit(1, 0, 0), unit(1, 0, 0), unit(0, 1, 0), unit(0, 1, 0)])
    recs = analyze(caps, emb, "fake", T).recommendations
    assert any("API-led layers" in r for r in recs)
    assert any("likely a facade" in r for r in recs)
    assert not any("Retire" in r for r in recs)


def test_near_duplicate_family_is_one_recommendation():
    caps = [cap(f"golf-{k}", "get_courses", "list the golf courses available for booking")
            for k in "abcd"]
    emb = np.stack([unit(1, 0)] * 4)
    recs = analyze(caps, emb, "fake", T).recommendations
    assert len(recs) == 1 and recs[0].startswith("4 near-duplicate assets")


def test_placeholder_operation_ids_fall_back_to_method_path():
    from sprawl_scanner.models import operation_name
    assert operation_name({"operationId": "null_7"}, "delete", "/orders/{po}") == "DELETE /orders/{po}"
    assert operation_name({"operationId": "null"}, "get", "/x") == "GET /x"
    assert operation_name({}, "get", "/x") == "GET /x"
    assert operation_name({"operationId": "getOrder"}, "get", "/x") == "getOrder"


def test_judge_cache_key_is_order_independent_and_change_sensitive():
    from sprawl_scanner.judge import _cache_key, _side
    a, b = _side(cap("A", "x", "create an incident")), _side(cap("B", "y", "open a ticket"))
    assert _cache_key("m", a, b) == _cache_key("m", b, a)
    assert _cache_key("m", a, b) != _cache_key("other-model", a, b)
    assert _cache_key("m", a, b) != _cache_key("m", a, _side(cap("B", "y", "open a ticket!")))


def test_report_names_both_models():
    caps = [cap("A", "x"), cap("B", "y")]
    a = analyze(caps, np.stack([unit(1, 0), unit(0.89, 0.456)]), "gemini:emb-test", T)
    a.judge_model = "judge-test"
    html = to_html(a, type("R", (), {"scanned": [], "skipped": [], "errors": []})())
    assert "gemini:emb-test" in html and "judge-test" in html


def test_zip_root_spec_comes_from_exchange_json():
    import io, zipfile
    from sprawl_scanner.exchange import _main_text
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("datatypes/Flight.raml", "#%RAML 1.0 DataType\ntype: object\n")
        z.writestr("exchange.json", json.dumps({"main": "api.raml"}))
        z.writestr("api.raml", "#%RAML 1.0\ntitle: Flights\n/flights:\n  get:\n")
    assert _main_text(buf.getvalue(), "zip", None).startswith("#%RAML 1.0\ntitle: Flights")


def test_agent_skill_matching_mcp_tool_is_not_retirement():
    caps = [cap("billing-mcp", "get_invoice", t="mcp"), cap("billing-broker", "invoice-skill", t="agent")]
    recs = analyze(caps, np.stack([unit(1, 0), unit(1, 0)]), "fake", T).recommendations
    assert recs and all("likely the agent calling" in r for r in recs)
