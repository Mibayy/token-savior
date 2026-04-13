"""First-order Markov model on tool-call sequences.

After ``get_function_source(X)``, the next call is ``get_dependents(X)`` ~70%
of the time. We learn these transitions per session and persist them to disk
so future sessions can pre-warm the most likely next response.

State = ``"tool_name:symbol_name"`` (or just ``"tool_name"`` for symbol-less
tools). The transition table is a sparse dict-of-Counters.

Threading: callers should warm the cache from a daemon=True thread so that
any in-flight prefetch never blocks process shutdown. See ``server.py`` for
the integration.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


class MarkovPrefetcher:
    """First-order Markov model with disk persistence."""

    def __init__(self, stats_dir: Path):
        self.stats_dir = Path(stats_dir)
        self.transitions: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self.call_sequence: list[str] = []
        self._load_model()

    def _model_path(self) -> Path:
        return self.stats_dir / "markov_model.json"

    def _load_model(self) -> None:
        try:
            data = json.loads(self._model_path().read_text())
            self.transitions = defaultdict(
                lambda: defaultdict(int),
                {k: defaultdict(int, v) for k, v in data.items()},
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass  # empty model on first run

    def save_model(self) -> None:
        try:
            self.stats_dir.mkdir(parents=True, exist_ok=True)
            payload = {k: dict(v) for k, v in self.transitions.items()}
            self._model_path().write_text(json.dumps(payload))
        except OSError:
            pass  # disk-full / permission errors must never crash a tool call

    @staticmethod
    def _state(tool_name: str, symbol_name: str = "") -> str:
        return f"{tool_name}:{symbol_name}" if symbol_name else tool_name

    def record_call(self, tool_name: str, symbol_name: str = "") -> None:
        """Append (tool, symbol) to the session sequence and update transitions."""
        if not tool_name:
            return
        state = self._state(tool_name, symbol_name)
        if self.call_sequence:
            prev = self.call_sequence[-1]
            self.transitions[prev][state] += 1
        self.call_sequence.append(state)
        if len(self.call_sequence) % 10 == 0:
            self.save_model()

    def predict_next(
        self, tool_name: str, symbol_name: str = "", top_k: int = 3
    ) -> list[tuple[str, float]]:
        """Return up to *top_k* (next_state, probability) pairs."""
        state = self._state(tool_name, symbol_name)
        transitions = self.transitions.get(state, {})
        if not transitions:
            return []
        total = sum(transitions.values())
        ranked = sorted(
            ((nxt, count / total) for nxt, count in transitions.items()),
            key=lambda x: x[1],
            reverse=True,
        )
        return ranked[:top_k]

    def get_stats(self) -> dict:
        total_states = len(self.transitions)
        total_transitions = sum(sum(v.values()) for v in self.transitions.values())
        return {
            "states": total_states,
            "transitions": total_transitions,
            "top_sequence": self._top_sequence(),
        }

    def _top_sequence(self) -> str:
        best_state = ""
        best_next = ""
        best_count = 0
        for state, nexts in self.transitions.items():
            for next_state, count in nexts.items():
                if count > best_count:
                    best_state, best_next, best_count = state, next_state, count
        if best_count == 0:
            return "none"
        return f"{best_state} -> {best_next} ({best_count}x)"
