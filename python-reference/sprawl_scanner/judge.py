"""LLM judge for gray-area pairs: Gemini decides whether two capabilities are true
duplicates, partial overlaps or distinct, and explains why in one sentence."""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .analyze import Analysis
from .embed import gemini_available

PROMPT = """You are auditing an enterprise integration catalog for duplicated capabilities.
Two capabilities from different assets scored as semantically similar. Decide:
- "duplicate": they do the same job; one could replace the other.
- "overlap": they share part of the job but each does something the other does not.
- "distinct": they only look similar; different purpose or business object.

A ({a_type} '{a_asset}'): {a_name}: {a_desc}
B ({b_type} '{b_asset}'): {b_name}: {b_desc}

Reply as JSON: {{"verdict": "duplicate|overlap|distinct", "rationale": "<one sentence>"}}"""


CACHE_DIR = Path.home() / ".cache" / "sprawl-scanner"


def _side(c) -> tuple[str, str, str, str]:
    return (c.asset_type, c.asset, c.name, c.description[:800])


def _cache_key(model: str, a: tuple, b: tuple) -> str:
    """Order-independent: A~B and B~A share one verdict. Any edit to either side's
    type, asset, name or description, or a different model, is a cache miss."""
    return hashlib.sha256(json.dumps([model, *sorted([a, b])]).encode()).hexdigest()


def judge_gray_pairs(analysis: Analysis, limit: int = 25,
                     cache_dir: Path | None = None) -> tuple[int, int]:
    """Annotate the top `limit` gray pairs in place.
    Returns (judged, served_from_cache). Only successful verdicts are cached, so pairs
    that failed (e.g. HTTP 429) are retried on the next scan."""
    if not gemini_available():
        return 0, 0
    from google import genai
    from google.genai import types

    client = genai.Client()
    # Pro is the strongest judge available; it was the tiebreaker when comparing
    # 2.5-flash vs 3.8-flash (scripts/compare_judges.py). Set GEMINI_JUDGE_MODEL=
    # gemini-3.8-flash for a faster, cheaper run.
    model = os.environ.get("GEMINI_JUDGE_MODEL", "gemini-3.1-pro-preview")
    analysis.judge_model = model
    cache_dir = cache_dir or CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"judge-{model}.json"
    cache: dict[str, dict] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    config = types.GenerateContentConfig(
        response_mime_type="application/json", temperature=0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))

    def judge(pair) -> str:  # "cached" | "judged" | "failed"
        a, b = sorted([_side(analysis.capabilities[pair.a]), _side(analysis.capabilities[pair.b])])
        key = _cache_key(model, a, b)
        if hit := cache.get(key):
            pair.verdict, pair.rationale = hit["verdict"], hit["rationale"]
            return "cached"
        prompt = PROMPT.format(a_type=a[0], a_asset=a[1], a_name=a[2], a_desc=a[3],
                               b_type=b[0], b_asset=b[1], b_name=b[2], b_desc=b[3])
        for attempt in range(3):
            try:
                out = json.loads(client.models.generate_content(
                    model=model, contents=prompt, config=config).text)
                pair.verdict, pair.rationale = out.get("verdict"), out.get("rationale")
                cache[key] = {"verdict": pair.verdict, "rationale": pair.rationale}
                return "judged"
            except Exception as e:  # judge is best-effort; the score still stands
                code = getattr(e, "code", None)
                # Never retry a 429: back-to-back retries only deepen the rate limit.
                if code == 429 or attempt == 2:
                    pair.rationale = f"(judge unavailable: HTTP {code or type(e).__name__})"
                    return "failed"
                time.sleep(2 ** attempt)
        return "failed"

    gray = [p for p in analysis.pairs if p.kind == "gray"][:limit]
    # Pro/preview models have tighter per-minute quotas than Flash.
    workers = int(os.environ.get("GEMINI_JUDGE_WORKERS", 3 if "pro" in model else 8))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(judge, gray))
    finally:  # keep whatever was judged even if the run is interrupted
        cache_path.write_text(json.dumps(cache))
    cached = outcomes.count("cached")
    return outcomes.count("judged") + cached, cached
