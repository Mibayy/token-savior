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

    def beam_search_continuations(
        self,
        tool_name: str,
        symbol_name: str = "",
        *,
        beam_width: int = 3,
        max_depth: int = 3,
        min_prob: float = 0.15,
    ) -> list[tuple[list[str], float]]:
        """Return top beams as (path, joint_probability) pairs.

        Starting from *(tool_name, symbol_name)*, expand up to *max_depth*
        transitions ahead, keeping only the top *beam_width* partial paths
        ranked by joint probability. A branch whose next-step probability
        falls below *min_prob* is pruned (dead-end).
        """
        start = self._state(tool_name, symbol_name)
        if start not in self.transitions:
            return []
        # Seed beams with the direct successors.
        beams: list[tuple[list[str], float]] = []
        for nxt, prob in self.predict_next(tool_name, symbol_name, top_k=beam_width):
            if prob >= min_prob:
                beams.append(([nxt], prob))
        if not beams:
            return []
        for _ in range(max_depth - 1):
            expanded: list[tuple[list[str], float]] = []
            grew = False
            for path, joint in beams:
                last = path[-1]
                if ":" in last:
                    nt, ns = last.split(":", 1)
                else:
                    nt, ns = last, ""
                succ = self.predict_next(nt, ns, top_k=beam_width)
                if not succ:
                    expanded.append((path, joint))
                    continue
                kept_any = False
                for nxt, prob in succ:
                    if prob < min_prob:
                        continue
                    expanded.append((path + [nxt], joint * prob))
                    kept_any = True
                    grew = True
                if not kept_any:
                    expanded.append((path, joint))
            expanded.sort(key=lambda x: x[1], reverse=True)
            beams = expanded[:beam_width]
            if not grew:
                break
        return beams

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


# ---------------------------------------------------------------------------
# PPM (Prediction by Partial Matching) — Variable-Order Markov
# ---------------------------------------------------------------------------


class PPMPrefetcher(MarkovPrefetcher):
    """Variable-Order Markov via PPM.

    Uses the longest context whose count table has at least
    ``MIN_OBSERVATIONS`` hits, with an escape probability blending toward the
    order-1 base model for unseen continuations.
    """

    MIN_OBSERVATIONS = 3
    MAX_ORDER = 5
    ESCAPE_PROB = 0.1

    def __init__(self, stats_dir: Path):
        super().__init__(stats_dir)
        self.higher_order: dict[int, dict[tuple, dict[str, int]]] = {
            o: defaultdict(lambda: defaultdict(int))
            for o in range(2, self.MAX_ORDER + 1)
        }
        self._load_higher_order()

    # --- persistence ------------------------------------------------------
    def _higher_order_path(self) -> Path:
        return self.stats_dir / "ppm_higher_order.json"

    def _load_higher_order(self) -> None:
        try:
            data = json.loads(self._higher_order_path().read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        for order_str, contexts in data.items():
            try:
                order = int(order_str)
            except ValueError:
                continue
            if order not in self.higher_order:
                continue
            for ctx_str, nexts in contexts.items():
                ctx = tuple(ctx_str.split("|||")) if ctx_str else tuple()
                self.higher_order[order][ctx] = defaultdict(int, nexts)

    def save_model(self) -> None:
        super().save_model()
        try:
            serialized: dict[str, dict[str, dict[str, int]]] = {}
            for order, contexts in self.higher_order.items():
                bucket: dict[str, dict[str, int]] = {}
                for ctx, nexts in contexts.items():
                    if sum(nexts.values()) >= self.MIN_OBSERVATIONS:
                        bucket["|||".join(ctx)] = dict(nexts)
                if bucket:
                    serialized[str(order)] = bucket
            self._higher_order_path().write_text(json.dumps(serialized))
        except OSError:
            pass

    # --- recording --------------------------------------------------------
    def record_call(self, tool_name: str, symbol_name: str = "") -> None:
        if not tool_name:
            return
        state = self._state(tool_name, symbol_name)
        # call_sequence[-1] is about to become the last element of the
        # preceding context after appending `state` via super().record_call.
        prior_sequence = list(self.call_sequence)
        super().record_call(tool_name, symbol_name)
        # For each order d, the context is the d previous states before `state`.
        for order in range(2, self.MAX_ORDER + 1):
            if len(prior_sequence) >= order:
                context = tuple(prior_sequence[-order:])
                self.higher_order[order][context][state] += 1
        # save_model is already invoked in super().record_call every 10 calls

    # --- prediction -------------------------------------------------------
    def predict_next_ppm(self, top_k: int = 3) -> list[tuple[str, float]]:
        """PPM prediction using the longest sufficiently-observed context.

        Mixes the higher-order distribution with the order-1 fallback using
        ``ESCAPE_PROB``: ``P = (1 - ε) · P_high + ε · P_low``.
        """
        history = self.call_sequence
        best_order = 1
        best_probs: dict[str, float] = {}

        max_order = min(self.MAX_ORDER, len(history))
        for order in range(max_order, 1, -1):
            context = tuple(history[-order:])
            nexts = self.higher_order.get(order, {}).get(context, {})
            total = sum(nexts.values()) if nexts else 0
            if total >= self.MIN_OBSERVATIONS:
                best_order = order
                best_probs = {k: v / total for k, v in nexts.items()}
                break

        # Order-1 fallback distribution conditioned on last state.
        if history:
            last_state = history[-1]
            if ":" in last_state:
                last_tool, last_sym = last_state.split(":", 1)
            else:
                last_tool, last_sym = last_state, ""
            order1_pairs = super().predict_next(last_tool, last_sym, top_k=top_k * 2)
        else:
            order1_pairs = []
        order1_preds = dict(order1_pairs)

        if best_probs and best_order > 1:
            all_states = set(best_probs) | set(order1_preds)
            mixed = {
                s: (1 - self.ESCAPE_PROB) * best_probs.get(s, 0.0)
                + self.ESCAPE_PROB * order1_preds.get(s, 0.0)
                for s in all_states
            }
            result = mixed
            self._last_order_used = best_order
        else:
            result = order1_preds
            self._last_order_used = 1

        ranked = sorted(result.items(), key=lambda x: x[1], reverse=True)
        return [(s, p) for s, p in ranked[:top_k] if p > 0.05]

    def predict_next(
        self, tool_name: str, symbol_name: str = "", top_k: int = 3
    ) -> list[tuple[str, float]]:
        """PPM-first predict_next.

        Uses ``predict_next_ppm()`` when the current end-of-sequence matches
        the requested (tool, symbol) state. Falls back to the base Markov
        behaviour otherwise so downstream code that passes ``tool_name``
        directly (e.g. beam expansion from arbitrary nodes) keeps working.
        """
        requested = self._state(tool_name, symbol_name)
        if self.call_sequence and self.call_sequence[-1] == requested:
            ppm = self.predict_next_ppm(top_k=top_k)
            if ppm:
                return ppm
        return super().predict_next(tool_name, symbol_name, top_k=top_k)

    # --- stats ------------------------------------------------------------
    def get_stats(self) -> dict:
        base = super().get_stats()
        coverage = {}
        for o, contexts in self.higher_order.items():
            coverage[f"order_{o}"] = sum(
                1
                for nexts in contexts.values()
                if sum(nexts.values()) >= self.MIN_OBSERVATIONS
            )
        max_active = max(
            (int(k.split("_")[1]) for k, v in coverage.items() if v > 0),
            default=1,
        )
        base["ppm_coverage"] = coverage
        base["ppm_max_order_active"] = f"order_{max_active}"
        base["ppm_last_order_used"] = getattr(self, "_last_order_used", 1)
        return base
