from __future__ import annotations

import sys
from collections import defaultdict

from token_savior.models import ProjectIndex


def _is_stdlib(module: str) -> bool:
    """Check if module is a Python stdlib module."""
    top = module.split(".")[0]
    return top in sys.stdlib_module_names


def _get_all_imports(index: ProjectIndex) -> set[str]:
    """Collect all top-level module names imported by this project (raw, no stdlib filter)."""
    result: set[str] = set()
    for meta in index.files.values():
        for imp in meta.imports:
            top = imp.module.split(".")[0]
            if top:
                result.add(top)
    return result


def _get_project_packages(index: ProjectIndex) -> set[str]:
    """Infer package names provided by this project from file paths.

    Handles common layouts:
    - src/pkg_name/module.py  →  pkg_name
    - pkg_name/module.py      →  pkg_name
    Files at root level (no subdirectory) are ignored.
    """
    packages: set[str] = set()
    for path in index.files:
        # Normalise separators
        parts = path.replace("\\", "/").split("/")
        if len(parts) < 2:
            # Top-level file (e.g. "main.py") — not a package
            continue
        # Skip common non-package first segments
        if parts[0] == "src" and len(parts) >= 3:
            # src/pkg_name/... → pkg_name
            candidate = parts[1]
        else:
            # pkg_name/... → pkg_name
            candidate = parts[0]

        # Only keep valid Python identifier-style names
        if candidate and candidate.isidentifier() and not candidate.startswith("."):
            packages.add(candidate)

    return packages


def find_cross_project_deps(indices: dict[str, ProjectIndex]) -> str:
    """Detect cross-project dependencies from a dict of project_name → ProjectIndex.

    Returns a formatted string describing:
    - Direct dependencies: project A imports a module provided by project B
    - Shared external dependencies: multiple projects import the same third-party module
    """
    n = len(indices)

    if n == 0:
        return "Cross-Project Dependencies -- no cross-project dependencies found"

    header = f"Cross-Project Dependencies -- {n} project{'s' if n != 1 else ''} analyzed"

    # Build per-project data
    project_packages: dict[str, set[str]] = {}
    project_imports: dict[str, set[str]] = {}  # top-level, raw

    for name, index in indices.items():
        project_packages[name] = _get_project_packages(index)
        project_imports[name] = _get_all_imports(index)

    # ------------------------------------------------------------------ #
    # 1. Direct dependencies: A imports top-level name provided by B
    # ------------------------------------------------------------------ #
    direct_deps: list[tuple[str, str, list[str]]] = []  # (A, B, [matched_modules])

    for proj_a, imports_a in project_imports.items():
        for proj_b, packages_b in project_packages.items():
            if proj_a == proj_b:
                continue
            matched = sorted(imports_a & packages_b)
            if matched:
                direct_deps.append((proj_a, proj_b, matched))

    # ------------------------------------------------------------------ #
    # 2. Shared external dependencies (third-party, not stdlib, not
    #    provided by any project in the set)
    # ------------------------------------------------------------------ #
    all_provided: set[str] = set()
    for pkgs in project_packages.values():
        all_provided |= pkgs

    # module → list of project names that import it
    module_users: dict[str, list[str]] = defaultdict(list)
    for proj_name, imports in project_imports.items():
        for mod in imports:
            if not _is_stdlib(mod) and mod not in all_provided:
                module_users[mod].append(proj_name)

    shared_deps = {
        mod: sorted(users)
        for mod, users in sorted(module_users.items())
        if len(users) >= 2
    }

    # ------------------------------------------------------------------ #
    # Format output
    # ------------------------------------------------------------------ #
    if not direct_deps and not shared_deps:
        return f"{header}\n\nno cross-project dependencies found"

    lines = [header, ""]

    if direct_deps:
        lines.append("Direct dependencies:")
        for proj_a, proj_b, mods in sorted(direct_deps):
            mods_str = ", ".join(mods)
            lines.append(f"  {proj_a} → {proj_b} (imports: {mods_str})")

    if shared_deps:
        if direct_deps:
            lines.append("")
        lines.append("Shared external dependencies:")
        for mod, users in shared_deps.items():
            users_str = ", ".join(users)
            lines.append(f"  {mod}: used by {users_str}")

    return "\n".join(lines)
