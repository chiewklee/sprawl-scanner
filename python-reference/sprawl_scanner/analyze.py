"""Similarity analysis: classify every capability as unique, redundant or gray, group
redundant capabilities into clusters, and roll capability overlap up to asset level
to produce consolidation recommendations."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from .embed import Thresholds
from .models import Capability


@dataclass
class Pair:
    a: int
    b: int
    score: float
    kind: str  # "redundant" | "gray"
    verdict: str | None = None  # LLM judge: duplicate | overlap | distinct
    rationale: str | None = None


@dataclass
class Cluster:
    members: list[int]
    canonical: int  # medoid: the member most similar to all the others
    mean_score: float


@dataclass
class AssetOverlap:
    source: str  # asset whose capabilities are covered...
    target: str  # ...by this asset
    redundant: int  # source capabilities with a redundant match in target
    gray: int  # source capabilities whose best match in target is gray
    total: int  # capabilities in source

    @property
    def coverage(self) -> float:
        return self.redundant / self.total if self.total else 0.0


@dataclass
class Analysis:
    capabilities: list[Capability]
    embedder: str
    thresholds: Thresholds
    status: list[str]  # per capability: unique | redundant | gray
    pairs: list[Pair]
    clusters: list[Cluster]
    overlaps: list[AssetOverlap]
    recommendations: list[str] = field(default_factory=list)
    judge_model: str | None = None  # set by judge_gray_pairs; None = gray pairs not judged

    def counts(self) -> dict[str, int]:
        c = {"unique": 0, "redundant": 0, "gray": 0}
        for s in self.status:
            c[s] += 1
        return c

    def assets(self) -> list[str]:
        return sorted({c.asset for c in self.capabilities})


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def similarity(embeddings: np.ndarray) -> np.ndarray:
    """Cosine similarity; embeddings are already L2-normalised."""
    return np.clip(embeddings @ embeddings.T, -1.0, 1.0)


def analyze(caps: list[Capability], embeddings: np.ndarray, embedder_name: str,
            thresholds: Thresholds, cross_asset_only: bool = True,
            consolidate_at: float = 0.5, preferred: set[str] = frozenset()) -> Analysis:
    n = len(caps)
    sim = similarity(embeddings) if n else np.zeros((0, 0))

    pairs: list[Pair] = []
    for i in range(n):
        for j in range(i + 1, n):
            if cross_asset_only and caps[i].asset == caps[j].asset:
                continue
            s = float(sim[i, j])
            if s >= thresholds.redundant and not (caps[i].thin or caps[j].thin):
                pairs.append(Pair(i, j, s, "redundant"))
            elif s >= thresholds.gray:
                pairs.append(Pair(i, j, s, "gray"))
    pairs.sort(key=lambda p: p.score, reverse=True)

    # Average-linkage merge: two clusters join only if their mean cross-similarity still
    # clears the redundant bar. Plain union-find chains A~B~C into one blob even when
    # A and C are unrelated. A redundant pair that can't merge is downgraded to gray.
    uf = _UnionFind(n)
    members_of: dict[int, list[int]] = {i: [i] for i in range(n)}
    for p in pairs:
        if p.kind != "redundant":
            continue
        ra, rb = uf.find(p.a), uf.find(p.b)
        if ra == rb:
            continue
        # Never put two capabilities of the same asset in one cluster: the asset's author
        # split them on purpose (e.g. H360 dispatch vs dispatch_readonly).
        assets_a = {caps[i].asset for i in members_of[ra]}
        if cross_asset_only and any(caps[i].asset in assets_a for i in members_of[rb]):
            p.kind = "gray"
            continue
        if float(sim[np.ix_(members_of[ra], members_of[rb])].mean()) >= thresholds.redundant:
            uf.union(ra, rb)
            members_of[uf.find(ra)] = members_of.pop(ra) + members_of.pop(rb)
        else:
            p.kind = "gray"
    groups = {root: m for root, m in members_of.items()}

    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        sub = sim[np.ix_(members, members)]
        # Preferred (e.g. gateway-governed) assets win; otherwise the medoid.
        bonus = np.array([len(members) if caps[i].asset in preferred else 0 for i in members])
        canonical = members[int(np.argmax(sub.sum(axis=1) + bonus))]
        off_diag = sub[~np.eye(len(members), dtype=bool)]
        clusters.append(Cluster(sorted(members), canonical, float(off_diag.mean())))
    clusters.sort(key=lambda c: (len(c.members), c.mean_score), reverse=True)

    status = ["unique"] * n
    for p in pairs:
        if p.kind == "gray":
            status[p.a] = status[p.b] = "gray"
    for c in clusters:  # redundant wins over gray
        for i in c.members:
            status[i] = "redundant"

    # Thin capabilities can't count as redundant at asset level either.
    thin = np.array([c.thin for c in caps], dtype=bool)
    capped = sim.astype(np.float64)  # float64: a float32 'just below' rounds back up
    if thin.any():
        either = thin[:, None] | thin[None, :]
        capped[either] = np.minimum(capped[either], thresholds.redundant - 1e-6)
    overlaps = _asset_overlaps(caps, capped, thresholds)
    return Analysis(caps, embedder_name, thresholds, status, pairs, clusters, overlaps,
                    _recommendations(caps, clusters, overlaps, consolidate_at,
                                     preferred=preferred))


def _asset_overlaps(caps: list[Capability], sim: np.ndarray,
                    thresholds: Thresholds) -> list[AssetOverlap]:
    by_asset: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(caps):
        by_asset[c.asset].append(i)
    out = []
    for src, s_idx in by_asset.items():
        for tgt, t_idx in by_asset.items():
            if src == tgt:
                continue
            best = sim[np.ix_(s_idx, t_idx)].max(axis=1)
            red = int((best >= thresholds.redundant).sum())
            gray = int(((best >= thresholds.gray) & (best < thresholds.redundant)).sum())
            if red or gray:
                out.append(AssetOverlap(src, tgt, red, gray, len(s_idx)))
    out.sort(key=lambda o: (o.coverage, o.redundant), reverse=True)
    return out


_LAYER = re.compile(r"^(exp|prc|sys|proc|xapi|papi|sapi)[-_ ]", re.I)


def _intentional(a: str, b: str, kind: dict[str, str]) -> str | None:
    """Overlap that is expected by design: say so instead of recommending retirement."""
    sa, sb = _LAYER.sub("", a.lower()), _LAYER.sub("", b.lower())
    if sa == sb and (_LAYER.match(a) or _LAYER.match(b)):
        return (f"'{a}' and '{b}' look like API-led layers of the same service; overlap is "
                f"expected. Check that each layer adds value rather than passing straight through.")
    if {kind[a], kind[b]} == {"agent", "mcp"}:
        agent, server = (a, b) if kind[a] == "agent" else (b, a)
        return (f"'{agent}' (agent) has skills matching '{server}' (mcp) tools: likely the agent "
                f"calling that server. Fine if so; sprawl if the agent re-implements the tools.")
    if {kind[a], kind[b]} in ({"mcp", "api"}, {"agent", "api"}):
        facade, api = (a, b) if kind[a] != "api" else (b, a)
        return (f"'{facade}' ({kind[facade]}) mirrors '{api}' (api): likely a facade over it. "
                f"Fine if it calls '{api}'; sprawl if it re-implements the backend call.")
    return None


def _recommendations(caps: list[Capability], clusters: list[Cluster],
                     overlaps: list[AssetOverlap], consolidate_at: float,
                     near_duplicate_at: float = 0.8,
                     preferred: set[str] = frozenset()) -> list[str]:
    recs, seen = [], set()
    size = defaultdict(int)
    for c in caps:
        size[c.asset] += 1
    cov = {(o.source, o.target): o for o in overlaps}
    kind = {c.asset: c.asset_type for c in caps}

    # overlaps is sorted by coverage desc, so the first direction seen for a pair is
    # the more-covered asset: that's the one to fold into the other.
    folded: set[str] = set()  # one fold recommendation per source: its best target
    families: dict[str, set[str]] = {}  # near-duplicate assets, grouped transitively
    for o in overlaps:
        if (o.coverage < consolidate_at or frozenset((o.source, o.target)) in seen
                or o.source in folded):
            continue
        seen.add(frozenset((o.source, o.target)))
        back = cov.get((o.target, o.source))
        if note := _intentional(o.source, o.target, kind):
            folded.add(o.source)
            recs.append(note)
        elif back and back.coverage >= near_duplicate_at and o.coverage >= near_duplicate_at:
            group = families.get(o.source, {o.source}) | families.get(o.target, {o.target})
            for a in group:
                families[a] = group
        elif o.source in families:
            continue  # already covered by its near-duplicate family
        elif o.source in preferred and o.target not in preferred:  # never retire a preferred asset
            folded.add(o.source)
            recs.append(f"'{o.target}' duplicates {o.redundant}/{o.total} capabilities of "
                        f"preferred '{o.source}'. Route those callers through '{o.source}'.")
        else:
            folded.add(o.source)
            recs.append(f"Retire or fold '{o.source}' into '{o.target}': {o.redundant}/{o.total} "
                        f"of its capabilities already exist there.")

    family_recs = []
    for group in {frozenset(g) for g in families.values()}:
        keep = sorted(group, key=lambda a: (a not in preferred, -size[a], a))[0]
        others = sorted(group - {keep})
        family_recs.append(
            f"{len(group)} near-duplicate assets: '{keep}' and {', '.join(repr(a) for a in others)} "
            f"expose the same capabilities. Consolidate into '{keep}'.")
    recs = sorted(family_recs, key=lambda r: -int(r.split()[0])) + recs

    covered = [g for g in {frozenset(g) for g in families.values()}]
    for c in clusters:
        assets = {caps[i].asset for i in c.members}
        if len(assets) < 3:
            continue
        if len({_LAYER.sub("", a.lower()) for a in assets}) == 1:
            continue  # API-led layers of one service: reported above as expected
        if any(assets <= g for g in covered):
            continue  # already reported as a near-duplicate family
        canon = caps[c.canonical]
        recs.append(f"'{canon.name}' is implemented {len(c.members)} times across "
                    f"{len(assets)} assets ({', '.join(sorted(assets))}). Standardise on "
                    f"'{canon.asset}' and reuse it.")
    return recs


def find_similar(caps: list[Capability], query_embedding: np.ndarray,
                 cap_embeddings: np.ndarray, top_k: int = 5) -> list[tuple[Capability, float]]:
    scores = cap_embeddings @ query_embedding
    order = np.argsort(-scores)[:top_k]
    return [(caps[i], float(scores[i])) for i in order]
