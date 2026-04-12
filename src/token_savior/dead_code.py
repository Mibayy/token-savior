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
_METHOD_REFERENCE_RE = re.compile(r"(?<![\w$])([A-Za-z_][\w.]*)::([A-Za-z_]\w*)")


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


def _find_enclosing_class(meta, line_number: int) -> ClassInfo | None:
    enclosing = [
        cls
        for cls in meta.classes
        if cls.line_range.start <= line_number <= cls.line_range.end
    ]
    if not enclosing:
        return None
    return min(enclosing, key=lambda cls: cls.line_range.end - cls.line_range.start)


def _class_matches_owner_token(cls: ClassInfo, owner_token: str) -> bool:
    qualified_name = cls.qualified_name or cls.name
    return (
        owner_token == cls.name
        or owner_token == qualified_name
        or qualified_name.endswith(f".{owner_token}")
    )


def _collect_method_reference_live_symbols(index: ProjectIndex) -> set[str]:
    live_symbols: set[str] = set()
    for _, meta in index.files.items():
        if not meta.lines:
            continue
        for line_number, line in enumerate(meta.lines, start=1):
            for owner_token, method_name in _METHOD_REFERENCE_RE.findall(line):
                if owner_token in {"this", "super"}:
                    matching_classes: list[ClassInfo] = []
                    enclosing_class = _find_enclosing_class(meta, line_number)
                    if enclosing_class is not None:
                        matching_classes.append(enclosing_class)
                else:
                    matching_classes = [
                        cls for cls in meta.classes if _class_matches_owner_token(cls, owner_token)
                    ]
                for cls in matching_classes:
                    qualified_name = cls.qualified_name or cls.name
                    live_symbols.add(qualified_name)
                    for method in cls.methods:
                        if method.name == method_name:
                            live_symbols.add(method.qualified_name)
    return live_symbols


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
    if _is_java_type_only_class(cls, file_path, meta):
        return True
    return False


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


@dataclass
class _DeadSymbol:
    file_path: str
    line: int
    kind: str  # "function" or "class"
    name: str
    signature: str  # e.g. "unused_helper(x, y)" or just "OldProcessor"


def _collect_dead_symbols(index: ProjectIndex) -> list[_DeadSymbol]:
    rdg = index.reverse_dependency_graph
    dead: list[_DeadSymbol] = []
    live_method_reference_symbols = _collect_method_reference_live_symbols(index)
    signature_propagated_live_symbols = _collect_signature_propagated_live_symbols(
        index, live_method_reference_symbols
    )
    live_symbols = live_method_reference_symbols | signature_propagated_live_symbols

    for file_path, meta in index.files.items():
        class_by_name = {cls.name: cls for cls in meta.classes}

        for func in meta.functions:
            parent_class = class_by_name.get(func.parent_class or "")
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

    dead.sort(key=lambda s: (s.file_path, s.line))
    return dead


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_dead_code(index: ProjectIndex, max_results: int = 50) -> str:
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
    all_dead = _collect_dead_symbols(index)
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
