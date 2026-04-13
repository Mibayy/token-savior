"""MDL memory distillation — crystallize similar observations.

Principle (Rissanen, 1978):

    min  Σ_j [ L(a_j) + Σ_{o ∈ C_j} L(o | a_j) ]

where ``L(·)`` is description length. When the code length of an abstraction
plus the deltas of its cluster members beats the sum of individual lengths,
the cluster is replaced by ``abstraction + deltas``.

Pipeline:
1. Group observations by ``type``.
2. Agglomerative clustering via Jaccard similarity on ``title + content[:100]``.
3. For each cluster ≥ ``min_cluster_size`` : propose abstraction, compute
   MDL before/after, keep candidates with compression ≥ threshold.
4. Caller persists (or previews) the chosen clusters.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field


_WORD_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def description_length(text: str) -> float:
    """Approximate token count (~4 chars/token)."""
    return len(text or "") / 4.0


def _tokenize(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def compute_shared_tokens(texts: list[str], min_freq: float = 0.7) -> list[str]:
    """Tokens present in at least ``min_freq`` fraction of texts."""
    if not texts:
        return []
    threshold = max(1, int(min_freq * len(texts)))
    counter: Counter = Counter()
    for t in texts:
        counter.update(set(_tokenize(t)))
    kept = [
        (tok, cnt) for tok, cnt in counter.items()
        if cnt >= threshold and len(tok) > 2
    ]
    kept.sort(key=lambda x: (-x[1], x[0]))
    return [tok for tok, _ in kept[:20]]


def propose_abstraction(
    obs_contents: list[str],
    shared_tokens: list[str],
    obs_types: list[str],
) -> str:
    """Compose a compact abstraction header + representative sentence."""
    if not obs_contents:
        return ""
    dominant_type = Counter(obs_types).most_common(1)[0][0] if obs_types else "note"
    shared_set = set(shared_tokens)

    best_sentence = ""
    best_overlap = -1
    for content in obs_contents:
        for sent in _SENT_SPLIT.split(content or ""):
            sent = sent.strip()
            if len(sent) < 10:
                continue
            toks = set(_tokenize(sent))
            overlap = len(toks & shared_set)
            if overlap > best_overlap:
                best_overlap = overlap
                best_sentence = sent
    if not best_sentence and obs_contents:
        best_sentence = (obs_contents[0] or "")[:150].strip()

    core = "+".join(shared_tokens[:5]) if shared_tokens else "n/a"
    # Compact single-line header keeps L(abstraction) low so MDL can win.
    return f"[MDL:{dominant_type}] {core} — {best_sentence[:120]}"


def delta_encode(content: str, abstraction: str) -> str:
    """Encode ``content`` as a delta against ``abstraction``.

    Return only the tokens that are NOT already in the abstraction, joined by
    spaces and capped at 3 sentences' worth of novel words. This gives MDL a
    real compression shot: shared tokens are stored once in the abstraction,
    and each observation only records what's unique.
    """
    if not content:
        return ""
    abstr_tokens = set(_tokenize(abstraction))
    novel_fragments: list[str] = []
    for sent in _SENT_SPLIT.split(content):
        sent = sent.strip()
        if not sent:
            continue
        toks = _tokenize(sent)
        if not toks:
            continue
        unique = [t for t in toks if t not in abstr_tokens]
        if len(unique) >= 2:  # ignore fully-covered sentences
            novel_fragments.append(" ".join(unique))
        if len(novel_fragments) >= 3:
            break
    if not novel_fragments:
        return ""  # fully covered by abstraction — maximal compression
    return " | ".join(novel_fragments)[:200]


@dataclass
class DistillationCluster:
    obs_ids: list[int]
    obs_contents: list[str]
    obs_titles: list[str] = field(default_factory=list)
    obs_types: list[str] = field(default_factory=list)
    proposed_abstraction: str = ""
    mdl_before: float = 0.0
    mdl_after: float = 0.0
    compression_ratio: float = 0.0
    shared_tokens: list[str] = field(default_factory=list)
    deltas: list[str] = field(default_factory=list)
    dominant_type: str = "note"


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _agglomerative_cluster(
    obs: list[dict],
    jaccard_threshold: float,
) -> list[list[int]]:
    """Single-link agglomerative clustering on token-sets of title+content[:100]."""
    if not obs:
        return []
    sigs = [set(_tokenize((o.get("title") or "") + " " + (o.get("content") or "")[:100]))
            for o in obs]
    n = len(obs)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        for j in range(i + 1, n):
            if _jaccard(sigs[i], sigs[j]) >= jaccard_threshold:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def find_distillation_candidates(
    observations: list[dict],
    jaccard_threshold: float = 0.4,
    min_cluster_size: int = 3,
    compression_required: float = 0.2,
) -> list[DistillationCluster]:
    """Detect clusters whose MDL-compression ratio exceeds threshold."""
    # Group by type first
    by_type: dict[str, list[dict]] = {}
    for o in observations:
        by_type.setdefault(o.get("type") or "note", []).append(o)

    results: list[DistillationCluster] = []
    for obs_type, bucket in by_type.items():
        if len(bucket) < min_cluster_size:
            continue
        groups = _agglomerative_cluster(bucket, jaccard_threshold)
        for group_idx in groups:
            if len(group_idx) < min_cluster_size:
                continue
            members = [bucket[i] for i in group_idx]
            ids = [m["id"] for m in members if "id" in m]
            titles = [m.get("title", "") or "" for m in members]
            contents = [m.get("content", "") or "" for m in members]
            types = [m.get("type", obs_type) or obs_type for m in members]

            shared = compute_shared_tokens(
                [t + "\n" + c for t, c in zip(titles, contents)],
                min_freq=0.7,
            )
            abstraction = propose_abstraction(contents, shared, types)
            deltas = [delta_encode(c, abstraction) for c in contents]

            # MDL before = Σ L(title + content)
            mdl_before = sum(description_length(t + "\n" + c)
                             for t, c in zip(titles, contents))
            # MDL after = L(abstraction) + Σ L(delta)
            mdl_after = description_length(abstraction) + sum(
                description_length(d) for d in deltas
            )
            if mdl_before <= 0:
                continue
            compression = (mdl_before - mdl_after) / mdl_before
            if compression < compression_required:
                continue

            results.append(DistillationCluster(
                obs_ids=ids,
                obs_contents=contents,
                obs_titles=titles,
                obs_types=types,
                proposed_abstraction=abstraction,
                mdl_before=round(mdl_before, 2),
                mdl_after=round(mdl_after, 2),
                compression_ratio=round(compression, 4),
                shared_tokens=shared,
                deltas=deltas,
                dominant_type=Counter(types).most_common(1)[0][0],
            ))

    results.sort(key=lambda c: -c.compression_ratio)
    return results
