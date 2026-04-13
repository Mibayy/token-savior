"""Breaking change detection for Token Savior.

Compares the current working tree against a git ref and reports functions/
methods whose signatures changed in a backward-incompatible way.
"""

from __future__ import annotations

import ast
import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum

from token_savior.git_tracker import get_changed_files
from token_savior.models import ProjectIndex
from token_savior.symbol_hash import compute_body_hash


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class ChangeType(Enum):
    """Classification of a symbol-level change.

    Only SIGNATURE_CHANGED and REMOVED count as API-breaking. BODY_ONLY_CHANGED
    is reported as informational (refactor/bugfix, safe for downstream callers).
    """

    SIGNATURE_CHANGED = "signature_changed"  # breaking
    BODY_ONLY_CHANGED = "body_only_changed"  # non-breaking (info)
    ADDED = "added"                           # non-breaking (info)
    REMOVED = "removed"                       # breaking


@dataclass
class BreakingChange:
    file: str
    symbol: str
    line: int
    severity: str  # "breaking" | "warning" | "info"
    message: str
    change_type: ChangeType | None = None


# ---------------------------------------------------------------------------
# Internal signature type: richer than FunctionInfo.parameters
# ---------------------------------------------------------------------------


@dataclass
class _ParamInfo:
    name: str
    has_default: bool


@dataclass
class _FuncSig:
    """Richer function signature extracted directly via AST."""

    name: str
    qualified_name: str
    line: int
    params: list[_ParamInfo]
    return_annotation: str | None  # ast.unparse'd, or None
    is_method: bool
    parent_class: str | None
    end_line: int = 0  # for body hashing
    body_hash: str = ""  # filled by _extract_signatures when source is provided


@dataclass
class _ClassSig:
    name: str
    line: int
    methods: list[_FuncSig]


# ---------------------------------------------------------------------------
# AST-level signature extraction
# ---------------------------------------------------------------------------


def _extract_signatures(source: str) -> tuple[list[_FuncSig], list[_ClassSig]]:
    """Parse *source* and extract rich function/class signatures.

    Each _FuncSig is populated with a body_hash so callers can distinguish
    body-only edits from signature-level breaking changes.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], []

    lines = source.splitlines()
    top_funcs: list[_FuncSig] = []
    classes: list[_ClassSig] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            top_funcs.append(_sig_from_func(node, parent_class=None, lines=lines))
        elif isinstance(node, ast.ClassDef):
            methods: list[_FuncSig] = []
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(_sig_from_func(item, parent_class=node.name, lines=lines))
            classes.append(_ClassSig(name=node.name, line=node.lineno, methods=methods))

    return top_funcs, classes


def _sig_from_func(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    parent_class: str | None,
    lines: list[str] | None = None,
) -> _FuncSig:
    args = node.args
    # Build a flat list of (arg, has_default) for positional args
    # defaults are right-aligned in node.args.defaults
    n_args = len(args.args)
    n_defaults = len(args.defaults)
    params: list[_ParamInfo] = []

    for i, arg in enumerate(args.args):
        # skip self/cls for methods
        if parent_class is not None and arg.arg in ("self", "cls"):
            continue
        default_offset = i - (n_args - n_defaults)
        has_default = default_offset >= 0
        params.append(_ParamInfo(name=arg.arg, has_default=has_default))

    # *args
    if args.vararg:
        params.append(_ParamInfo(name=f"*{args.vararg.arg}", has_default=False))

    # keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        has_default = args.kw_defaults[i] is not None
        params.append(_ParamInfo(name=arg.arg, has_default=has_default))

    # **kwargs
    if args.kwarg:
        params.append(_ParamInfo(name=f"**{args.kwarg.arg}", has_default=False))

    # Return annotation
    ret_ann: str | None = None
    if node.returns is not None:
        try:
            ret_ann = ast.unparse(node.returns)
        except Exception:
            ret_ann = "<unknown>"

    qualified_name = f"{parent_class}.{node.name}" if parent_class else node.name

    end_line = getattr(node, "end_lineno", None) or node.lineno
    body_hash = ""
    if lines is not None:
        body_hash = compute_body_hash(lines, node.lineno, end_line)

    return _FuncSig(
        name=node.name,
        qualified_name=qualified_name,
        line=node.lineno,
        params=params,
        return_annotation=ret_ann,
        is_method=parent_class is not None,
        parent_class=parent_class,
        end_line=end_line,
        body_hash=body_hash,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_breaking_changes(index: ProjectIndex, since_ref: str = "HEAD~1") -> str:
    """Detect breaking changes between *since_ref* and the current working tree.

    Returns a human-readable report string.
    """
    changeset = get_changed_files(index.root_path, since_ref)

    all_changes: list[BreakingChange] = []

    # Modified files: compare old vs new signatures
    for rel_path in changeset.modified:
        if not rel_path.endswith(".py"):
            continue
        old_content = _get_old_file_content(index.root_path, since_ref, rel_path)
        if old_content is None:
            continue

        abs_path = os.path.join(index.root_path, rel_path)
        try:
            with open(abs_path, "r", encoding="utf-8") as fh:
                new_content = fh.read()
        except OSError:
            continue

        old_funcs, old_classes = _extract_signatures(old_content)
        new_funcs, new_classes = _extract_signatures(new_content)

        all_changes.extend(_compare_functions(old_funcs, new_funcs, rel_path))
        all_changes.extend(_compare_classes(old_classes, new_classes, rel_path))

    # Deleted files: every top-level function/class is a breaking removal
    for rel_path in changeset.deleted:
        if not rel_path.endswith(".py"):
            continue
        old_content = _get_old_file_content(index.root_path, since_ref, rel_path)
        if old_content is None:
            continue
        old_funcs, old_classes = _extract_signatures(old_content)
        for func in old_funcs:
            all_changes.append(
                BreakingChange(
                    file=rel_path,
                    symbol=func.name,
                    line=func.line,
                    severity="breaking",
                    message=f"function {func.name}(): file was deleted",
                )
            )
        for cls in old_classes:
            all_changes.append(
                BreakingChange(
                    file=rel_path,
                    symbol=cls.name,
                    line=cls.line,
                    severity="breaking",
                    message=f"class {cls.name}: file was deleted",
                )
            )

    return _format_report(since_ref, all_changes)


# ---------------------------------------------------------------------------
# Helpers: git
# ---------------------------------------------------------------------------


def _get_old_file_content(root_path: str, ref: str, file_path: str) -> str | None:
    """Return file content at *ref*, or None if it didn't exist."""
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{file_path}"],
            cwd=root_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------------------
