"""LinUCB contextual bandit for observation injection ranking.

LinUCB (Li et al. 2010) models expected reward as a linear function of
features ``φ(obs, context)``:

    r̂ = θᵀ · φ

with exploration bonus ``α · √(φᵀ · A⁻¹ · φ)`` giving the UCB score. Online
updates: ``A ← A + φφᵀ``, ``b ← b + r · φ``.  Convergence O(√T log T).

The model persists A (10×10) and b (10) to ``linucb_model.json`` under the
stats dir so weights survive restarts. No numpy dependency — all linear
algebra is pure Python (Gauss–Jordan inverse, 10-dim is trivially fast).
"""

from __future__ import annotations

import json
import re
from pathlib import Path


FEATURE_NAMES = (
    "type_score",
    "age_score",
    "access_score",
    "semantic_sim",
    "mode_match",
    "tokens_used_pct",
    "task_is_edit",
    "task_is_debug",
    "symbol_match",
    "has_context",
)

_TYPE_SCORES = {
    "guardrail": 1.0,
    "ruled_out": 0.95,
    "convention": 0.9,
    "warning": 0.85,
    "decision": 0.8,
    "error_pattern": 0.75,
    "bugfix": 0.7,
    "infra": 0.6,
    "config": 0.55,
    "command": 0.5,
    "research": 0.35,
    "note": 0.2,
    "idea": 0.15,
}

_EDIT_TOOLS = frozenset({
    "replace_symbol_source",
    "insert_near_symbol",
    "apply_symbol_change_and_validate",
})
_DEBUG_TOOLS = frozenset({
    "run_impacted_tests",
    "find_dead_code",
    "get_backward_slice",
    "get_change_impact",
    "detect_breaking_changes",
})

_WORD_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b")


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Tiny matrix ops (10×10 max) — pure Python.
# ---------------------------------------------------------------------------

def _eye(n: int) -> list[list[float]]:
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def _mat_vec(M: list[list[float]], v: list[float]) -> list[float]:
    return [sum(M[i][k] * v[k] for k in range(len(v))) for i in range(len(M))]


def _dot(u: list[float], v: list[float]) -> float:
    return sum(a * b for a, b in zip(u, v))


def _inverse(M: list[list[float]]) -> list[list[float]]:
    """Gauss–Jordan inverse with partial pivoting. Returns identity on failure."""
    n = len(M)
    A = [row[:] + e for row, e in zip(M, _eye(n))]
    for i in range(n):
        # Pivot
        pivot_row = max(range(i, n), key=lambda r: abs(A[r][i]))
        if abs(A[pivot_row][i]) < 1e-12:
            return _eye(n)  # singular — return identity as safe fallback
        A[i], A[pivot_row] = A[pivot_row], A[i]
        pivot = A[i][i]
        A[i] = [v / pivot for v in A[i]]
        for k in range(n):
            if k == i:
                continue
            factor = A[k][i]
            if factor == 0:
                continue
            A[k] = [a - factor * b for a, b in zip(A[k], A[i])]
    return [row[n:] for row in A]


# ---------------------------------------------------------------------------

