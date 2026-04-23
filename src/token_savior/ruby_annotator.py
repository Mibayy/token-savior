"""Tree-sitter-based Ruby annotator."""

from __future__ import annotations

import tree_sitter_ruby
from tree_sitter import Language, Node, Parser

from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    ImportInfo,
    LineRange,
    StructuralMetadata,
    build_line_char_offsets,
)

_RUBY_LANGUAGE = Language(tree_sitter_ruby.language())

_VISIBILITY_KEYWORDS = frozenset({"private", "protected", "public"})


def _node_text(node: Node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _declaration_line(node: Node) -> int:
    return node.start_point.row + 1


def _extract_string_arg(call_node: Node, source_bytes: bytes) -> str | None:
    """Extract the string value from a single-arg call like require 'foo'."""
    arg_list = call_node.child_by_field_name("arguments")
    if arg_list is None:
        return None
    for child in arg_list.named_children:
        if child.type == "string":
            content = child.child_by_field_name("content")
            if content is not None:
                return _node_text(content, source_bytes)
            # fallback: strip surrounding quotes from raw text
            raw = _node_text(child, source_bytes)
            if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] in ("'", '"'):
                return raw[1:-1]
    return None


def _extract_constant_arg(call_node: Node, source_bytes: bytes) -> str | None:
    """Extract the constant / scope_resolution value from include Foo or include Foo::Bar."""
    arg_list = call_node.child_by_field_name("arguments")
    if arg_list is None:
        return None
    for child in arg_list.named_children:
        if child.type in {"constant", "scope_resolution"}:
            return _node_text(child, source_bytes)
    return None


def _collect_docstring(lines: list[str], decl_line_0: int) -> str | None:
    """Collect a leading # comment block immediately before decl_line_0 (0-indexed)."""
    doc_lines: list[str] = []
    idx = decl_line_0 - 1
    while idx >= 0:
        stripped = lines[idx].strip()
        if stripped.startswith("#"):
            doc_lines.append(stripped.lstrip("#").strip())
            idx -= 1
        else:
            break
    return "\n".join(reversed(doc_lines)) if doc_lines else None


def _method_params(node: Node, source_bytes: bytes) -> list[str]:
    """Extract parameter names from a method or singleton_method node."""
    params_node = node.child_by_field_name("parameters")
    if params_node is None:
        return []
    names: list[str] = []
    for child in params_node.named_children:
        if child.type in {
            "identifier",
            "optional_parameter",
            "splat_parameter",
            "hash_splat_parameter",
            "block_parameter",
            "keyword_parameter",
        }:
            if child.type == "identifier":
                names.append(_node_text(child, source_bytes))
            else:
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    prefix = ""
                    if child.type == "splat_parameter":
                        prefix = "*"
                    elif child.type == "hash_splat_parameter":
                        prefix = "**"
                    elif child.type == "block_parameter":
                        prefix = "&"
                    suffix = ":" if child.type == "keyword_parameter" else ""
                    names.append(prefix + _node_text(name_node, source_bytes) + suffix)
    return names


