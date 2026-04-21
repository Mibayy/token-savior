"""Dead code detection for Token Savior.

Finds functions and classes in a ProjectIndex that have no known callers and
are not considered entry points (routes, test helpers, __init__, framework
dispatch hooks, etc.).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from token_savior.models import ClassInfo, FunctionInfo, ProjectIndex


# ---------------------------------------------------------------------------
# Entry-point detection
# ---------------------------------------------------------------------------

_ENTRY_POINT_NAMES = frozenset({"main", "__init__", "__main__"})

_ENTRY_POINT_DECORATOR_KEYWORDS = frozenset(
    {
        "route",
        "app.",
        "api.",
        "click",
        "command",
        "task",
        "test",
        "fixture",
        "setup",
        "teardown",
    }
)

_ENTRY_POINT_CLASS_DECORATOR_KEYWORDS = frozenset({"dataclass", "model"})
_UNSUPPORTED_FILE_EXTENSIONS = frozenset({".ts", ".tsx", ".jsx"})
_SPRING_CLASS_DECORATORS = frozenset(
    {
        "RestController",
        "Controller",
        "RequestMapping",
        "Configuration",
        "ConfigurationProperties",
        "Service",
        "Component",
        "Repository",
        "SpringBootApplication",
    }
)
_SPRING_METHOD_DECORATORS = frozenset(
    {
        "Bean",
        "GetMapping",
        "PostMapping",
        "PutMapping",
        "PatchMapping",
        "DeleteMapping",
        "RequestMapping",
    }
)
_JAVA_LIFECYCLE_DECORATORS = frozenset({"PreDestroy", "PostConstruct"})
_JMH_CLASS_DECORATORS = frozenset({"State"})
_JMH_METHOD_DECORATORS = frozenset({"Benchmark", "Setup", "TearDown"})
_JAVA_DYNAMIC_DISPATCH_BASES = frozenset({"Runnable", "Thread"})
_JAVA_CALLBACK_BASE_SUFFIXES = frozenset(
    {
        "Handler",
        "Listener",
        "Consumer",
        "Supplier",
        "Function",
        "Predicate",
        "Resolver",
        "Sender",
        "Transport",
        "Decoder",
        "Reader",
        "Writer",
        "Callback",
    }
)
_JAVA_CALLBACK_METHOD_NAMES = frozenset(
    {
        "run",
        "call",
        "accept",
        "handle",
        "send",
        "get",
        "decode",
        "resolve",
        "tick",
        "pollOnce",
        "backfillAll",
        "snapshot",
    }
)
_JAVA_VALUE_CLASS_SUFFIXES = frozenset(
    {
        "Config",
        "Properties",
        "Event",
        "Snapshot",
        "Response",
        "Request",
        "Payload",
        "Summary",
        "Options",
        "State",
        "Query",
    }
)


def _is_test_file(file_path: str) -> bool:
    """Return True if the file is a Python or Java test file."""
    basename = os.path.basename(file_path)
    return (
        basename.startswith("test_")
        or basename.endswith("_test.py")
        or (
            file_path.endswith(".java")
            and (
                "src/test/java/" in file_path
                or basename.endswith(("Test.java", "Tests.java", "IT.java", "ITCase.java"))
            )
        )
    )


def _is_unsupported_file(file_path: str) -> bool:
    return os.path.splitext(file_path)[1].lower() in _UNSUPPORTED_FILE_EXTENSIONS


def _is_jmh_file(file_path: str) -> bool:
    normalized = file_path.replace("\\", "/").lower()
    return "/jmh/" in normalized or os.path.basename(normalized).endswith("benchmark.java")


def _is_init_file(file_path: str) -> bool:
    """Return True if the file is a package __init__.py."""
    return os.path.basename(file_path) == "__init__.py"


def _decorator_matches_keywords(decorators: list[str], keywords: frozenset[str]) -> bool:
    """Return True if any decorator string contains any of the given keywords."""
    for dec in decorators:
        dec_lower = dec.lower()
        for kw in keywords:
            if kw in dec_lower:
                return True
    return False


def _decorator_names(decorators: list[str]) -> set[str]:
    return {decorator.split(".")[-1] for decorator in decorators}


def _is_spring_managed_class(cls: ClassInfo | None) -> bool:
    if cls is None:
        return False
    return bool(_decorator_names(cls.decorators) & _SPRING_CLASS_DECORATORS)


def _is_java_dynamic_dispatch_method(func: FunctionInfo, parent_class: ClassInfo | None) -> bool:
    if parent_class is None:
        return False
    decorator_names = _decorator_names(func.decorators)
    if "Override" in decorator_names and parent_class.base_classes:
        return True
    if func.name == "run" and set(parent_class.base_classes) & _JAVA_DYNAMIC_DISPATCH_BASES:
        return True
    return False


def _class_declaration_text(cls: ClassInfo, meta) -> str:
    if not meta.lines:
        return ""
    start = max(0, cls.line_range.start - 1)
    end = min(len(meta.lines), cls.line_range.start + 1)
    return " ".join(meta.lines[start:end]).lower()


def _is_java_type_only_class(cls: ClassInfo, file_path: str, meta) -> bool:
    if not file_path.endswith(".java"):
        return False
    declaration_text = _class_declaration_text(cls, meta)
    return " interface " in f" {declaration_text} " or "@interface" in declaration_text


def _is_java_record_class(cls: ClassInfo, file_path: str, meta) -> bool:
    if not file_path.endswith(".java"):
        return False
    declaration_text = _class_declaration_text(cls, meta)
    return " record " in f" {declaration_text} "


def _class_has_java_main_method(cls: ClassInfo) -> bool:
    for method in cls.methods:
        if method.name != "main":
            continue
        if len(method.parameters) != 1:
            continue
        parameter = method.parameters[0]
        if "String[]" in parameter or "String..." in parameter or "args" in parameter:
            return True
    return False


def _method_signature_key(func: FunctionInfo) -> str:
    qualified_name = func.qualified_name or func.name
    if "." not in qualified_name:
        return qualified_name
    return qualified_name.rsplit(".", 1)[-1]


def _class_name_index(index: ProjectIndex) -> dict[str, list[ClassInfo]]:
    class_index: dict[str, list[ClassInfo]] = {}
    for meta in index.files.values():
        for cls in meta.classes:
            class_index.setdefault(cls.name, []).append(cls)
            qualified_name = cls.qualified_name or cls.name
            class_index.setdefault(qualified_name, []).append(cls)
    return class_index


def _collect_signature_propagated_live_symbols(
    index: ProjectIndex, pre_live_symbols: set[str]
) -> set[str]:
    propagated: set[str] = set()
    class_index = _class_name_index(index)
    rdg = index.reverse_dependency_graph

    for meta in index.files.values():
        for cls in meta.classes:
            for base_name in cls.base_classes:
                for base_cls in class_index.get(base_name, []):
                    base_methods = {
                        _method_signature_key(method): method for method in base_cls.methods
                    }
                    for method in cls.methods:
                        base_method = base_methods.get(_method_signature_key(method))
                        if base_method is None:
                            continue
                        base_symbol = base_method.qualified_name
                        if (
                            rdg.get(base_symbol)
                            or rdg.get(base_method.name)
                            or base_symbol in pre_live_symbols
                        ):
                            propagated.add(method.qualified_name)
    return propagated


def _duplicate_symbol_sets(index: ProjectIndex) -> tuple[set[str], set[str]]:
    duplicate_classes = set(index.duplicate_classes.keys())
    duplicate_methods: set[str] = set()
    if not duplicate_classes:
        return duplicate_classes, duplicate_methods
    for meta in index.files.values():
        for cls in meta.classes:
            qualified_name = cls.qualified_name or cls.name
            if qualified_name not in duplicate_classes:
                continue
            duplicate_methods.update(method.qualified_name for method in cls.methods)
    return duplicate_classes, duplicate_methods


def _cross_project_live_symbols(
    index: ProjectIndex,
    sibling_indices: dict[str, ProjectIndex] | None,
) -> set[str]:
    if not sibling_indices:
        return set()

    current_symbols: set[str] = set()
    simple_to_qualified: dict[str, set[str]] = {}
    class_methods: dict[str, set[str]] = {}
    package_to_classes: dict[str, set[str]] = {}
    symbol_files = index.symbol_table
    for file_path, meta in index.files.items():
        for func in meta.functions:
            current_symbols.add(func.qualified_name)
            if symbol_files.get(func.name) == file_path:
                current_symbols.add(func.name)
            simple_to_qualified.setdefault(func.name, set()).add(func.qualified_name)
        for cls in meta.classes:
            qualified_name = cls.qualified_name or cls.name
            current_symbols.add(qualified_name)
            if symbol_files.get(cls.name) == file_path:
                current_symbols.add(cls.name)
            simple_to_qualified.setdefault(cls.name, set()).add(qualified_name)
            class_methods.setdefault(qualified_name, set()).update(
                method.qualified_name for method in cls.methods
            )
            if "." in qualified_name:
                package_to_classes.setdefault(qualified_name.rsplit(".", 1)[0], set()).add(qualified_name)

    live: set[str] = set()

    def _mark_symbol(symbol: str) -> None:
        if symbol in current_symbols:
            live.add(symbol)
            if "." in symbol:
                live.add(symbol.rsplit(".", 1)[0])
            for qualified in simple_to_qualified.get(symbol, set()):
                live.add(qualified)
                if "." in qualified:
                    live.add(qualified.rsplit(".", 1)[0])

    def _mark_class_api(class_name: str) -> None:
        if class_name not in current_symbols and class_name not in simple_to_qualified:
            return
        _mark_symbol(class_name)
        for qualified_class in simple_to_qualified.get(class_name, {class_name}):
            if qualified_class in class_methods:
                live.update(class_methods[qualified_class])

    for sibling in sibling_indices.values():
        if sibling.root_path == index.root_path:
            continue
        for deps in sibling.global_dependency_graph.values():
            for dep in deps:
                _mark_symbol(dep)
                base_dep = dep.rsplit(".", 1)[0] if "." in dep else dep
                _mark_class_api(base_dep)

        for meta in sibling.files.values():
            for imp in meta.imports:
                if imp.is_from_import:
                    if imp.module in current_symbols:
                        _mark_symbol(imp.module)
                    for name in imp.names:
                        _mark_symbol(name)
                    continue

                imported_class = imp.module
                imported_simple = imported_class.rsplit(".", 1)[-1]
                if imported_class in current_symbols:
                    _mark_class_api(imported_class)
                elif imported_simple in simple_to_qualified:
                    _mark_class_api(imported_simple)

                if imp.names == ["*"] and imported_class in package_to_classes:
                    for qualified_class in package_to_classes[imported_class]:
                        _mark_class_api(qualified_class)
    return live


def _is_java_callback_like_method(func: FunctionInfo, parent_class: ClassInfo | None) -> bool:
    if parent_class is None or not parent_class.base_classes:
        return False
    callback_bases = any(
        base.endswith(tuple(_JAVA_CALLBACK_BASE_SUFFIXES))
        or base in _JAVA_DYNAMIC_DISPATCH_BASES
        for base in parent_class.base_classes
    )
    if not callback_bases:
        return False
    if func.name in _JAVA_CALLBACK_METHOD_NAMES:
        return True
    if func.name.startswith("on") and len(func.name) > 2 and func.name[2].isupper():
        return True
    if func.name.startswith("fetchAndIngest") and func.name.endswith("Once"):
        return True
    return False


def _is_java_trivial_value_method(
    func: FunctionInfo, file_path: str, parent_class: ClassInfo | None, meta
) -> bool:
    if parent_class is None or not file_path.endswith(".java"):
        return False

    parent_name = parent_class.name
    declaration_text = _class_declaration_text(parent_class, meta)
    is_value_class = (
        _is_spring_managed_class(parent_class)
        or _is_java_record_class(parent_class, file_path, meta)
        or parent_name.startswith("Mutable")
        or parent_name.endswith(tuple(_JAVA_VALUE_CLASS_SUFFIXES))
        or " final class " in f" {declaration_text} "
    )
    if not is_value_class:
        return False

    body_lines = []
    if meta.lines:
        start = max(0, func.line_range.start - 1)
        end = min(len(meta.lines), func.line_range.end)
        body_lines = [line.strip() for line in meta.lines[start:end] if line.strip()]
    body_text = " ".join(body_lines)

    if func.name == "set" and parent_name.startswith("Mutable"):
        return True
    if func.name.startswith("set") and len(func.parameters) <= 1:
        return True
    if func.name.startswith(("get", "is")) and len(func.parameters) == 0:
        return True
    if len(func.parameters) == 0 and len(body_lines) <= 3 and "return " in body_text:
        return True
    return False


def _is_function_entry_point(
    func: FunctionInfo, file_path: str, parent_class: ClassInfo | None = None, meta=None
) -> bool:
    """Return True if this function should never be reported as dead code."""
    if _is_unsupported_file(file_path):
        return True
    if _is_jmh_file(file_path):
        return True
    if _is_test_file(file_path):
        return True
    if _is_init_file(file_path):
        return True
    if func.name in _ENTRY_POINT_NAMES:
        return True
    if func.name.startswith("test_"):
        return True
    if func.is_method and func.parent_class and func.name == func.parent_class:
        return True
    if parent_class is not None and meta is not None and _is_java_type_only_class(parent_class, file_path, meta):
        return True
    if _decorator_matches_keywords(func.decorators, _ENTRY_POINT_DECORATOR_KEYWORDS):
        return True

    decorator_names = _decorator_names(func.decorators)
    if decorator_names & (
        _SPRING_METHOD_DECORATORS | _JAVA_LIFECYCLE_DECORATORS | _JMH_METHOD_DECORATORS
    ):
        return True
    if _is_spring_managed_class(parent_class):
        return True
    if _is_java_dynamic_dispatch_method(func, parent_class):
        return True
    if _is_java_callback_like_method(func, parent_class):
        return True
    if meta is not None and _is_java_trivial_value_method(func, file_path, parent_class, meta):
        return True
    return False


def _is_class_entry_point(cls: ClassInfo, file_path: str, meta) -> bool:
    """Return True if this class should never be reported as dead code."""
    if _is_unsupported_file(file_path):
        return True
    if _is_jmh_file(file_path):
        return True
    if _is_test_file(file_path):
        return True
    if _is_init_file(file_path):
        return True
    if _decorator_matches_keywords(cls.decorators, _ENTRY_POINT_CLASS_DECORATOR_KEYWORDS):
        return True
    if _is_spring_managed_class(cls):
        return True
    if _decorator_names(cls.decorators) & _JMH_CLASS_DECORATORS:
        return True
    if file_path.endswith(".java") and _class_has_java_main_method(cls):
        return True
    if _is_java_type_only_class(cls, file_path, meta):
        return True
    return False


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dynamic-reachability pass
# ---------------------------------------------------------------------------
#
# The static call graph in ProjectIndex misses several Python call patterns
# that are common in this codebase: functions passed as dict values (annotator
# dispatch tables, handler maps), method calls routed through module-level
# singleton instances (``state._leiden.compute()``), and intra-class self.X
# chains whose seed entry only lands in the graph via one of the above.
#
# _dynamic_live_symbols does a lightweight textual pass over the project's
# Python files to mark as "live" anything reachable through those patterns.

_TOKEN_RE = re.compile(r"[A-Za-z_]\w*")
_CLASS_INSTANTIATION_RE = re.compile(r"\b([A-Z]\w*)\s*\(")
_METHOD_CALL_RE = re.compile(r"\.([A-Za-z_]\w*)\s*\(")
_SELF_CALL_RE = re.compile(r"\bself\.(\w+)\s*\(")
_DICT_VALUE_RE = re.compile(r":\s*([A-Za-z_]\w*)\s*[,}\n]")
_STRING_LITERAL_RE = re.compile(
    r"'''(?:[^\\]|\\.)*?'''"
    r'|"""(?:[^\\]|\\.)*?"""'
    r"|'(?:[^'\\\n]|\\.)*'"
    r'|"(?:[^"\\\n]|\\.)*"',
    re.DOTALL,
)


def _strip_string_literals(text: str) -> str:
    """Remove Python string literals before token-scanning for liveness.

    Without this, any symbol name that appears as a STRING (e.g. registry
    entries like ``("apps/…/legacy_helpers.py", "calculate_legacy_discount")``
    in a generator script) gets marked live, hiding real dead symbols. We
    only care about bare-identifier references, not name strings.
    """
    return _STRING_LITERAL_RE.sub("", text)


def _dynamic_live_symbols(
    index: ProjectIndex,
    rdg,
    pre_live: set[str],
) -> set[str]:
    """Mark symbols live via registration, dispatch, and singleton patterns."""
    live: set[str] = set()

    python_files = [(fp, meta) for fp, meta in index.files.items() if fp.endswith(".py")]
    if not python_files:
        return live

    file_text: dict[str, str] = {}
    file_tokens: dict[str, set[str]] = {}
    for fp, meta in python_files:
        text = "\n".join(meta.lines)
        file_text[fp] = text
        file_tokens[fp] = set(_TOKEN_RE.findall(_strip_string_literals(text)))

    toplevel_defs: dict[str, list[tuple[str, FunctionInfo]]] = {}
    class_by_simple: dict[str, list[tuple[str, ClassInfo]]] = {}

    for fp, meta in python_files:
        for func in meta.functions:
            if not func.is_method:
                toplevel_defs.setdefault(func.name, []).append((fp, func))
        for cls in meta.classes:
            qn = cls.qualified_name or cls.name
            class_by_simple.setdefault(cls.name, []).append((qn, cls))

    # Fix 1 — bare-identifier textual references reach top-level functions
    # passed as dict values, aliased-imports, decorator args, etc.
    dict_value_names: set[str] = set()
    for text in file_text.values():
        for m in _DICT_VALUE_RE.finditer(text):
            dict_value_names.add(m.group(1))
    for name, defs in toplevel_defs.items():
        def_files = {fp for fp, _ in defs}
        cross_file = False
        for fp, tokens in file_tokens.items():
            if fp in def_files:
                continue
            if name in tokens:
                cross_file = True
                break
        if cross_file or name in dict_value_names:
            for _fp, func in defs:
                live.add(func.qualified_name)
            live.add(name)

    # Fix 2 — any class that is instantiated anywhere has every method whose
    # simple name is called via attribute access marked live. This catches
    # module-level singletons (``state._leiden.compute()``) and factory-return
    # chains (``slot_manager._cache_mgr(root).save(index)``).
    instantiated: set[str] = set()
    called_method_names: set[str] = set()
    for text in file_text.values():
        for m in _CLASS_INSTANTIATION_RE.finditer(text):
            instantiated.add(m.group(1))
        for m in _METHOD_CALL_RE.finditer(text):
            called_method_names.add(m.group(1))

    for simple, clses in class_by_simple.items():
        if simple not in instantiated:
            continue
        for qn, cls in clses:
            live.add(qn)
            live.add(simple)
            for method in cls.methods:
                if method.name in called_method_names:
                    live.add(method.qualified_name)

    # Fix 3 — intra-class self.X closure. Any method in a class that is already
    # live (via rdg, pre_live, Fix 1, or Fix 2) pulls in any sibling method it
    # calls through ``self.X()`` -- transitively.
    for _fp, meta in python_files:
        for cls in meta.classes:
            method_names = {m.name for m in cls.methods}
            if not method_names:
                continue
            adjacency: dict[str, set[str]] = {}
            for m in cls.methods:
                start = max(0, m.line_range.start - 1)
                end = min(len(meta.lines), m.line_range.end)
                body_text = "\n".join(meta.lines[start:end])
                adjacency[m.name] = {
                    called
                    for called in _SELF_CALL_RE.findall(body_text)
                    if called in method_names
                }

            seeded: set[str] = set()
            for m in cls.methods:
                method_qn = m.qualified_name
                if (
                    method_qn in live
                    or method_qn in pre_live
                    or rdg.get(method_qn)
                    or rdg.get(m.name)
                ):
                    seeded.add(m.name)

            if not seeded:
                continue

            frontier = set(seeded)
            while frontier:
                next_frontier: set[str] = set()
                for name in frontier:
                    for target in adjacency.get(name, ()):
                        if target not in seeded:
                            seeded.add(target)
                            next_frontier.add(target)
                frontier = next_frontier

            name_to_qn = {m.name: m.qualified_name for m in cls.methods}
            for name in seeded:
                live.add(name_to_qn[name])

    return live


@dataclass
class _DeadSymbol:
    file_path: str
    line: int
    kind: str  # "function" or "class"
    name: str
    signature: str  # e.g. "unused_helper(x, y)" or just "OldProcessor"


def _collect_dead_symbols(
    index: ProjectIndex,
    sibling_indices: dict[str, ProjectIndex] | None = None,
) -> list[_DeadSymbol]:
    from token_savior.project_indexer import is_path_excluded_from_scans

    rdg = index.reverse_dependency_graph
    dead: list[_DeadSymbol] = []
    cross_project_live_symbols = _cross_project_live_symbols(index, sibling_indices)
    pre_live_symbols = cross_project_live_symbols
    signature_propagated_live_symbols = _collect_signature_propagated_live_symbols(
        index, pre_live_symbols
    )
    live_symbols = pre_live_symbols | signature_propagated_live_symbols
    live_symbols |= _dynamic_live_symbols(index, rdg, live_symbols)
    duplicate_classes, duplicate_methods = _duplicate_symbol_sets(index)

    for file_path, meta in index.files.items():
        if is_path_excluded_from_scans(file_path):
            continue
        class_by_name = {cls.name: cls for cls in meta.classes}

        for func in meta.functions:
            parent_class = class_by_name.get(func.parent_class or "")
            parent_qualified_name = (parent_class.qualified_name or parent_class.name) if parent_class else None
            if func.qualified_name in duplicate_methods or parent_qualified_name in duplicate_classes:
                continue
            if _is_function_entry_point(func, file_path, parent_class, meta):
                continue
            symbol_key = func.qualified_name
            callers = rdg.get(symbol_key) or rdg.get(func.name)
            if callers or symbol_key in live_symbols:
                continue
            params_str = ", ".join(func.parameters)
            signature = f"{func.name}({params_str})"
            dead.append(
                _DeadSymbol(
                    file_path=file_path,
                    line=func.line_range.start,
                    kind="function",
                    name=func.name,
                    signature=signature,
                )
            )

        for cls in meta.classes:
            if (cls.qualified_name or cls.name) in duplicate_classes:
                continue
            if _is_class_entry_point(cls, file_path, meta):
                continue
            qualified_name = cls.qualified_name or cls.name
            callers = rdg.get(qualified_name) or rdg.get(cls.name)
            if callers or qualified_name in live_symbols:
                continue
            dead.append(
                _DeadSymbol(
                    file_path=file_path,
                    line=cls.line_range.start,
                    kind="class",
                    name=cls.name,
                    signature=cls.name,
                )
            )

    _NAME_MARKERS = ("legacy", "deprecated", "unused", "old_", "stale", "orphan", "obsolete")
    _FILE_MARKERS = ("legacy", "deprecated", "unused", "stale", "orphan", "obsolete", "dead")

    def _cleanup_rank(sym: _DeadSymbol) -> tuple[int, str, int]:
        name_l = sym.name.lower()
        path_l = sym.file_path.lower()
        name_hit = any(mk in name_l for mk in _NAME_MARKERS)
        file_hit = any(mk in path_l for mk in _FILE_MARKERS)
        # Tier 0 = both markers agree (strongest signal, e.g. legacy_foo in legacy.py).
        # Tier 1 = file is a cleanup file (every symbol inside is suspect).
        # Tier 2 = only name suggests cleanup (weaker: could be a stray "old_" match).
        # Tier 3 = neither (the long tail).
        if name_hit and file_hit:
            tier = 0
        elif file_hit:
            tier = 1
        elif name_hit:
            tier = 2
        else:
            tier = 3
        return (tier, sym.file_path, sym.line)

    dead.sort(key=_cleanup_rank)
    return dead


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_dead_code(
    index: ProjectIndex,
    max_results: int = 50,
    sibling_indices: dict[str, ProjectIndex] | None = None,
) -> str:
    """Analyse *index* and return a formatted dead-code report.

    Parameters
    ----------
    index:
        The ProjectIndex produced by the project indexer.
    max_results:
        Maximum number of dead symbols to include in the output (default 50).
        The header always shows the true total count.

    Returns
    -------
    str
        Multi-line report string.
    """
    all_dead = _collect_dead_symbols(index, sibling_indices=sibling_indices)
    total = len(all_dead)
    shown = all_dead[:max_results]

    symbol_word = "symbol" if total == 1 else "symbols"
    lines: list[str] = [
        f"Dead Code Analysis -- {total} unreferenced {symbol_word} found",
        "",
    ]

    if not shown:
        return lines[0]

    current_file: str | None = None
    for sym in shown:
        if sym.file_path != current_file:
            current_file = sym.file_path
            lines.append(f"{sym.file_path}:")
        lines.append(f"  line {sym.line}: {sym.kind} {sym.signature}")

    return "\n".join(lines)
