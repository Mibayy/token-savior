"""Program slicing for Token Savior.

Backward slice = minimal subset of statements that affect a variable at a given
line. Useful for an agent that wants to debug "why is X wrong at line N" without
loading the full file.

This implementation is a pragmatic AST-level approximation of classic backward
slicing (Weiser 1981): we follow data dependencies (assignments, AugAssign,
For-target bindings) and include enclosing control-flow statements that
condition the path to the target line. It is intentionally simple and stays
robust on real-world Python -- on SyntaxError we fall back to a small window
around the criterion.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass
class SliceResult:
    lines: list[int]          # line numbers (1-based) in the slice
    source_lines: list[str]   # contents of those lines
    criterion: str            # "{variable}@{line}"
    total_lines: int          # original file/symbol size
    reduction_pct: float      # percentage of reduction vs total


def backward_slice(source: str, variable: str, line: int) -> SliceResult:
    """Compute the backward slice of *variable* at *line* inside *source*.

    Returns the minimal set of statements that influence the value of
    ``variable`` at ``line``. The algorithm walks the AST, records all
    assignments per variable, and runs a BFS from ``(variable, line)`` over
    the data-dependency graph. Enclosing if/for/while statements that lie
    before the criterion are also included, since they condition control flow.
    """
    lines_list = source.split("\n")
    total = len(lines_list)

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Robust fallback: 20-line window around the criterion.
        start = max(0, line - 10)
        end = min(total, line + 10)
        window = list(range(start + 1, end + 1))  # 1-based lines
        return SliceResult(
            lines=window,
            source_lines=lines_list[start:end],
            criterion=f"{variable}@{line}",
            total_lines=total,
            reduction_pct=round((1 - len(window) / max(total, 1)) * 100, 1),
        )

    # Map line -> AST nodes that start on that line.
    line_to_nodes: dict[int, list[ast.AST]] = {}
    for node in ast.walk(tree):
        ln = getattr(node, "lineno", None)
        if ln is not None:
            line_to_nodes.setdefault(ln, []).append(node)

    # Per-variable definition sites: var_name -> [line numbers].
    definitions: dict[str, list[int]] = {}

    class _DefVisitor(ast.NodeVisitor):
        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                for name in _names_in_target(target):
                    definitions.setdefault(name, []).append(node.lineno)
            self.generic_visit(node)

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            for name in _names_in_target(node.target):
                definitions.setdefault(name, []).append(node.lineno)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            for name in _names_in_target(node.target):
                definitions.setdefault(name, []).append(node.lineno)
            self.generic_visit(node)

        def visit_For(self, node: ast.For) -> None:
            for name in _names_in_target(node.target):
                definitions.setdefault(name, []).append(node.lineno)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            for arg in node.args.args:
                definitions.setdefault(arg.arg, []).append(node.lineno)
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    _DefVisitor().visit(tree)

    # BFS backward from (variable, line). visited prevents cycles on graphs
    # like x = f(x) or mutually recursive definitions.
    slice_lines: set[int] = set()
    queue: list[tuple[str, int]] = [(variable, line)]
    visited: set[tuple[str, int]] = set()

    while queue:
        var, ln = queue.pop(0)
        if (var, ln) in visited:
            continue
        visited.add((var, ln))

        for def_line in definitions.get(var, []):
            if def_line > ln:
                continue
            slice_lines.add(def_line)
            for node in line_to_nodes.get(def_line, []):
                if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.For)):
                    for child in ast.walk(node):
                        if isinstance(child, ast.Name) and child.id != var:
                            if (child.id, def_line) not in visited:
                                queue.append((child.id, def_line))

    slice_lines.add(line)

    # Include enclosing control-flow nodes (if/for/while/try) that start before
    # the criterion -- they condition the path to the target line.
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.For, ast.While, ast.Try)):
            ln = getattr(node, "lineno", None)
            if ln is not None and ln <= line:
                # Only keep control-flow nodes that actually contain (or
                # precede and dominate) a slice line.
                end_ln = getattr(node, "end_lineno", ln)
                if any(ln <= s <= end_ln for s in slice_lines):
                    slice_lines.add(ln)

    sorted_lines = sorted(slice_lines)
    chosen_source = [
        lines_list[ln - 1] if 0 < ln <= total else "" for ln in sorted_lines
    ]

    return SliceResult(
        lines=sorted_lines,
        source_lines=chosen_source,
        criterion=f"{variable}@{line}",
        total_lines=total,
        reduction_pct=round((1 - len(sorted_lines) / max(total, 1)) * 100, 1),
    )


def _names_in_target(target: ast.AST) -> list[str]:
    """Extract bound names from an assignment target (Name, Tuple, List, Starred)."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        out: list[str] = []
        for elt in target.elts:
            out.extend(_names_in_target(elt))
        return out
    if isinstance(target, ast.Starred):
        return _names_in_target(target.value)
    return []
