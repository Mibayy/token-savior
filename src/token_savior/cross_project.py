from __future__ import annotations

import sys
from collections import defaultdict

from token_savior.models import ProjectIndex


def _is_stdlib(module: str) -> bool:
    """Check if module is a Python stdlib module."""
    top = module.split(".")[0]
    return top in sys.stdlib_module_names


def _get_all_imports(index: ProjectIndex) -> set[str]:
    """Collect normalized import references for this project."""
    result: set[str] = set()
    for path, meta in index.files.items():
        for imp in meta.imports:
            normalized = _normalize_import_reference(path, imp.module)
            if normalized:
                result.add(normalized)
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


def _get_java_namespaces(index: ProjectIndex) -> set[str]:
    """Collect package and class namespaces provided by Java sources in this project."""
    namespaces: set[str] = set()
    for path, meta in index.files.items():
        if not path.endswith(".java"):
            continue
        if meta.module_name:
            namespaces.add(meta.module_name)
        for cls in meta.classes:
            qualified_name = getattr(cls, "qualified_name", None)
            if qualified_name:
                namespaces.add(qualified_name)
    return namespaces


def _get_project_namespaces(index: ProjectIndex) -> set[str]:
    """Collect importable namespaces provided by this project."""
    return _get_project_packages(index) | _get_java_namespaces(index)


def _normalize_import_reference(file_path: str, module: str) -> str:
    """Normalize imports to a comparable namespace form for each language."""
    if file_path.endswith(".java"):
        return module
    return module.split(".")[0]


def _matches_namespace(import_name: str, namespace: str) -> bool:
    """Whether an imported name falls within a provided namespace."""
    return import_name == namespace or import_name.startswith(f"{namespace}.")


def _is_standard_library_import(import_name: str) -> bool:
    """Whether an import belongs to a standard library namespace."""
    top = import_name.split(".")[0]
    return _is_stdlib(import_name) or top in {"java", "javax", "jdk", "sun"}


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
    project_namespaces: dict[str, set[str]] = {}
    project_imports: dict[str, set[str]] = {}

    for name, index in indices.items():
        project_namespaces[name] = _get_project_namespaces(index)
        project_imports[name] = _get_all_imports(index)

    # ------------------------------------------------------------------ #
    # 1. Direct dependencies: A imports top-level name provided by B
    # ------------------------------------------------------------------ #
    direct_deps: list[tuple[str, str, list[str]]] = []  # (A, B, [matched_modules])

    for proj_a, imports_a in project_imports.items():
        for proj_b, namespaces_b in project_namespaces.items():
            if proj_a == proj_b:
                continue
            matched = sorted(
                import_name
                for import_name in imports_a
                if any(_matches_namespace(import_name, namespace) for namespace in namespaces_b)
            )
            if matched:
                direct_deps.append((proj_a, proj_b, matched))

    # ------------------------------------------------------------------ #
    # 2. Shared external dependencies (third-party, not stdlib, not
    #    provided by any project in the set)
    # ------------------------------------------------------------------ #
    all_provided: set[str] = set()
    for namespaces in project_namespaces.values():
        all_provided |= namespaces

    # module → list of project names that import it
    module_users: dict[str, list[str]] = defaultdict(list)
    for proj_name, imports in project_imports.items():
        for import_name in imports:
            if _is_standard_library_import(import_name):
                continue
            if any(_matches_namespace(import_name, namespace) for namespace in all_provided):
                continue
            module_users[import_name].append(proj_name)

    shared_deps = {
        mod: sorted(users) for mod, users in sorted(module_users.items()) if len(users) >= 2
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
