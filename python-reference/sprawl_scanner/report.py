"""Render an Analysis as JSON and as a self-contained HTML report.

Layout: KPI row (status counts) -> recommendations -> asset-overlap heatmap
(sequential blue, one hue) -> redundancy clusters -> gray-area pairs -> sources.
Status colours always ship with an icon + label; the heatmap is an HTML table, so
it doubles as its own table view.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from .analyze import Analysis
from .ingest import IngestResult

STATUS = {  # icon + label + reserved status colour
    "unique": ("✓", "Unique", "var(--good)"),
    "gray": ("◐", "Gray area", "var(--warning)"),
    "redundant": ("⧉", "Redundant", "var(--serious)"),
}
# Sequential blue ramp (steps 150 -> 650) for coverage > 0; 0 stays on the surface.
RAMP = ["#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5",
        "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281"]
MAX_HEATMAP_ASSETS = 30


def to_dict(analysis: Analysis, ingest: IngestResult) -> dict:
    caps = analysis.capabilities

    def cap(i: int) -> dict:
        return {**caps[i].to_dict(), "status": analysis.status[i]}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "embedder": analysis.embedder,
        "judge": _judge_stats(analysis),
        "thresholds": vars(analysis.thresholds),
        "summary": {"assets": len(analysis.assets()), "capabilities": len(caps),
                    **analysis.counts(), "undocumented": len(_gaps(ingest))},
        "recommendations": analysis.recommendations,
        "clusters": [{"canonical": caps[c.canonical].id, "mean_score": round(c.mean_score, 3),
                      "members": [cap(i) for i in c.members]} for c in analysis.clusters],
        "gray_pairs": [{"a": caps[p.a].id, "b": caps[p.b].id, "score": round(p.score, 3),
                        "verdict": p.verdict, "rationale": p.rationale}
                       for p in analysis.pairs if p.kind == "gray"],
        "asset_overlap": [{"source": o.source, "target": o.target, "redundant": o.redundant,
                           "gray": o.gray, "total": o.total, "coverage": round(o.coverage, 3)}
                          for o in analysis.overlaps],
        "capabilities": [cap(i) for i in range(len(caps))],
        "ingest": {"scanned": ingest.scanned, "skipped": ingest.skipped,
                   "errors": ingest.errors, "notes": getattr(ingest, "notes", [])},
    }


def _judge_stats(analysis: Analysis) -> dict:
    gray = [p for p in analysis.pairs if p.kind == "gray"]
    failed = sum(1 for p in gray if (p.rationale or "").startswith("(judge unavailable"))
    judged = sum(1 for p in gray if p.verdict)
    return {"model": analysis.judge_model, "gray_pairs": len(gray), "judged": judged,
            "failed": failed, "not_judged": len(gray) - judged - failed}


def _gaps(ingest: IngestResult) -> list[dict]:
    """Assets the catalog lists but doesn't describe (no tools/skills/operations).
    Agent stubs whose skills live in an agent network are not gaps."""
    return [n for n in getattr(ingest, "notes", [])
            if n["note"].startswith("no spec and no description") and n.get("type") != "agent"]


def _e(s: object) -> str:
    return html.escape(str(s), quote=True)


def _badge(status: str) -> str:
    icon, label, color = STATUS[status]
    return (f'<span class="badge"><span class="dot" style="background:{color}">{icon}</span>'
            f"{label}</span>")


def _cell_color(coverage: float) -> tuple[str, str]:
    if coverage <= 0:
        return "transparent", "var(--text-muted)"
    step = min(len(RAMP) - 1, int(coverage * (len(RAMP) - 1) + 0.5))
    return RAMP[step], ("#ffffff" if step >= 6 else "#0b0b0b")


def _heatmap(analysis: Analysis) -> str:
    cov = {(o.source, o.target): o for o in analysis.overlaps}
    involved = sorted({o.source for o in analysis.overlaps} | {o.target for o in analysis.overlaps},
                      key=lambda a: -sum(o.coverage for o in analysis.overlaps
                                         if a in (o.source, o.target)))[:MAX_HEATMAP_ASSETS]
    if not involved:
        return '<p class="muted">No cross-asset overlap found.</p>'
    def short(a: str, n: int = 26) -> str:
        return a if len(a) <= n else a[: n - 1] + "…"

    head = "".join(f'<th scope="col" title="{_e(a)}"><span class="vert">{_e(short(a))}</span></th>'
                   for a in involved)
    rows = []
    for src in involved:
        cells = []
        for tgt in involved:
            if src == tgt:
                cells.append('<td class="self" aria-label="same asset">—</td>')
                continue
            o = cov.get((src, tgt))
            c = o.coverage if o else 0.0
            bg, fg = _cell_color(c)
            tip = (f"{src} → {tgt}: {o.redundant}/{o.total} redundant, {o.gray} gray"
                   if o else f"{src} → {tgt}: no overlap")
            label = f"{round(c * 100)}%" if o and o.redundant else ("·" if o else "")
            cells.append(f'<td class="cell" style="background:{bg};color:{fg}" '
                         f'data-tip="{_e(tip)}">{label}</td>')
        rows.append(f'<tr><th scope="row" title="{_e(src)}">{_e(short(src, 34))}</th>'
                    f'{"".join(cells)}</tr>')
    return (f'<div class="scroll"><table class="heat"><thead><tr><th class="corner">'
            f'covered ↓ / by →</th>{head}</tr></thead><tbody>{"".join(rows)}</tbody>'
            f'</table></div><p class="muted legend">Cell = share of the row asset\'s '
            f'capabilities that already exist (redundant) in the column asset. '
            f'<span class="ramp">{"".join(f"<i style=background:{c}></i>" for c in RAMP)}'
            f'</span> 0% → 100% &nbsp; · = gray-area overlap only. Hover a cell for counts.</p>')


def to_html(analysis: Analysis, ingest: IngestResult) -> str:
    d = to_dict(analysis, ingest)
    caps, s, j = analysis.capabilities, d["summary"], d["judge"]

    kpis = "".join(
        f'<div class="kpi"><div class="kpi-label">{label}</div><div class="kpi-value">{value}</div>'
        f"{extra}</div>" for label, value, extra in [
            ("Assets scanned", s["assets"], ""),
            ("Capabilities", s["capabilities"], ""),
            ("Redundant", s["redundant"], _badge("redundant")),
            ("Gray area", s["gray"], _badge("gray")),
            ("Unique", s["unique"], _badge("unique")),
            ("Undocumented", s["undocumented"],
             '<span class="badge"><span class="dot" style="background:var(--critical);'
             'color:#fff">!</span>No tool/op metadata</span>'),
        ])

    gaps = _gaps(ingest)
    by_type: dict[str, list[str]] = {}
    for g in gaps:
        by_type.setdefault(g.get("type") or "unknown", []).append(g["asset"])
    gap_html = "".join(
        f"<p><b>{_e(t)}</b> ({len(names)}): " + ", ".join(f"<code>{_e(n)}</code>" for n in sorted(names))
        + "</p>" for t, names in sorted(by_type.items()))

    recs = "".join(f"<li>{_e(r)}</li>" for r in d["recommendations"]) or \
        '<li class="muted">No consolidation candidates above the threshold.</li>'

    clusters = []
    for n, c in enumerate(analysis.clusters, 1):
        members = "".join(
            f'<tr><td>{"★ " if i == c.canonical else ""}<code>{_e(caps[i].name)}</code></td>'
            f"<td>{_e(caps[i].asset)}</td><td>{_e(caps[i].asset_type)}</td>"
            f'<td class="desc">{_e(caps[i].description[:220])}</td></tr>' for i in c.members)
        clusters.append(
            f'<details {"open" if n <= 5 else ""}><summary><b>Cluster {n}</b>: '
            f"{len(c.members)} implementations across "
            f"{len({caps[i].asset for i in c.members})} assets · mean similarity "
            f"{c.mean_score:.2f} · canonical: <code>{_e(caps[c.canonical].asset)} / "
            f"{_e(caps[c.canonical].name)}</code></summary><table class=\"grid\"><thead><tr>"
            f"<th>Capability</th><th>Asset</th><th>Type</th><th>Description</th></tr></thead>"
            f"<tbody>{members}</tbody></table></details>")

    gray_rows = "".join(
        f"<tr><td>{p.score:.2f}</td><td><code>{_e(caps[p.a].name)}</code><br>"
        f'<span class="muted">{_e(caps[p.a].asset)}</span></td><td><code>{_e(caps[p.b].name)}'
        f'</code><br><span class="muted">{_e(caps[p.b].asset)}</span></td>'
        f'<td>{_e(p.verdict or "—")}</td><td class="desc">{_e(p.rationale or "")}</td></tr>'
        for p in analysis.pairs if p.kind == "gray")

    src_rows = "".join(f"<li>✓ {_e(x)}</li>" for x in ingest.scanned)
    src_rows += "".join(f'<li class="muted">↷ {_e(x["source"])}: skipped ({_e(x["reason"])})</li>'
                        for x in ingest.skipped)
    src_rows += "".join(f'<li class="err">✕ {_e(x["source"])}: {_e(x["error"])}</li>'
                        for x in ingest.errors)
    notes = getattr(ingest, "notes", [])
    if notes:
        src_rows += (f'<li><details><summary>{len(notes)} asset notes (fallbacks / skips)</summary>'
                     '<ul>' + "".join(f'<li class="muted">{_e(n["asset"])}: {_e(n["note"])}</li>'
                                      for n in notes) + '</ul></details></li>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sprawl Scanner report</title>
<style>
.viz-root {{
  color-scheme: light;
  --surface-0: #f4f3f0; --surface-1: #fcfcfb; --border: #e2e1dc;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #7a7974;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) .viz-root {{
    color-scheme: dark;
    --surface-0: #121211; --surface-1: #1a1a19; --border: #383835;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #8f8e86;
  }}
}}
:root[data-theme="dark"] .viz-root {{
  color-scheme: dark;
  --surface-0: #121211; --surface-1: #1a1a19; --border: #383835;
  --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #8f8e86;
}}
body {{ margin: 0; }}
.viz-root {{ background: var(--surface-0); color: var(--text-primary);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  min-height: 100vh; padding: 24px 32px; box-sizing: border-box; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }} h2 {{ font-size: 16px; margin: 0 0 12px; }}
.muted {{ color: var(--text-muted); }} .err {{ color: var(--critical); }}
section {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
  padding: 16px 20px; margin: 16px 0; }}
.kpis {{ display: grid; grid-template-columns: repeat(6, minmax(130px, 1fr)); gap: 12px; }}
.kpi {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 16px; }}
.kpi-label {{ color: var(--text-secondary); font-size: 12px; }}
.kpi-value {{ font-size: 32px; font-weight: 600; margin: 2px 0 6px; }}
.badge {{ display: inline-flex; gap: 6px; align-items: center; font-size: 12px;
  color: var(--text-secondary); }}
.dot {{ display: inline-grid; place-items: center; width: 16px; height: 16px;
  border-radius: 50%; color: #0b0b0b; font-size: 10px; }}
.scroll {{ overflow: auto; }}
table {{ border-collapse: separate; border-spacing: 2px; }}
.heat th {{ font-weight: 500; color: var(--text-secondary); font-size: 12px; text-align: right;
  padding: 2px 8px; white-space: nowrap; }}
.heat thead th {{ vertical-align: bottom; text-align: left; }}
.vert {{ writing-mode: vertical-rl; transform: rotate(180deg); }}
.heat td {{ width: 34px; height: 30px; text-align: center; font-size: 11px; border-radius: 4px; }}
.heat td.cell {{ border: 1px solid var(--border); cursor: default; }}
.heat td.cell:hover {{ outline: 2px solid var(--text-primary); }}
.heat td.self {{ color: var(--text-muted); }}
.corner {{ color: var(--text-muted); font-weight: 400; }}
.legend {{ font-size: 12px; margin-top: 8px; }}
.ramp {{ display: inline-flex; vertical-align: middle; margin: 0 6px; }}
.ramp i {{ width: 14px; height: 10px; display: inline-block; }}
.grid {{ width: 100%; border-spacing: 0; margin: 8px 0 4px; }}
.grid th, .grid td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border);
  vertical-align: top; }}
.grid th {{ color: var(--text-secondary); font-weight: 500; font-size: 12px; }}
.desc {{ color: var(--text-secondary); max-width: 560px; }}
details {{ border-top: 1px solid var(--border); padding: 8px 0; }}
summary {{ cursor: pointer; }}
code {{ font-size: 12px; }}
.models {{ width: auto; margin-top: 10px; }} .models th {{ white-space: nowrap; }}
#tip {{ position: fixed; pointer-events: none; background: var(--surface-1);
  color: var(--text-primary); border: 1px solid var(--border); border-radius: 6px;
  padding: 6px 10px; font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.15); display: none; }}
ul {{ margin: 0; padding-left: 20px; }} li {{ margin: 4px 0; }}
.theme {{ float: right; }}
</style></head>
<body><div class="viz-root">
<button class="theme" onclick="const r=document.documentElement;r.dataset.theme=
  (r.dataset.theme==='dark'||(!r.dataset.theme&&matchMedia('(prefers-color-scheme: dark)').matches))
  ?'light':'dark'">Toggle theme</button>
<h1>Sprawl Scanner</h1>
<div class="muted">Generated {_e(d["generated_at"])}
 · similarity thresholds: redundant ≥ {analysis.thresholds.redundant}, gray ≥ {analysis.thresholds.gray}</div>
<table class="grid models"><tbody>
<tr><th>Embedding model</th><td><code>{_e(d["embedder"])}</code></td>
<td class="muted">scores every capability pair: drives redundant / gray / unique, clusters and recommendations</td></tr>
<tr><th>Judge model</th><td><code>{_e(j["model"] or "none (judge off)")}</code></td>
<td class="muted">verdict + rationale for gray pairs only: {j["judged"]} of {j["gray_pairs"]} judged{f", {j['failed']} failed (rate limit / error)" if j["failed"] else ""}{f", {j['not_judged']} beyond --judge-limit" if j["not_judged"] else ""}</td></tr>
</tbody></table>
<section style="background:none;border:none;padding:0"><div class="kpis">{kpis}</div></section>
<section><h2>Recommendations</h2><ul>{recs}</ul></section>
<section><h2>Asset overlap</h2>{_heatmap(analysis)}</section>
<section><h2>Redundancy clusters ({len(analysis.clusters)})</h2>
{"".join(clusters) or '<p class="muted">None.</p>'}</section>
<section><h2>Gray areas ({s["gray"]} capabilities)</h2>
{f'<div class="scroll"><table class="grid"><thead><tr><th>Score</th><th>Capability A</th><th>Capability B</th><th>Verdict <span class="muted">({_e(j["model"] or "—")})</span></th><th>Rationale</th></tr></thead><tbody>{gray_rows}</tbody></table></div>' if gray_rows else '<p class="muted">None.</p>'}
</section>
<section><h2>Catalog gaps ({len(gaps)})</h2>
{('<p class="muted">Published without tool, skill or operation metadata, so they cannot be '
  'compared, and Exchange search cannot see what they do either. Republish them with their '
  'tool list (MCP) or spec (API) to bring them into scope.</p>' + gap_html) if gaps else
 '<p class="muted">Every asset carries capability metadata.</p>'}</section>
<section><h2>Sources</h2><ul>{src_rows}</ul></section>
<div id="tip" role="tooltip"></div>
</div>
<script>
const tip = document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el => {{
  el.addEventListener('mousemove', e => {{ tip.textContent = el.dataset.tip;
    tip.style.display = 'block'; tip.style.left = (e.clientX + 12) + 'px';
    tip.style.top = (e.clientY + 12) + 'px'; }});
  el.addEventListener('mouseleave', () => tip.style.display = 'none');
}});
</script>
<script type="application/json" id="data">{json.dumps(d).replace("</", "<\\/")}</script>
</body></html>"""
