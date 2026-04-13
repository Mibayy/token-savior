"""Functional community detection for token-savior.

Uses label propagation on the bidirectional dependency graph to group
closely related symbols into functional clusters.
"""

from __future__ import annotations
import random
from collections import defaultdict
from token_savior.models import ProjectIndex


def compute_communities(index: ProjectIndex, max_iterations: int = 10) -> dict[str, str]:
    """Compute symbol communities using label propagation.

    Returns: dict mapping symbol_name -> community_label (which is the
    alphabetically smallest symbol in the community, used as stable ID).
    """
    # Build undirected adjacency from both dep graphs
    adjacency: dict[str, set[str]] = defaultdict(set)

    all_symbols: set[str] = set()
    for sym, deps in index.global_dependency_graph.items():
        all_symbols.add(sym)
        for dep in deps:
            all_symbols.add(dep)
            adjacency[sym].add(dep)
            adjacency[dep].add(sym)
    for sym, deps in index.reverse_dependency_graph.items():
        all_symbols.add(sym)
        for dep in deps:
            all_symbols.add(dep)
            adjacency[sym].add(dep)
            adjacency[dep].add(sym)

    if not all_symbols:
        return {}

    # Initialize: each symbol is its own community
    labels: dict[str, str] = {sym: sym for sym in all_symbols}

    # Label propagation
    symbols_list = sorted(all_symbols)
    for _ in range(max_iterations):
        changed = False
        # Shuffle for stability
        order = symbols_list[:]
        random.shuffle(order)
        for sym in order:
            neighbors = adjacency.get(sym, set())
            if not neighbors:
                continue
            # Count neighbor labels
            label_counts: dict[str, int] = defaultdict(int)
            for nb in neighbors:
                label_counts[labels.get(nb, nb)] += 1
            if not label_counts:
                continue
            # Pick most common label (tie-break: alphabetically smallest)
            max_count = max(label_counts.values())
            best_labels = sorted(lbl for lbl, c in label_counts.items() if c == max_count)
            new_label = best_labels[0]
            if new_label != labels[sym]:
                labels[sym] = new_label
                changed = True
        if not changed:
            break

    # Normalize: use the alphabetically smallest member as the community ID
    # Group by label
    groups: dict[str, list[str]] = defaultdict(list)
    for sym, lbl in labels.items():
        groups[lbl].append(sym)

    # Reassign: community ID = most-connected member, then lexical tie-break.
    final: dict[str, str] = {}
    for members in groups.values():
        community_id = min(
            members,
            key=lambda sym: (-len(adjacency.get(sym, set()) & set(members)), sym),
        )
        for sym in members:
            final[sym] = community_id

    return final


def get_cluster_for_symbol(
    symbol: str,
    communities: dict[str, str],
    index: ProjectIndex,
    max_members: int = 30,
) -> dict:
    """Get the full cluster for a symbol.

    Returns {community_id, members: [{name, file, line, type}], size}.
    """
    if symbol not in communities:
        # Try partial match
        matches = [s for s in communities if s == symbol or s.endswith(f".{symbol}")]
        if not matches:
            return {"error": f"Symbol '{symbol}' not found in any community"}
        symbol = matches[0]

    canonical_community_id = communities[symbol]

    # Find all members of this community
    members_raw = sorted(s for s, cid in communities.items() if cid == canonical_community_id)
    if symbol in members_raw:
        members_raw.remove(symbol)
        members_raw.insert(0, symbol)

    def _resolve_member(sym: str) -> dict:
        entry: dict = {"name": sym}
        for file_path, meta in index.files.items():
            for cls in meta.classes:
                qualified_name = getattr(cls, "qualified_name", None) or cls.name
                if cls.name == sym or qualified_name == sym:
                    entry.update({"file": file_path, "line": cls.line_range.start, "type": "class"})
                    return entry
            for func in meta.functions:
                if func.name == sym or func.qualified_name == sym:
                    entry.update(
                        {
                            "file": file_path,
                            "line": func.line_range.start,
                            "type": "method" if func.is_method else "function",
                        }
                    )
                    return entry
        if sym in index.symbol_table:
            entry["file"] = index.symbol_table[sym]
        return entry

    # Enrich member info
    members = []
    for sym in members_raw[:max_members]:
        members.append(_resolve_member(sym))

    return {
        "community_id": symbol,
        "canonical_community_id": canonical_community_id,
        "queried_symbol": symbol,
        "size": len([s for s, cid in communities.items() if cid == canonical_community_id]),
        "members": members,
        "truncated": len(members_raw) > max_members,
    }
