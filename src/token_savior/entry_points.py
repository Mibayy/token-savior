"""Entry point detection for token-savior."""
from __future__ import annotations
import os
from token_savior.models import ProjectIndex


def score_entry_points(index: ProjectIndex, max_results: int = 20) -> list[dict]:
    """Score all functions by likelihood of being execution entry points.

    Returns list of {name, file, line, score, reasons} sorted descending by score.
    """
    results = []

    for file_path, meta in index.files.items():
        filename = os.path.basename(file_path).lower()
        path_lower = file_path.lower()

        # File-level bonuses
        file_path_bonus = 0.0
        file_path_reasons = []
        if any(seg in path_lower for seg in ["/routes/", "/route.", "/api/", "/handlers/", "/controllers/"]):
            file_path_bonus += 3.0
            file_path_reasons.append("routes/api path")
        if filename in ("main.py", "app.py", "server.py", "cli.py", "index.ts", "index.js", "main.ts"):
            file_path_bonus += 0.5
            file_path_reasons.append(f"entry file ({filename})")

        for func in meta.functions:
            score = file_path_bonus
            reasons = list(file_path_reasons)
            name_lower = func.name.lower()

            if func.name in ("main", "run", "start", "serve", "app", "cli"):
                score += 2.0
                reasons.append(f"entry name ({func.name})")

            if any(name_lower.startswith(p) for p in ("handle", "on_", "dispatch")):
                score += 1.5
                reasons.append("handler prefix")
            elif name_lower.startswith("on") and len(func.name) > 2 and func.name[2].isupper():
                score += 1.5
                reasons.append("on* handler")

            if any(name_lower.endswith(s) for s in ("_handler", "_route", "_controller", "_view", "_endpoint")):
                score += 1.5
                reasons.append("handler suffix")

            callers = index.reverse_dependency_graph.get(func.qualified_name, set()) or \
                      index.reverse_dependency_graph.get(func.name, set())
            if not callers:
                score += 1.0
                reasons.append("no internal callers")

            if not func.is_method:
                score += 1.0
                reasons.append("top-level function")

            normalized = min(score / 5.0, 1.0)
            if normalized > 0.1:
                results.append({
                    "name": func.qualified_name or func.name,
                    "file": file_path,
                    "line": func.line_range.start,
                    "score": round(normalized, 3),
                    "reasons": reasons,
                    "params": func.parameters,
                })

    results.sort(key=lambda x: x["score"], reverse=True)
    if max_results > 0:
        results = results[:max_results]
    return results
