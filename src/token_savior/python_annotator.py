"""AST-based Python file annotator.

Uses Python's ast module to extract structural information from Python source code:
functions, classes, imports, and a best-effort dependency graph.
"""

import ast
import logging

from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    ImportInfo,
    LineRange,
    StructuralMetadata,
    build_line_char_offsets,
)

logger = logging.getLogger(__name__)


def _decorator_name(node: ast.expr) -> str:
    """Extract a readable name from a decorator AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ast.dump(node)


def _base_name(node: ast.expr) -> str:
    """Extract a readable name from a base class AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    if isinstance(node, ast.Subscript):
        # e.g., Generic[T]
        return _base_name(node.value)
    return ast.dump(node)


def _extract_function_info(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    parent_class: str | None = None,
) -> FunctionInfo:
    """Extract FunctionInfo from a function/method AST node."""
    is_method = parent_class is not None
    qualified_name = f"{parent_class}.{node.name}" if parent_class else node.name

    # Parameters: skip self/cls for methods
    params: list[str] = []
    for arg in node.args.args:
        if is_method and arg.arg in ("self", "cls"):
            continue
        params.append(arg.arg)
    # Also include *args and **kwargs style params
    if node.args.vararg:
        params.append(f"*{node.args.vararg.arg}")
    for arg in node.args.kwonlyargs:
        params.append(arg.arg)
    if node.args.kwarg:
        params.append(f"**{node.args.kwarg.arg}")

    decorators = [_decorator_name(d) for d in node.decorator_list]
    docstring = ast.get_docstring(node)

    # Line range: account for decorators
    start_line = node.lineno
    if node.decorator_list:
        start_line = min(d.lineno for d in node.decorator_list)

    return FunctionInfo(
        name=node.name,
        qualified_name=qualified_name,
        line_range=LineRange(start=start_line, end=node.end_lineno or node.lineno),
        parameters=params,
        decorators=decorators,
        docstring=docstring,
        is_method=is_method,
        parent_class=parent_class,
    )


def _extract_class_info(node: ast.ClassDef) -> ClassInfo:
    """Extract ClassInfo from a class AST node."""
    base_classes = [_base_name(b) for b in node.bases]
    decorators = [_decorator_name(d) for d in node.decorator_list]
    docstring = ast.get_docstring(node)

    methods: list[FunctionInfo] = []
    for child in ast.walk(node):
        if child is node:
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Only direct methods (check parent is this class)
            pass

    # Use iter_child_nodes to get only direct children
    methods = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.append(_extract_function_info(child, parent_class=node.name))

    start_line = node.lineno
    if node.decorator_list:
        start_line = min(d.lineno for d in node.decorator_list)

    return ClassInfo(
        name=node.name,
        line_range=LineRange(start=start_line, end=node.end_lineno or node.lineno),
        base_classes=base_classes,
        methods=methods,
        decorators=decorators,
        docstring=docstring,
    )


def _collect_name_references(node: ast.AST) -> set[str]:
    """Collect all Name and Attribute references in an AST subtree."""
    refs: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            refs.add(child.id)
        elif isinstance(child, ast.Attribute):
            # Collect the full dotted name's root
            current: ast.expr = child
            while isinstance(current, ast.Attribute):
                current = current.value
            if isinstance(current, ast.Name):
                refs.add(current.id)
    return refs


def _build_dependency_graph(
    tree: ast.Module,
    defined_names: set[str],
) -> dict[str, list[str]]:
    """Build a dependency graph mapping each defined name to the other defined names it references.

    For classes, we use the class name as the key and collect references from the
    entire class body. For functions, we collect references from the function body.
    """
    graph: dict[str, list[str]] = {}

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            refs = _collect_name_references(node)
            # Remove self-reference and keep only defined names
            deps = sorted(refs & defined_names - {node.name})
            graph[node.name] = deps

        elif isinstance(node, ast.ClassDef):
            refs = _collect_name_references(node)
            deps = sorted(refs & defined_names - {node.name})
            graph[node.name] = deps

    return graph


def _extract_imports(tree: ast.Module) -> list[ImportInfo]:
    """Extract all import statements from the AST."""
    imports: list[ImportInfo] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(
                    ImportInfo(
                        module=alias.name,
                        names=[],
                        alias=alias.asname,
                        line_number=node.lineno,
                        is_from_import=False,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [alias.name for alias in node.names]
            # For `from X import Y as Z`, we record Y in names and Z as alias
            # But ImportInfo has a single alias field - use it for single-name imports
            alias = None
            if len(node.names) == 1 and node.names[0].asname:
                alias = node.names[0].asname
            imports.append(
                ImportInfo(
                    module=module,
                    names=names,
                    alias=alias,
                    line_number=node.lineno,
                    is_from_import=True,
                )
            )

    return imports


def annotate_python(source: str, source_name: str = "<source>") -> StructuralMetadata:
    """Parse Python source code and extract structural metadata.

    Uses ast.parse() to build the AST, then walks it to extract:
    - All function definitions (top-level and methods, including async)
    - All class definitions with base classes, methods, decorators, docstrings
    - All import statements (import X, from X import Y, aliases)
    - A dependency graph mapping each function/class to the names it references

    If the source fails to parse (syntax error), falls back to line-only annotation
    (empty functions/classes/imports lists) and logs a warning.

    Args:
        source: Python source code as a string.
        source_name: Identifier for error messages.

    Returns:
        StructuralMetadata with code structure populated.
    """
    lines = source.split("\n")
    line_offsets = build_line_char_offsets(lines)
    total_lines = len(lines)
    total_chars = len(source)

    # Attempt to parse the AST
    try:
        tree = ast.parse(source, filename=source_name)
    except SyntaxError as e:
        logger.warning(
            "Syntax error in %s: %s. Falling back to line-only annotation.", source_name, e
        )
        return StructuralMetadata(
            source_name=source_name,
            total_lines=total_lines,
            total_chars=total_chars,
            lines=lines,
            line_char_offsets=line_offsets,
        )

    # Extract top-level functions (not methods)
    functions: list[FunctionInfo] = []
    classes: list[ClassInfo] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_extract_function_info(node, parent_class=None))
        elif isinstance(node, ast.ClassDef):
            class_info = _extract_class_info(node)
            classes.append(class_info)
            # Also add methods to the top-level functions list for easy lookup
            functions.extend(class_info.methods)

    # Extract imports
    imports = _extract_imports(tree)

    # Build dependency graph
    defined_names: set[str] = set()
    for f in functions:
        defined_names.add(f.name)
    for c in classes:
        defined_names.add(c.name)

    dependency_graph = _build_dependency_graph(tree, defined_names)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        functions=functions,
        classes=classes,
        imports=imports,
        dependency_graph=dependency_graph,
    )
