"""Louvain-style community detection for the symbol dependency graph.

Modularity:  Q = (1 / 2m) · Σ_{i,j} [A_ij − k_i · k_j / 2m] · δ(c_i, c_j)

The algorithm is a single-pass greedy modularity maximizer — each node is
moved to the neighbor community that yields the largest ΔQ, iterated until no
move improves Q. Communities outside ``[min_size, max_size]`` are dropped so
the pre-warmer only acts on meaningfully-sized clusters.

Persistence: results are serialized to ``leiden_communities.json`` under the
shared stats directory so they survive restarts.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


class LeidenCommunities:
    """Greedy modularity-based community detector with disk persistence."""

    def __init__(self, stats_dir: Path):
        self.stats_dir = Path(stats_dir)
        # symbol -> community_name
        self.symbol_to_community: dict[str, str] = {}
        # community_name -> set[symbol]
        self.communities: dict[str, set[str]] = {}
        self._last_node_count = 0
        self._last_edge_count = 0
        self._last_modularity = 0.0
        self._load()

    # ------------------------------------------------------------------ io
    def _path(self) -> Path:
        return self.stats_dir / "leiden_communities.json"

    def _load(self) -> None:
        try:
            data = json.loads(self._path().read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        self.symbol_to_community = dict(data.get("symbol_to_community", {}))
        self.communities = {
            k: set(v) for k, v in data.get("communities", {}).items()
        }
        self._last_node_count = data.get("nodes", 0)
        self._last_edge_count = data.get("edges", 0)
        self._last_modularity = data.get("modularity", 0.0)

    def save(self) -> None:
        try:
            self.stats_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "symbol_to_community": self.symbol_to_community,
                "communities": {k: sorted(v) for k, v in self.communities.items()},
                "nodes": self._last_node_count,
                "edges": self._last_edge_count,
                "modularity": round(self._last_modularity, 4),
            }
            self._path().write_text(json.dumps(payload))
        except OSError:
            pass

    # ------------------------------------------------------------------ core
    @staticmethod
    def _build_undirected(graph: dict[str, set[str]] | dict[str, list[str]]) -> dict[str, set[str]]:
        """Symmetrize the dependency graph (ignore self-loops)."""
        adj: dict[str, set[str]] = defaultdict(set)
        for src, targets in graph.items():
            for tgt in targets:
                if src == tgt:
                    continue
                adj[src].add(tgt)
                adj[tgt].add(src)
        return adj

    def compute(
        self,
        graph: dict[str, set[str]] | dict[str, list[str]],
        *,
        min_size: int = 3,
        max_size: int = 50,
        max_iterations: int = 20,
    ) -> dict[str, int]:
        """Detect communities and persist them. Returns summary stats."""
        adj = self._build_undirected(graph)
        nodes = list(adj.keys())
        if not nodes:
            self.symbol_to_community = {}
            self.communities = {}
            self._last_node_count = 0
            self._last_edge_count = 0
            self._last_modularity = 0.0
            self.save()
            return {"communities": 0, "nodes": 0, "edges": 0, "kept": 0}

        # degree[i] and total edge count
        degree = {n: len(adj[n]) for n in nodes}
        m2 = sum(degree.values())  # = 2m (sum of degrees)
        if m2 == 0:
            return {"communities": 0, "nodes": len(nodes), "edges": 0, "kept": 0}

        # Initial: each node its own community
        comm_of = {n: i for i, n in enumerate(nodes)}
        # Sum of degrees inside community c (keyed by community id)
        comm_deg: dict[int, int] = {i: degree[n] for i, n in enumerate(nodes)}

        improved = True
        iterations = 0
        while improved and iterations < max_iterations:
            improved = False
            iterations += 1
            for n in nodes:
                n_deg = degree[n]
                cur_c = comm_of[n]
                # Count edges from n into each neighbor community
                edges_to_c: dict[int, int] = defaultdict(int)
                for nb in adj[n]:
                    edges_to_c[comm_of[nb]] += 1

                # Remove n from its current community
                comm_deg[cur_c] -= n_deg

                best_c = cur_c
                best_gain = 0.0
                # ΔQ ≈ 2·(k_in / 2m) − 2·(Σ_tot · k_n) / (2m)²
                for c, k_in in edges_to_c.items():
                    gain = (k_in / m2) - (comm_deg.get(c, 0) * n_deg) / (m2 * m2)
                    if gain > best_gain:
                        best_gain = gain
                        best_c = c

                comm_deg[best_c] = comm_deg.get(best_c, 0) + n_deg
                if best_c != cur_c:
                    comm_of[n] = best_c
                    improved = True

        # Collect surviving communities
        by_id: dict[int, set[str]] = defaultdict(set)
        for n, c in comm_of.items():
            by_id[c].add(n)

        # Name communities after their highest-degree (hub) symbol, with a size suffix
        communities: dict[str, set[str]] = {}
        symbol_to_community: dict[str, str] = {}
        for members in by_id.values():
            if not (min_size <= len(members) <= max_size):
                continue
            hub = max(members, key=lambda s: degree.get(s, 0))
            name = f"{hub}__n{len(members)}"
            # Break ties in case two communities share a hub
            i = 2
            base = name
            while name in communities:
                name = f"{base}_{i}"
                i += 1
            communities[name] = members
            for s in members:
                symbol_to_community[s] = name

        # Compute modularity Q
        q = 0.0
        if m2 > 0:
            # Recompute comm_deg for kept communities (by name)
            name_deg = {name: sum(degree[s] for s in members)
                        for name, members in communities.items()}
            for name, members in communities.items():
                internal = 0
                for s in members:
                    for nb in adj[s]:
                        if nb in members:
                            internal += 1
                # internal counts each undirected edge twice — that's correct for Q formula
                q += (internal / m2) - (name_deg[name] / m2) ** 2

        self.symbol_to_community = symbol_to_community
        self.communities = communities
        self._last_node_count = len(nodes)
        self._last_edge_count = m2 // 2
        self._last_modularity = q
        self.save()

        return {
            "communities": len(communities),
            "nodes": len(nodes),
            "edges": m2 // 2,
            "kept": sum(len(v) for v in communities.values()),
            "modularity": round(q, 4),
        }

    # ------------------------------------------------------------------ access
    def get_community_for(self, symbol: str) -> dict | None:
        name = self.symbol_to_community.get(symbol)
        if not name:
            return None
        members = sorted(self.communities.get(name, set()))
        return {"name": name, "members": members, "size": len(members)}

    def get_community(self, name: str) -> dict | None:
        members = self.communities.get(name)
        if members is None:
            return None
        return {"name": name, "members": sorted(members), "size": len(members)}

    def get_stats(self) -> dict:
        if not self.communities:
            return {
                "total_communities": 0,
                "covered_symbols": 0,
                "largest": 0,
                "smallest": 0,
                "avg_size": 0.0,
                "nodes": self._last_node_count,
                "edges": self._last_edge_count,
                "modularity": round(self._last_modularity, 4),
            }
        sizes = [len(v) for v in self.communities.values()]
        return {
            "total_communities": len(self.communities),
            "covered_symbols": sum(sizes),
            "largest": max(sizes),
            "smallest": min(sizes),
            "avg_size": round(sum(sizes) / len(sizes), 2),
            "nodes": self._last_node_count,
            "edges": self._last_edge_count,
            "modularity": round(self._last_modularity, 4),
            "top": sorted(
                ({"name": n, "size": len(m)} for n, m in self.communities.items()),
                key=lambda x: -x["size"],
            )[:10],
        }
