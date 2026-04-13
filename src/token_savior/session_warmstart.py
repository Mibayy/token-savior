"""Cross-session warm start — pre-load patterns from similar past sessions.

A session is summarized as a 32-dim signature vector:

  - dims 0..9   : top-10 tools, fraction of session calls each one represents
  - dims 10..13 : duration / 60 min, turns / 100, obs_accessed / 20, symbols / 30
  - dims 14..18 : mode one-hot (code/debug/review/infra/silent)
  - dims 19..31 : 13-bucket hash projection of touched symbols

At session start we compute an embryonic signature (mode + project), look up
the top-K most similar historical signatures by cosine, and pre-warm caches
based on the tools/symbols those sessions actually used. Persisted to
``session_signatures.json`` (capped at 200, oldest evicted FIFO).
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


SIGNATURE_DIM = 32

TOP_TOOLS = (
    "get_function_source",
    "get_class_source",
    "find_symbol",
    "search_codebase",
    "get_dependents",
    "run_impacted_tests",
    "replace_symbol_source",
    "memory_search",
    "get_backward_slice",
    "pack_context",
)
_TOOL_INDEX = {t: i for i, t in enumerate(TOP_TOOLS)}

_MODES = ("code", "debug", "review", "infra", "silent")
_MODE_INDEX = {m: i for i, m in enumerate(_MODES)}

_SYMBOL_BUCKETS = 13  # dims 19..31


def _bucket(symbol: str) -> int:
    h = int(hashlib.md5(symbol.encode("utf-8")).hexdigest()[:8], 16)
    return h % _SYMBOL_BUCKETS


def compute_signature(session_data: dict) -> list[float]:
    """Compute the 32-dim signature for a session.

    ``session_data`` keys:
      - tool_counts: dict[str,int]
      - duration_min: float
      - turns: int
      - obs_accessed: int
      - symbols: iterable[str]
      - mode: str
    """
    sig = [0.0] * SIGNATURE_DIM

    # Dims 0..9 — tool fractions of total calls
    tool_counts = session_data.get("tool_counts") or {}
    total_calls = sum(tool_counts.values()) or 1
    for tool, idx in _TOOL_INDEX.items():
        sig[idx] = tool_counts.get(tool, 0) / total_calls

    # Dims 10..13 — normalized scalar metrics, clamped to [0, 1]
    sig[10] = min(1.0, float(session_data.get("duration_min", 0)) / 60.0)
    sig[11] = min(1.0, float(session_data.get("turns", 0)) / 100.0)
    sig[12] = min(1.0, float(session_data.get("obs_accessed", 0)) / 20.0)
    symbols = list(session_data.get("symbols") or [])
    sig[13] = min(1.0, len(symbols) / 30.0)

    # Dims 14..18 — mode one-hot
    mode = (session_data.get("mode") or "").lower()
    midx = _MODE_INDEX.get(mode)
    if midx is not None:
        sig[14 + midx] = 1.0

    # Dims 19..31 — 13-bucket hash projection of touched symbols
    if symbols:
        bucket_counts = [0] * _SYMBOL_BUCKETS
        for s in symbols:
            if s:
                bucket_counts[_bucket(s)] += 1
        m = max(bucket_counts) or 1
        for i, c in enumerate(bucket_counts):
            sig[19 + i] = c / m
    return sig


def _cosine(u: list[float], v: list[float]) -> float:
    nu = math.sqrt(sum(a * a for a in u))
    nv = math.sqrt(sum(b * b for b in v))
    if nu == 0 or nv == 0:
        return 0.0
    dot = sum(a * b for a, b in zip(u, v))
    return dot / (nu * nv)


class SessionWarmStart:
    SIGNATURE_DIM = SIGNATURE_DIM
    MAX_HISTORY = 200

    def __init__(self, stats_dir: Path):
        self.stats_dir = Path(stats_dir)
        # Each entry: {session_id, project_root, signature, tool_counts, symbols, ts}
        self.history: list[dict] = []
        self._load()

    # --- persistence ------------------------------------------------------
    def _path(self) -> Path:
        return self.stats_dir / "session_signatures.json"

    def _load(self) -> None:
        try:
            data = json.loads(self._path().read_text())
            if isinstance(data, list):
                self.history = data[-self.MAX_HISTORY:]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return

    def _save(self) -> None:
        try:
            self.stats_dir.mkdir(parents=True, exist_ok=True)
            self._path().write_text(json.dumps(self.history[-self.MAX_HISTORY:]))
        except OSError:
            pass

    # --- save / lookup ----------------------------------------------------
    def save_session_signature(
        self,
        session_id: str | int,
        project_root: str,
        session_data: dict,
    ) -> list[float]:
        sig = compute_signature(session_data)
        entry = {
            "session_id": str(session_id),
            "project_root": project_root or "",
            "signature": sig,
            "tool_counts": dict(session_data.get("tool_counts") or {}),
            "symbols": list(session_data.get("symbols") or [])[:50],
            "mode": session_data.get("mode") or "",
        }
        self.history.append(entry)
        if len(self.history) > self.MAX_HISTORY:
            self.history = self.history[-self.MAX_HISTORY:]
        self._save()
        return sig

    def find_similar_sessions(
        self,
        sig: list[float],
        project_root: str | None = None,
        top_k: int = 3,
        min_sim: float = 0.5,
    ) -> list[tuple[dict, float]]:
        scored: list[tuple[dict, float]] = []
        for entry in self.history:
            if project_root and entry.get("project_root") and entry["project_root"] != project_root:
                continue
            s = _cosine(sig, entry.get("signature") or [])
            if s >= min_sim:
                scored.append((entry, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    # --- stats ------------------------------------------------------------
    def get_stats(self) -> dict:
        if not self.history:
            return {
                "signatures": 0,
                "by_project": {},
                "avg_pairwise_similarity": 0.0,
            }
        by_project: dict[str, int] = {}
        for e in self.history:
            p = e.get("project_root") or "(none)"
            by_project[p] = by_project.get(p, 0) + 1
        # Average pairwise similarity in a small sample (cap at 30 for cost).
        sample = self.history[-30:]
        sims: list[float] = []
        for i in range(len(sample)):
            for j in range(i + 1, len(sample)):
                sims.append(_cosine(sample[i]["signature"], sample[j]["signature"]))
        avg = sum(sims) / len(sims) if sims else 0.0
        return {
            "signatures": len(self.history),
            "by_project": by_project,
            "avg_pairwise_similarity": round(avg, 4),
        }