# Helpers: comparison
# ---------------------------------------------------------------------------


def _compare_functions(
    old: list[_FuncSig],
    new: list[_FuncSig],
    file_path: str,
) -> list[BreakingChange]:
    """Compare top-level functions between two versions."""
    changes: list[BreakingChange] = []
    old_map = {f.name: f for f in old}
    new_map = {f.name: f for f in new}

    # Removed entirely
    for name, old_func in old_map.items():
        if name not in new_map:
            changes.append(
                BreakingChange(
                    file=file_path,
                    symbol=name,
                    line=old_func.line,
                    severity="breaking",
                    message=f"function {name}(): was removed",
                    change_type=ChangeType.REMOVED,
                )
            )
            continue

        new_func = new_map[name]
        sig_changes = _diff_params(
            old_func.params, new_func.params, name, new_func.line, file_path
        )
        ret_changes = _diff_return_type(old_func, new_func, file_path)
        sig_changes_combined = sig_changes + ret_changes
        for c in sig_changes_combined:
            c.change_type = ChangeType.SIGNATURE_CHANGED
        changes.extend(sig_changes_combined)

        # Non-breaking: signature identical, body differs (refactor/bugfix).
        if not sig_changes_combined and old_func.body_hash and new_func.body_hash:
            if old_func.body_hash != new_func.body_hash:
                changes.append(
                    BreakingChange(
                        file=file_path,
                        symbol=name,
                        line=new_func.line,
                        severity="info",
                        message=f"function {name}(): body only (refactor/bugfix)",
                        change_type=ChangeType.BODY_ONLY_CHANGED,
                    )
                )

    # Added functions (non-breaking, info only).
    for name, new_func in new_map.items():
        if name not in old_map:
            changes.append(
                BreakingChange(
                    file=file_path,
                    symbol=name,
                    line=new_func.line,
                    severity="info",
                    message=f"function {name}(): added",
                    change_type=ChangeType.ADDED,
                )
            )

    return changes


