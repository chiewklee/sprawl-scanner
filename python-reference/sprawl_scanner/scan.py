"""One scan, end to end: ingest -> embed -> analyze -> judge -> report. Shared by the
CLI and the MCP server."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .analyze import Analysis, analyze, find_similar
from .embed import get_embedder
from .ingest import IngestResult, load_sources
from .judge import judge_gray_pairs
from .models import Capability
from .report import to_dict, to_html


@dataclass
class ScanRun:
    ingest: IngestResult
    analysis: Analysis
    embeddings: np.ndarray
    embedder: object
    judged: int
    judge_cached: int = 0  # of `judged`, how many came from the verdict cache
    html_path: Path | None = None
    json_path: Path | None = None

    def similar(self, description: str, top_k: int = 5) -> list[tuple[Capability, float]]:
        """'Does a capability like this already exist?' against the scanned inventory."""
        query = self.embedder.embed([c.text() for c in self.analysis.capabilities] + [description])
        # Local TF-IDF fits its vocabulary per call, so embed corpus + query together.
        return find_similar(self.analysis.capabilities, query[-1], query[:-1], top_k)


def run_scan(sources: str | Path, out_dir: str | Path | None = "out", embedder: str = "auto",
             include_live: bool = True, judge: bool = True, judge_limit: int = 25) -> ScanRun:
    ingest = load_sources(sources, include_live=include_live)
    if not ingest.capabilities:
        raise RuntimeError(f"no capabilities ingested; errors: {ingest.errors}")
    emb = get_embedder(embedder)
    vectors = emb.embed([c.text() for c in ingest.capabilities])
    analysis = analyze(ingest.capabilities, vectors, emb.name, emb.thresholds,
                       preferred=ingest.preferred)
    judged, judge_cached = judge_gray_pairs(analysis, judge_limit) if judge else (0, 0)
    run = ScanRun(ingest, analysis, vectors, emb, judged, judge_cached)

    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        run.html_path = out / "sprawl-report.html"
        run.json_path = out / "sprawl-report.json"
        run.html_path.write_text(to_html(analysis, ingest))
        run.json_path.write_text(json.dumps(to_dict(analysis, ingest), indent=2))
    return run
