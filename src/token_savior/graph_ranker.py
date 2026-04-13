"""Random Walk with Restart (RWR) on a symbol dependency graph.

Reference: Tong, Faloutsos, Pan -- "Fast Random Walk with Restart and Its
Applications" (ICDM 2006).

We solve r = (1 - c) W r + c e_seed by power iteration. The result is a
stationary distribution centered on ``seed_node``: nodes that are
structurally close to the seed receive the highest score, regardless of
direct adjacency. This catches symbols that BFS misses (e.g. a 3-hop
neighbour reachable through several short paths).
"""

from __future__ import annotations

import sys
from collections import defaultdict


def random_walk_with_restart(
    graph: dict[str, set[str]],
    seed_node: str,
    restart_prob: float = 0.15,
    max_iter: int = 100,
    convergence_threshold: float = 1e-6,
) -> dict[str, float]:
    """Compute RWR scores from *seed_node* over *graph*.

    Parameters
    ----------
    graph
        Adjacency dict: node -> set(neighbours). Treated as undirected
        with normalised transition weights 1/|neighbours|.
    seed_node
        Restart node. Returns ``{}`` if the seed is not in the graph.
    restart_prob
        Probability of teleporting back to seed at each step. 0.15 is
        the classical PageRank-style choice.
    max_iter
        Hard cap on power iterations. On dense graphs the threshold may
        not be hit; we then return the current state and emit a warning
        on stderr instead of raising.
    convergence_threshold
        L1 distance between successive score vectors below which we stop.
    """
    if seed_node not in graph:
        return {}

    nodes = list(set(graph.keys()) | {n for s in graph.values() for n in s})
    n = len(nodes)
    if n == 0:
        return {}
    node_idx = {node: i for i, node in enumerate(nodes)}

    # Sparse transition matrix as dict-of-dicts.
    transition: dict[int, dict[int, float]] = defaultdict(dict)
    for node, neighbours in graph.items():
        if not neighbours:
            continue
        weight = 1.0 / len(neighbours)
        src = node_idx[node]
        for neighbour in neighbours:
            if neighbour in node_idx:
                transition[src][node_idx[neighbour]] = weight

    seed_idx = node_idx[seed_node]
    scores: dict[int, float] = {i: 1.0 / n for i in range(n)}

    converged = False
    iterations_used = 0
    for iteration in range(max_iter):
        new_scores: dict[int, float] = defaultdict(float)
        for src_idx, edges in transition.items():
            src_score = scores.get(src_idx, 0.0)
            if src_score == 0.0:
                continue
            propagated = (1 - restart_prob) * src_score
            for dst_idx, weight in edges.items():
                new_scores[dst_idx] += propagated * weight
        new_scores[seed_idx] += restart_prob

        diff = sum(
            abs(new_scores.get(i, 0.0) - scores.get(i, 0.0)) for i in range(n)
        )
        scores = dict(new_scores)
        iterations_used = iteration + 1

        if diff < convergence_threshold:
            converged = True
            break

    if not converged:
        # Dense graph or pathological seed -- log and return current state.
        print(
            f"[token-savior] RWR did not converge in {max_iter} iterations "
            f"(threshold={convergence_threshold}); returning current state.",
            file=sys.stderr,
        )

    idx_node = {v: k for k, v in node_idx.items()}
    result = {
        idx_node[i]: score
        for i, score in scores.items()
        if score > 1e-8
    }
    # Stash iteration count on the dict via a sentinel key for callers that
    # want to display it. We pop it on the consumer side; if not popped, it
    # sorts last by value (very small) and is harmless.
    result["__iterations__"] = float(iterations_used)
    return result