def _compare_classes(
    old: list[_ClassSig],
    new: list[_ClassSig],
    file_path: str,
) -> list[BreakingChange]:
    """Compare classes and their methods between two versions."""
    changes: list[BreakingChange] = []
    old_map = {c.name: c for c in old}
    new_map = {c.name: c for c in new}

    for name, old_cls in old_map.items():
        if name not in new_map:
            changes.append(
                BreakingChange(
                    file=file_path,
                    symbol=name,
                    line=old_cls.line,
                    severity="breaking",
                    message=f"class {name}: was removed entirely",
                    change_type=ChangeType.REMOVED,
                )
            )
            continue

        new_cls = new_map[name]
        old_methods = {m.name: m for m in old_cls.methods}
        new_methods = {m.name: m for m in new_cls.methods}

        for mname, old_m in old_methods.items():
            symbol = f"{name}.{mname}"
            if mname not in new_methods:
                changes.append(
                    BreakingChange(
                        file=file_path,
                        symbol=symbol,
                        line=old_m.line,
                        severity="breaking",
                        message=f"class {name}: method {mname}() was removed",
                        change_type=ChangeType.REMOVED,
                    )
                )
            else:
                new_m = new_methods[mname]
                sig_changes = _diff_params(
                    old_m.params, new_m.params, symbol, new_m.line, file_path
                )
                ret_changes = _diff_return_type(old_m, new_m, file_path)
                sig_changes_combined = sig_changes + ret_changes
                for c in sig_changes_combined:
                    c.change_type = ChangeType.SIGNATURE_CHANGED
                changes.extend(sig_changes_combined)

                if not sig_changes_combined and old_m.body_hash and new_m.body_hash:
                    if old_m.body_hash != new_m.body_hash:
                        changes.append(
                            BreakingChange(
                                file=file_path,
                                symbol=symbol,
                                line=new_m.line,
                                severity="info",
                                message=f"method {symbol}(): body only (refactor/bugfix)",
                                change_type=ChangeType.BODY_ONLY_CHANGED,
                            )
                        )

        for mname, new_m in new_methods.items():
            if mname not in old_methods:
                changes.append(
                    BreakingChange(
                        file=file_path,
                        symbol=f"{name}.{mname}",
                        line=new_m.line,
                        severity="info",
                        message=f"class {name}: method {mname}() added",
                        change_type=ChangeType.ADDED,
                    )
                )

    return changes


def _diff_params(
    old_params: list[_ParamInfo],
    new_params: list[_ParamInfo],
    symbol_name: str,
    line: int,
    file_path: str,
) -> list[BreakingChange]:
    """Return breaking/warning changes between two parameter lists."""
    changes: list[BreakingChange] = []

    old_names = {p.name for p in old_params}
    new_names = {p.name for p in new_params}
    new_map = {p.name: p for p in new_params}

    # Removed params
    removed = old_names - new_names
    for pname in sorted(removed):
        changes.append(
            BreakingChange(
                file=file_path,
                symbol=symbol_name,
                line=line,
                severity="breaking",
                message=f"function {symbol_name}(): parameter '{pname}' was removed",
            )
        )

    # Added params
    added_names = new_names - old_names
    for pname in sorted(added_names):
        p = new_map[pname]
        if p.has_default:
            changes.append(
                BreakingChange(
                    file=file_path,
                    symbol=symbol_name,
                    line=line,
                    severity="warning",
                    message=(
                        f"function {symbol_name}(): parameter '{pname}' added "
                        f"(has default, backward compatible)"
                    ),
                )
            )
        else:
            changes.append(
                BreakingChange(
                    file=file_path,
                    symbol=symbol_name,
                    line=line,
                    severity="breaking",
                    message=(
                        f"function {symbol_name}(): parameter '{pname}' added without default "
                        f"(existing callers will fail)"
                    ),
                )
            )

    return changes


def _diff_return_type(
    old_func: _FuncSig,
    new_func: _FuncSig,
    file_path: str,
) -> list[BreakingChange]:
    """Warn when a return type annotation changes."""
    if (
        old_func.return_annotation is not None
        and new_func.return_annotation is not None
        and old_func.return_annotation != new_func.return_annotation
    ):
        return [
            BreakingChange(
                file=file_path,
                symbol=old_func.name,
                line=new_func.line,
                severity="warning",
                message=(
                    f"function {old_func.name}(): return type changed from "
                    f"'{old_func.return_annotation}' to '{new_func.return_annotation}'"
                ),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_report(since_ref: str, changes: list[BreakingChange]) -> str:
    if not changes:
        return (
            f"Breaking Change Analysis ({since_ref}..working tree) -- no breaking changes detected"
        )

    breaking = [c for c in changes if c.severity == "breaking"]
    warnings = [c for c in changes if c.severity == "warning"]
    infos = [c for c in changes if c.severity == "info"]

    total = len(changes)
    lines: list[str] = [
        f"Breaking Change Analysis ({since_ref}..working tree) "
        f"-- {total} issue{'s' if total != 1 else ''} found",
    ]

    if breaking:
        lines.append("")
        lines.append("BREAKING:")
        for c in breaking:
            lines.append(f"  {c.file}:{c.line} - {c.message}")

    if warnings:
        lines.append("")
        lines.append("WARNING:")
        for c in warnings:
            lines.append(f"  {c.file}:{c.line} - {c.message}")

    if infos:
        lines.append("")
        lines.append("NON-BREAKING:")
        for c in infos:
            lines.append(f"  {c.file}:{c.line} - {c.message}")

    return "\n".join(lines)
