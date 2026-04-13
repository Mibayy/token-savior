"""Tenseur de Co-Activation — PMI-scored symbol co-activation model.

We record which symbols are touched within a single session and, on session
flush, increment pairwise co-activation counts. At query time we return the
top-k co-actives of a seed symbol, scored by normalized Pointwise Mutual
Information (NPMI ∈ [-1, 1]). A high NPMI means the pair co-activates far
more often than chance, which is what we want for pre-injection hints.

State is JSON-persisted under the Token Savior stats dir.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path


class TCAEngine:
    """Co-activation tensor with PMI scoring and on-disk persistence."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.coactivation_path = self.data_dir / "tca_coactivation.json"

        # coactivation[a][b] = # sessions where both a and b were touched
        self.coactivation: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self.activation_counts: dict[str, int] = defaultdict(int)
        self.session_count: int = 0
        # Session-local: unique symbols activated in the current session
        self.session_activations: list[str] = []
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.coactivation_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        self.coactivation = defaultdict(
            lambda: defaultdict(int),
            {
                k: defaultdict(int, v)
                for k, v in (data.get("coactivation") or {}).items()
            },
        )
        self.activation_counts = defaultdict(int, data.get("counts") or {})
        self.session_count = int(data.get("session_count", 0))

    def save(self) -> None:
        try:
            payload = {
                "coactivation": {k: dict(v) for k, v in self.coactivation.items()},
                "counts": dict(self.activation_counts),
                "session_count": self.session_count,
            }
            self.coactivation_path.write_text(json.dumps(payload))
        except OSError:
            pass  # disk-full / permission failures must not crash tool calls

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def record_activation(self, symbol: str) -> None:
        """Register a symbol touched this session (first-time within session)."""
        if not symbol:
            return
        if symbol in self.session_activations:
            return
        self.session_activations.append(symbol)
        self.activation_counts[symbol] += 1

    def flush_session(self) -> int:
        """Fold the current session into the co-activation tensor.

        Returns the number of symbol pairs updated.
        """
        syms = self.session_activations
        updated = 0
        for i in range(len(syms)):
            for j in range(i + 1, len(syms)):
                a, b = syms[i], syms[j]
                self.coactivation[a][b] += 1
                self.coactivation[b][a] += 1
                updated += 1
        if syms:
            self.session_count += 1
        self.session_activations = []
        self.save()
        return updated

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def get_coactive_symbols(
        self,
        seed_symbol: str,
        top_k: int = 5,
        min_coactivation: int = 2,
    ) -> list[tuple[str, float]]:
        """Top-*k* co-actives of *seed_symbol*, ranked by NPMI.

        Pairs with co-activation count below *min_coactivation* are dropped to
        avoid one-off noise. Returns list of ``(symbol, npmi)``.
        """
        if seed_symbol not in self.coactivation:
            return []
        N = sum(self.activation_counts.values()) or 1
        p_seed = self.activation_counts.get(seed_symbol, 1) / N
        results: list[tuple[str, float]] = []
        for other_sym, coact in self.coactivation[seed_symbol].items():
            if coact < min_coactivation:
                continue
            p_other = self.activation_counts.get(other_sym, 1) / N
            p_joint = coact / N
            try:
                denom = p_seed * p_other
                if denom <= 0 or p_joint <= 0:
                    continue
                pmi = math.log(p_joint / denom)
                # NPMI ∈ [-1, 1]; -log(p_joint) > 0 since p_joint < 1
                npmi = pmi / (-math.log(p_joint)) if p_joint < 1 else pmi
            except (ValueError, ZeroDivisionError):
                continue
            results.append((other_sym, npmi))
        return sorted(results, key=lambda x: x[1], reverse=True)[:top_k]

    def get_stats(self) -> dict:
        """Summarise the co-activation tensor."""
        total_symbols = len(self.coactivation)
        total_pairs = sum(len(v) for v in self.coactivation.values()) // 2
        all_pairs: list[tuple[str, str, int]] = []
        for a, others in self.coactivation.items():
            for b, count in others.items():
                if a < b:
                    all_pairs.append((a, b, count))
        top_pairs = sorted(all_pairs, key=lambda x: x[2], reverse=True)[:3]
        return {
            "symbols_tracked": total_symbols,
            "co_activation_pairs": total_pairs,
            "session_activations": len(self.session_activations),
            "sessions_flushed": self.session_count,
            "top_pairs": top_pairs,
        }