class LinUCBInjector:
    FEATURE_DIM = 10
    ALPHA = 1.0

    def __init__(self, stats_dir: Path):
        self.stats_dir = Path(stats_dir)
        # A starts as identity (ridge prior), b starts at zero
        self.A: list[list[float]] = _eye(self.FEATURE_DIM)
        self.b: list[float] = [0.0] * self.FEATURE_DIM
        self.updates = 0
        self.scored = 0
        self._load()

    # --- persistence ------------------------------------------------------
    def _path(self) -> Path:
        return self.stats_dir / "linucb_model.json"

    def _load(self) -> None:
        try:
            data = json.loads(self._path().read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        A = data.get("A")
        b = data.get("b")
        if (
            isinstance(A, list) and len(A) == self.FEATURE_DIM
            and all(isinstance(r, list) and len(r) == self.FEATURE_DIM for r in A)
            and isinstance(b, list) and len(b) == self.FEATURE_DIM
        ):
            self.A = [[float(x) for x in row] for row in A]
            self.b = [float(x) for x in b]
        self.updates = int(data.get("updates", 0))
        self.scored = int(data.get("scored", 0))

    def save(self) -> None:
        try:
            self.stats_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "A": self.A,
                "b": self.b,
                "updates": self.updates,
                "scored": self.scored,
            }
            self._path().write_text(json.dumps(payload))
        except OSError:
            pass

    # --- features ---------------------------------------------------------
    def extract_features(self, obs: dict, context: dict) -> list[float]:
        ot = obs.get("type") or "note"
        phi: list[float] = [0.0] * self.FEATURE_DIM

        # 0: type_score
        phi[0] = _TYPE_SCORES.get(ot, 0.3)

        # 1: age_score — 1/(1 + age_days/30)
        now_epoch = int(context.get("now_epoch") or 0)
        created = obs.get("created_at_epoch") or obs.get("last_accessed_epoch") or now_epoch
        age_days = max(0.0, (now_epoch - created) / 86400.0) if now_epoch else 0.0
        phi[1] = 1.0 / (1.0 + age_days / 30.0)

        # 2: access_score
        ac = int(obs.get("access_count") or 0)
        phi[2] = min(ac / 10.0, 1.0)

        # 3: semantic_sim — Jaccard(prompt, obs.title + obs.content[:100])
        prompt = context.get("prompt") or ""
        sig = (obs.get("title") or "") + " " + (obs.get("content") or "")[:100]
        phi[3] = _jaccard(_tokens(prompt), _tokens(sig))

        # 4: mode_match
        auto_types = context.get("auto_capture_types") or frozenset()
        phi[4] = 1.0 if ot in auto_types else 0.0

        # 5: tokens_used_pct
        phi[5] = max(0.0, min(1.0, float(context.get("tokens_used_pct") or 0.0)))

        # 6: task_is_edit
        last_tool = context.get("last_tool") or ""
        phi[6] = 1.0 if last_tool in _EDIT_TOOLS else 0.0

        # 7: task_is_debug
        phi[7] = 1.0 if last_tool in _DEBUG_TOOLS else 0.0

        # 8: symbol_match
        recent_symbols = context.get("recent_symbols") or ()
        obs_sym = obs.get("symbol") or ""
        phi[8] = 1.0 if obs_sym and obs_sym in recent_symbols else 0.0

        # 9: has_context
        phi[9] = 1.0 if obs.get("context") else 0.0

        return phi

    # --- scoring ----------------------------------------------------------
    def _theta_and_Ainv(self) -> tuple[list[float], list[list[float]]]:
        Ainv = _inverse(self.A)
        theta = _mat_vec(Ainv, self.b)
        return theta, Ainv

    def score_observation(self, obs: dict, context: dict) -> float:
        phi = self.extract_features(obs, context)
        theta, Ainv = self._theta_and_Ainv()
        exploitation = _dot(theta, phi)
        # variance = φᵀ · A⁻¹ · φ, clamped positive
        var = max(0.0, _dot(phi, _mat_vec(Ainv, phi)))
        exploration = self.ALPHA * (var ** 0.5)
        self.scored += 1
        return exploitation + exploration

    def rank_observations(
        self, obs_list: list[dict], context: dict, top_k: int = 10,
    ) -> list[tuple[dict, float]]:
        if not obs_list:
            return []
        theta, Ainv = self._theta_and_Ainv()
        scored: list[tuple[dict, float]] = []
        for obs in obs_list:
            phi = self.extract_features(obs, context)
            exploit = _dot(theta, phi)
            var = max(0.0, _dot(phi, _mat_vec(Ainv, phi)))
            scored.append((obs, exploit + self.ALPHA * (var ** 0.5)))
        self.scored += len(obs_list)
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    # --- update -----------------------------------------------------------
    def update(self, obs: dict, context: dict, reward: float) -> None:
        """Online update: A ← A + φφᵀ, b ← b + r·φ."""
        phi = self.extract_features(obs, context)
        for i in range(self.FEATURE_DIM):
            for j in range(self.FEATURE_DIM):
                self.A[i][j] += phi[i] * phi[j]
            self.b[i] += reward * phi[i]
        self.updates += 1
        if self.updates % 5 == 0:
            self.save()

    # --- stats ------------------------------------------------------------
    def get_stats(self) -> dict:
        theta, _ = self._theta_and_Ainv()
        ranked = sorted(
            ((name, theta[i]) for i, name in enumerate(FEATURE_NAMES)),
            key=lambda x: -abs(x[1]),
        )
        return {
            "feature_weights": [
                {"name": n, "weight": round(w, 4)} for n, w in ranked
            ],
            "top_feature": ranked[0][0] if ranked else "n/a",
            "top_weight": round(ranked[0][1], 4) if ranked else 0.0,
            "updates": self.updates,
            "scored": self.scored,
        }
