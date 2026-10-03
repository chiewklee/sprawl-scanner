"""sprawl-scan CLI.

  sprawl-scan scan  [--sources sources.yaml] [--out out] [--embedder auto|gemini|local]
                    [--no-live] [--no-judge]
  sprawl-scan find  "create a ServiceNow incident"   # does this already exist?
  sprawl-scan serve [--transport stdio|streamable-http] [--port 8765]
"""

from __future__ import annotations

import argparse
import sys

from .scan import run_scan


def _scan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sources", default="sources.yaml")
    p.add_argument("--embedder", choices=["auto", "gemini", "local"], default="auto")
    p.add_argument("--no-live", action="store_true", help="skip live MCP servers")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sprawl-scan")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="scan sources and write the report")
    _scan_args(s)
    s.add_argument("--out", default="out")
    s.add_argument("--no-judge", action="store_true", help="skip the Gemini judge")
    s.add_argument("--judge-limit", type=int, default=25, help="max gray pairs to judge")

    f = sub.add_parser("find", help="find existing capabilities similar to a description")
    _scan_args(f)
    f.add_argument("description")
    f.add_argument("--top", type=int, default=5)

    v = sub.add_parser("serve", help="run as an MCP server")
    v.add_argument("--sources", default="sources.yaml")
    v.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    v.add_argument("--port", type=int, default=8765)

    a = ap.parse_args(argv)

    if a.cmd == "serve":
        from .server import serve
        serve(a.sources, a.transport, a.port)
        return 0

    if a.cmd == "find":
        run = run_scan(a.sources, None, a.embedder, not a.no_live, judge=False)
        t = run.analysis.thresholds
        for cap, score in run.similar(a.description, a.top):
            tag = "REDUNDANT" if score >= t.redundant else "gray" if score >= t.gray else ""
            print(f"{score:.3f} {tag:9} {cap.asset_type:5} {cap.asset} / {cap.name}")
        return 0

    run = run_scan(a.sources, a.out, a.embedder, not a.no_live, judge=not a.no_judge,
                   judge_limit=a.judge_limit)
    c = run.analysis.counts()
    print(f"embedder: {run.analysis.embedder} | judge: {run.analysis.judge_model or 'off'}")
    for line in run.ingest.scanned:
        print(f"  ✓ {line}")
    for x in run.ingest.skipped:
        print(f"  ↷ {x['source']}: {x['reason']}")
    if run.ingest.notes:
        print(f"  ! {len(run.ingest.notes)} asset notes (fallbacks/skips), see report")
    for x in run.ingest.errors:
        print(f"  ✕ {x['source']}: {x['error']}", file=sys.stderr)
    print(f"{len(run.analysis.assets())} assets, {len(run.analysis.capabilities)} capabilities: "
          f"{c['redundant']} redundant, {c['gray']} gray, {c['unique']} unique; "
          f"{len(run.analysis.clusters)} clusters; {run.judged} gray pairs judged "
          f"({run.judge_cached} from cache)")
    for r in run.analysis.recommendations:
        print(f"  → {r}")
    print(f"report: {run.html_path}\n  json: {run.json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
