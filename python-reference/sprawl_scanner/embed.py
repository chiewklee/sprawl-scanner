"""Embedders. Gemini is the default when credentials are present; LocalEmbedder is a
dependency-free TF-IDF fallback so the scanner (and its tests) run with no keys.

Each embedder carries its own similarity thresholds, because cosine scores are not
comparable across models. Calibrate against the seed inventory when switching models.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class Thresholds:
    redundant: float  # >= this: same capability, consolidate
    gray: float  # >= this (and < redundant): overlapping, needs a human/LLM look


def _normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(norms == 0, 1, norms)


# ------------------------------------------------------------------------ Local

_STOPWORDS = set("""a an and are as at be by can for from has have i in into is it its of on
or that the this to via with will you your their them these those which who what when
where how use used using leverage leveraged able allow allows assist help helps related
queries query tasks task i.e e.g etc examples example given returns return based new""".split())

# Collapse common verb/noun synonyms so "fetch customer" ~ "get account" without a model.
_SYNONYMS = {
    "retrieve": "get", "fetch": "get", "lookup": "get", "read": "get", "find": "get",
    "search": "get", "list": "get", "query": "get", "view": "get", "show": "get",
    "obtain": "get", "load": "get",
    "add": "create", "insert": "create", "new": "create", "open": "create",
    "submit": "create", "raise": "create", "register": "create", "log": "create",
    "modify": "update", "edit": "update", "change": "update", "patch": "update",
    "remove": "delete", "cancel": "delete", "purge": "delete",
    "customer": "account", "client": "account", "company": "account",
    "incident": "ticket", "issue": "ticket", "case": "ticket",
    "employee": "worker", "staff": "worker", "personnel": "worker",
    "summarize": "summary", "summarise": "summary", "brief": "summary",
    "briefing": "summary", "overview": "summary",
    "price": "quote", "pricing": "quote", "quotation": "quote",
    "opportunity": "deal", "oppty": "deal", "pursuit": "deal",
}


def _stem(w: str) -> str:
    """Crude but consistent: create/created/creates -> creat, case/cases -> cas."""
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    return w


_STEMMED_SYNONYMS = {_stem(k): _stem(v) for k, v in _SYNONYMS.items()}


def _tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _STOPWORDS or len(w) < 2:
            continue
        s = _stem(w)
        out.append(_STEMMED_SYNONYMS.get(s, s))
    return out + [f"{a}_{b}" for a, b in zip(out, out[1:])]


class LocalEmbedder:
    name = "local-tfidf"
    thresholds = Thresholds(redundant=0.55, gray=0.30)

    def embed(self, texts: list[str]) -> np.ndarray:
        docs = [Counter(_tokens(t)) for t in texts]
        df = Counter(term for d in docs for term in d)
        vocab = {t: i for i, t in enumerate(sorted(df))}
        n = len(texts)
        m = np.zeros((n, len(vocab)), dtype=np.float32)
        for row, d in enumerate(docs):
            for term, tf in d.items():
                m[row, vocab[term]] = (1 + math.log(tf)) * (math.log((1 + n) / (1 + df[term])) + 1)
        return _normalize(m)


# ----------------------------------------------------------------------- Gemini


KEY_FILE = Path.home() / ".config" / "sprawl-scanner" / "gemini_api_key"


def load_key_file() -> None:
    """Populate GEMINI_API_KEY from the user-only key file if the env doesn't set it."""
    if not os.environ.get("GEMINI_API_KEY") and KEY_FILE.exists():
        os.environ["GEMINI_API_KEY"] = KEY_FILE.read_text().strip()


def gemini_available() -> bool:
    load_key_file()
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                or os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"))


class GeminiEmbedder:
    # Calibrated on the seed inventory (gemini-embedding-001, SEMANTIC_SIMILARITY):
    # known duplicates 0.91-0.99, known overlaps 0.88-0.90, distinct cross-asset <= 0.875.
    # The space is compressed (median pair ~0.78), hence the narrow band.
    thresholds = Thresholds(redundant=0.91, gray=0.88)
    batch_size = 100

    def __init__(self, model: str | None = None, cache_dir: Path | None = None):
        from google import genai  # imported lazily so local mode needs no SDK setup

        load_key_file()
        self.model = model or os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
        self.name = f"gemini:{self.model}"
        self._client = genai.Client()
        cache_dir = cache_dir or Path.home() / ".cache" / "sprawl-scanner"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path = cache_dir / f"embeddings-{self.model}.json"
        self._cache: dict[str, list[float]] = (
            json.loads(self._cache_path.read_text()) if self._cache_path.exists() else {})

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def embed(self, texts: list[str]) -> np.ndarray:
        from google.genai import types

        missing = list(dict.fromkeys(t for t in texts if self._key(t) not in self._cache))
        for i in range(0, len(missing), self.batch_size):
            batch = missing[i: i + self.batch_size]
            # One Content per text: multimodal models (gemini-embedding-2) otherwise treat a
            # list of strings as parts of a single input and return one vector.
            resp = self._client.models.embed_content(
                model=self.model,
                contents=[types.Content(parts=[types.Part(text=t)]) for t in batch],
                config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY"))
            if len(resp.embeddings) != len(batch):
                raise RuntimeError(f"{self.model} returned {len(resp.embeddings)} embeddings "
                                   f"for {len(batch)} texts")
            for text, emb in zip(batch, resp.embeddings):
                self._cache[self._key(text)] = list(emb.values)
        if missing:
            self._cache_path.write_text(json.dumps(self._cache))
        return _normalize(np.array([self._cache[self._key(t)] for t in texts], dtype=np.float32))


def get_embedder(kind: str = "auto"):
    if kind == "local" or (kind == "auto" and not gemini_available()):
        return LocalEmbedder()
    return GeminiEmbedder()