def _build_function_info(
    node: Node,
    parent_class: str | None,
    parent_qualified: str | None,
    lines: list[str],
    source_bytes: bytes,
    visibility: str,
    *,
    is_singleton: bool = False,
) -> FunctionInfo | None:
    """Build a FunctionInfo from a method or singleton_method node."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _node_text(name_node, source_bytes)
    display_name = f"self.{name}" if is_singleton else name

    params = _method_params(node, source_bytes)
    decl_line_0 = node.start_point.row
    docstring = _collect_docstring(lines, decl_line_0)
    qualified_name = f"{parent_qualified}.{display_name}" if parent_qualified else display_name
    is_method = parent_class is not None

    return FunctionInfo(
        name=display_name,
        qualified_name=qualified_name,
        line_range=LineRange(start=decl_line_0 + 1, end=node.end_point.row + 1),
        parameters=params,
        decorators=[],
        docstring=docstring,
        is_method=is_method,
        parent_class=parent_class,
        visibility=visibility,
    )


def _emit_mixin_import(
    call_node: Node, source_bytes: bytes, imports: list[ImportInfo]
) -> None:
    """Append an ImportInfo for an include/extend call node."""
    mod = _extract_constant_arg(call_node, source_bytes)
    if mod is not None:
        imports.append(
            ImportInfo(
                module=mod,
                names=["*"],
                alias=None,
                line_number=_declaration_line(call_node),
                is_from_import=True,
            )
        )


def _walk_body(
    body_node: Node | None,
    parent_class: str | None,
    parent_qualified: str | None,
    lines: list[str],
    source_bytes: bytes,
    default_visibility: str,
) -> tuple[list[ClassInfo], list[FunctionInfo], list[ImportInfo]]:
    """Recursively walk a body_statement node, tracking visibility state."""
    functions: list[FunctionInfo] = []
    classes: list[ClassInfo] = []
    imports: list[ImportInfo] = []

    if body_node is None:
        return classes, functions, imports

    current_visibility = default_visibility

    for child in body_node.named_children:
        node_type = child.type

        if node_type == "identifier":
            text = _node_text(child, source_bytes)
            if text in _VISIBILITY_KEYWORDS:
                current_visibility = text
            continue

        # `private def foo` / `protected def foo` — call with visibility keyword + method arg
        # Also handles `include`/`extend` inside class/module bodies
        if node_type == "call":
            method_node = child.child_by_field_name("method")
            if method_node is not None:
                method_text = _node_text(method_node, source_bytes)
                if method_text in _VISIBILITY_KEYWORDS:
                    arg_list = child.child_by_field_name("arguments")
                    if arg_list is not None:
                        for arg in arg_list.named_children:
                            if arg.type == "method":
                                fn = _build_function_info(
                                    arg,
                                    parent_class,
                                    parent_qualified,
                                    lines,
                                    source_bytes,
                                    method_text,
                                    is_singleton=False,
                                )
                                if fn is not None:
                                    functions.append(fn)
                            elif arg.type == "singleton_method":
                                fn = _build_function_info(
                                    arg,
                                    parent_class,
                                    parent_qualified,
                                    lines,
                                    source_bytes,
                                    method_text,
                                    is_singleton=True,
                                )
                                if fn is not None:
                                    functions.append(fn)
                    # Do NOT update current_visibility — inline form only affects that method
                    continue

                if method_text in {"include", "extend"}:
                    _emit_mixin_import(child, source_bytes, imports)
                    continue

            # Other call nodes — skip
            continue

        if node_type == "method":
            fn = _build_function_info(
                child,
                parent_class,
                parent_qualified,
                lines,
                source_bytes,
                current_visibility,
                is_singleton=False,
            )
            if fn is not None:
                functions.append(fn)
            continue

        if node_type == "singleton_method":
            fn = _build_function_info(
                child,
                parent_class,
                parent_qualified,
                lines,
                source_bytes,
                current_visibility,
                is_singleton=True,
            )
            if fn is not None:
                functions.append(fn)
            continue

        if node_type in {"class", "module"}:
            nested_classes, nested_fns, nested_imports = _extract_type_node(
                child, parent_qualified, lines, source_bytes
            )
            classes.extend(nested_classes)
            functions.extend(nested_fns)
            imports.extend(nested_imports)
            continue

    return classes, functions, imports


def _extract_type_node(
    node: Node,
    parent_qualified: str | None,
    lines: list[str],
    source_bytes: bytes,
) -> tuple[list[ClassInfo], list[FunctionInfo], list[ImportInfo]]:
    """Extract a class or module node into ClassInfo + FunctionInfo + ImportInfo lists."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return [], [], []

    name = _node_text(name_node, source_bytes)
    qualified_name = f"{parent_qualified}.{name}" if parent_qualified else name

    base_classes: list[str] = []
    if node.type == "class":
        superclass_node = node.child_by_field_name("superclass")
        if superclass_node is not None:
            for child in superclass_node.named_children:
                if child.type in {"constant", "scope_resolution"}:
                    base_classes.append(_node_text(child, source_bytes))
                    break

    decl_line_0 = node.start_point.row
    docstring = _collect_docstring(lines, decl_line_0)

    body_node = node.child_by_field_name("body")

    nested_classes, methods, body_imports = _walk_body(
        body_node,
        name,
        qualified_name,
        lines,
        source_bytes,
        "public",
    )

    class_info = ClassInfo(
        name=name,
        line_range=LineRange(start=decl_line_0 + 1, end=node.end_point.row + 1),
        base_classes=base_classes,
        methods=methods,
        decorators=[],
        docstring=docstring,
        qualified_name=qualified_name,
        visibility="public",
    )

    return [class_info] + nested_classes, methods, body_imports


def _parse_top_level(
    root: Node,
    lines: list[str],
    source_bytes: bytes,
) -> tuple[list[ClassInfo], list[FunctionInfo], list[ImportInfo]]:
    """Walk the top-level program node."""
    classes: list[ClassInfo] = []
    functions: list[FunctionInfo] = []
    imports: list[ImportInfo] = []

    for child in root.named_children:
        node_type = child.type

        if node_type == "call":
            method_node = child.child_by_field_name("method")
            if method_node is None:
                continue
            method_text = _node_text(method_node, source_bytes)

            if method_text in {"require", "require_relative"}:
                mod = _extract_string_arg(child, source_bytes)
                if mod is not None:
                    imports.append(
                        ImportInfo(
                            module=mod,
                            names=[],
                            alias=None,
                            line_number=_declaration_line(child),
                            is_from_import=False,
                        )
                    )
                continue

            if method_text in {"include", "extend"}:
                _emit_mixin_import(child, source_bytes, imports)
                continue

        elif node_type in {"class", "module"}:
            found_classes, found_fns, found_imports = _extract_type_node(child, None, lines, source_bytes)
            classes.extend(found_classes)
            functions.extend(found_fns)
            imports.extend(found_imports)

        elif node_type == "method":
            fn = _build_function_info(
                child,
                None,
                None,
                lines,
                source_bytes,
                "public",
                is_singleton=False,
            )
            if fn is not None:
                functions.append(fn)

        elif node_type == "singleton_method":
            fn = _build_function_info(
                child,
                None,
                None,
                lines,
                source_bytes,
                "public",
                is_singleton=True,
            )
            if fn is not None:
                functions.append(fn)

    return classes, functions, imports


def annotate_ruby(source: str, source_name: str = "<source>") -> StructuralMetadata:
    """Parse Ruby source and extract structural metadata using tree-sitter."""
    lines = source.split("\n")
    total_lines = len(lines)
    total_chars = len(source)
    line_offsets = build_line_char_offsets(lines)
    source_bytes = source.encode("utf-8")

    parser = Parser(_RUBY_LANGUAGE)
    tree = parser.parse(source_bytes)
    root = tree.root_node

    classes, functions, imports = _parse_top_level(root, lines, source_bytes)

    return StructuralMetadata(
        source_name=source_name,
        total_lines=total_lines,
        total_chars=total_chars,
        lines=lines,
        line_char_offsets=line_offsets,
        functions=functions,
        classes=classes,
        imports=imports,
    )
