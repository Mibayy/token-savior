"""Tree-sitter-based Java annotator."""

from __future__ import annotations

import re
from os.path import basename

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    ImportInfo,
    LineRange,
    SectionInfo,
    StructuralMetadata,
    build_line_char_offsets,
)

_JAVA_LANGUAGE = Language(tree_sitter_java.language())
_TYPE_NODE_KINDS = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    }
)
_METHOD_NODE_KINDS = {
    "method_declaration",
    "constructor_declaration",
    "compact_constructor_declaration",
    "annotation_type_element_declaration",
}
_IMPORT_RE = re.compile(r"^import\s+(static\s+)?([A-Za-z_][\w.]*)(\.\*)?\s*;$")
_PACKAGE_RE = re.compile(r"^package\s+([A-Za-z_][\w.]*)\s*;$")
_ANNOTATION_LINE_RE = re.compile(r"^\s*@([\w.]+)")
_SIMPLE_TYPE_TOKEN_RE = re.compile(r"[A-Z][A-Za-z0-9_]*")
_LOCAL_SCOPE_PREFIX = "::<local>"
_COMMON_JAVA_TYPES = frozenset(
    {
        "Boolean",
        "Byte",
        "Character",
        "Class",
        "Collection",
        "Collections",
        "Comparable",
        "Double",
        "Enum",
        "Exception",
        "Float",
        "Integer",
        "Iterable",
        "List",
        "Long",
        "Map",
        "Math",
        "Object",
        "Objects",
        "Optional",
        "Override",
        "Runnable",
        "Set",
        "Short",
        "String",
        "SuppressWarnings",
        "System",
        "Thread",
        "Throwable",
        "Void",
    }
)
_JAVA_EXECUTOR_TYPE_SUFFIXES = (
    "Executor",
    "ExecutorService",
    "ScheduledExecutor",
    "ScheduledExecutorService",
    "ThreadPoolExecutor",
    "ForkJoinPool",
)


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _node_text(node: Node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")



def _declaration_line(node: Node) -> int:
    return node.start_point.row + 1


def _clean_javadoc_line(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("/**"):
        stripped = stripped[3:]
    elif stripped.startswith("/*"):
        stripped = stripped[2:]
    if stripped.endswith("*/"):
        stripped = stripped[:-2]
    if stripped.startswith("*"):
        stripped = stripped[1:]
    return stripped.strip()


def _collect_leading_metadata(lines: list[str], decl_line_0: int) -> tuple[list[str], int, str | None]:
    decorators: list[str] = []
    docstring: str | None = None
    start_line_0 = decl_line_0
    idx = decl_line_0 - 1

    while idx >= 0:
        stripped = lines[idx].strip()
        if not stripped:
            break

        annotation_match = _ANNOTATION_LINE_RE.match(stripped)
        if annotation_match:
            decorators.insert(0, annotation_match.group(1).split(".")[-1])
            start_line_0 = idx
            idx -= 1
            continue

        if stripped.endswith("*/"):
            raw_doc_lines: list[str] = [lines[idx]]
            start_doc_idx = idx
            idx -= 1
            while idx >= 0:
                raw_doc_lines.insert(0, lines[idx])
                start_doc_idx = idx
                line_stripped = lines[idx].strip()
                if line_stripped.startswith("/**"):
                    break
                if line_stripped.startswith("/*") and not line_stripped.startswith("/**"):
                    raw_doc_lines = []
                    break
                idx -= 1
            if raw_doc_lines:
                cleaned = [_clean_javadoc_line(line) for line in raw_doc_lines]
                cleaned = [line for line in cleaned if line]
                docstring = "\n".join(cleaned) if cleaned else None
                start_line_0 = min(start_line_0, start_doc_idx)
            break

        break

    return decorators, start_line_0, docstring


def _parse_imports(nodes: list[Node], source_bytes: bytes) -> list[ImportInfo]:
    imports: list[ImportInfo] = []
    for node in nodes:
        text = _normalize_ws(_node_text(node, source_bytes))
        match = _IMPORT_RE.match(text)
        if not match:
            continue
        is_static = bool(match.group(1))
        target = match.group(2)
        is_wildcard = bool(match.group(3))
        if is_wildcard:
            module = target
            names = ["*"]
        elif is_static:
            owner, _, member = target.rpartition(".")
            module = owner or target
            names = [member] if owner else []
        else:
            module = target
            names = [target.rsplit(".", 1)[-1]]
        imports.append(
            ImportInfo(
                module=module,
                names=names,
                alias=None,
                line_number=_declaration_line(node),
                is_from_import=is_static,
            )
        )
    return imports


def _parse_package_name(nodes: list[Node], source_bytes: bytes) -> str | None:
    for node in nodes:
        if node.type != "package_declaration":
            continue
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            return _node_text(name_node, source_bytes)
        for child in node.named_children:
            if child.type in {"identifier", "scoped_identifier"}:
                return _node_text(child, source_bytes)
        match = _PACKAGE_RE.match(_normalize_ws(_node_text(node, source_bytes)))
        if match:
            return match.group(1)
    return None


def _extract_annotations(node: Node, source_bytes: bytes) -> list[tuple[str, str]]:
    annotations: list[tuple[str, str]] = []
    for child in node.children:
        if child.type in {"marker_annotation", "annotation"}:
            name_node = child.child_by_field_name("name")
            if name_node is None:
                for grandchild in child.named_children:
                    if grandchild.type in {"identifier", "scoped_identifier"}:
                        name_node = grandchild
                        break
            if name_node is not None:
                name = _node_text(name_node, source_bytes).split(".")[-1]
                text = _node_text(child, source_bytes)
                detail = ""
                if "(" in text and text.rstrip().endswith(")"):
                    _, _, raw_detail = text.partition("(")
                    detail = raw_detail[:-1]
                annotations.append((name, detail))
    return annotations


def _extract_annotation_names(node: Node, source_bytes: bytes) -> list[str]:
    return [name for name, _ in _extract_annotations(node, source_bytes)]


def _modifier_tokens(node: Node | None, source_bytes: bytes) -> set[str]:
    if node is None:
        return set()
    text = _node_text(node, source_bytes)
    return {
        token
        for token in ("public", "protected", "private", "static", "final", "abstract")
        if re.search(rf"\b{token}\b", text)
    }


def _visibility_from_modifiers(node: Node | None, source_bytes: bytes) -> str:
    modifiers = _modifier_tokens(node, source_bytes)
    if "public" in modifiers:
        return "public"
    if "protected" in modifiers:
        return "protected"
    if "private" in modifiers:
        return "private"
    return "package"


def _return_type_text(node: Node, source_bytes: bytes) -> str | None:
    type_node = node.child_by_field_name("type")
    if type_node is not None:
        return _normalize_ws(_node_text(type_node, source_bytes))
    return None


def _declaration_metadata(
    node: Node, lines: list[str], source_bytes: bytes
) -> tuple[list[str], dict[str, str], int, str | None]:
    decl_line_0 = node.start_point.row
    fallback_decorators, start_line_0, docstring = _collect_leading_metadata(lines, decl_line_0)
    modifiers = next((child for child in node.children if child.type == "modifiers"), None)
    annotation_items = _extract_annotations(modifiers, source_bytes) if modifiers else []
    decorators = [name for name, _ in annotation_items] or fallback_decorators
    decorator_details = {name: detail for name, detail in annotation_items if detail}
    return decorators, decorator_details, start_line_0, docstring


def _parse_declared_type_names(text: str) -> list[str]:
    names: list[str] = []
    for keyword in ("extends", "implements", "permits"):
        if f" {keyword} " not in f" {text} ":
            continue
        _, _, remainder = text.partition(keyword)
        for part in remainder.split(","):
            for token in _SIMPLE_TYPE_TOKEN_RE.findall(part):
                if token not in _COMMON_JAVA_TYPES or token in {"Runnable", "Thread"}:
                    names.append(token)
                    break
    return names


def _type_base_classes(node: Node, source_bytes: bytes) -> list[str]:
    pieces: list[str] = []
    for field_name in ("superclass", "interfaces", "super_interfaces", "permits"):
        child = node.child_by_field_name(field_name)
        if child is not None:
            pieces.append(_node_text(child, source_bytes))
    return _parse_declared_type_names(" ".join(pieces))


def _split_parameters(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    if not raw.strip():
        return []

    params: list[str] = []
    current: list[str] = []
    angle_depth = 0
    paren_depth = 0
    bracket_depth = 0
    for ch in raw:
        if ch == "<":
            angle_depth += 1
        elif ch == ">":
            angle_depth = max(0, angle_depth - 1)
        elif ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif ch == "," and angle_depth == 0 and paren_depth == 0 and bracket_depth == 0:
            piece = "".join(current).strip()
            if piece:
                params.append(piece)
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        params.append(tail)
    return params


def _extract_params(raw: str) -> list[str]:
    params = _split_parameters(raw)
    names: list[str] = []
    for part in params:
        cleaned = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", part)
        cleaned = cleaned.split("=", 1)[0].strip()
        cleaned = re.sub(r"\bfinal\s+", "", cleaned)
        tokens = cleaned.split()
        if not tokens:
            continue
        last = tokens[-1]
        if last == "this" and len(tokens) >= 2:
            last = tokens[-2]
        if last.endswith("this") and "." in last:
            last = last.rsplit(".", 1)[0]
        if last.startswith("..."):
            last = last[3:]
        last = last.lstrip("*").rstrip("[]")
        names.append(last)
    return names


def _extract_param_types(raw: str) -> list[str]:
    params = _split_parameters(raw)
    param_types: list[str] = []
    for part in params:
        cleaned = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", part)
        cleaned = cleaned.split("=", 1)[0].strip()
        cleaned = re.sub(r"\bfinal\s+", "", cleaned)
        tokens = cleaned.split()
        if len(tokens) < 2:
            continue
        type_text = " ".join(tokens[:-1]).strip()
        if type_text:
            param_types.append(type_text.replace(" ,", ","))
    return param_types


def _build_method_symbol(owner_qualified_name: str, name: str, param_types: list[str]) -> str:
    return f"{owner_qualified_name}.{name}({','.join(param_types)})"


def _method_body_node(node: Node) -> Node | None:
    body_node = node.child_by_field_name("body")
    if body_node is not None:
        return body_node
    return next((child for child in node.named_children if child.type == "block"), None)


def _local_scope_qualified_name(scope_qualified_name: str, local_name: str) -> str:
    return f"{scope_qualified_name}{_LOCAL_SCOPE_PREFIX}.{local_name}"


def _method_info(
    node: Node,
    owner_name: str,
    owner_qualified_name: str,
    lines: list[str],
    source_bytes: bytes,
) -> FunctionInfo | None:
    if node.type == "annotation_type_element_declaration":
        name_node = node.child_by_field_name("name")
        parameters_node = node.child_by_field_name("parameters")
    elif node.type == "compact_constructor_declaration":
        name_node = node.child_by_field_name("name")
        parameters_node = None
    else:
        name_node = node.child_by_field_name("name")
        parameters_node = node.child_by_field_name("parameters")

    if name_node is None:
        return None

    name = _node_text(name_node, source_bytes)
    raw_params = _node_text(parameters_node, source_bytes) if parameters_node is not None else ""
    if node.type == "compact_constructor_declaration":
        raw_params = ""
    params = _extract_params(raw_params)
    param_types = _extract_param_types(raw_params)
    modifiers = next((child for child in node.children if child.type == "modifiers"), None)

    decorators, decorator_details, start_line_0, docstring = _declaration_metadata(
        node, lines, source_bytes
    )
    return FunctionInfo(
        name=name,
        qualified_name=_build_method_symbol(owner_qualified_name, name, param_types),
        line_range=LineRange(start=start_line_0 + 1, end=node.end_point.row + 1),
        parameters=params,
        decorators=decorators,
        docstring=docstring,
        is_method=True,
        parent_class=owner_name,
        decorator_details=decorator_details,
        visibility=_visibility_from_modifiers(modifiers, source_bytes),
        return_type=_return_type_text(node, source_bytes),
    )


def _body_children(body_node: Node | None) -> list[Node]:
    if body_node is None:
        return []
    result: list[Node] = []
    for child in body_node.named_children:
        if child.type == "enum_body_declarations":
            result.extend(list(child.named_children))
        else:
            result.append(child)
    return result


def _extract_local_type_trees(
    node: Node | None,
    package: str | None,
    scope_qualified_name: str,
    lines: list[str],
    source_bytes: bytes,
    method_nodes: dict[str, Node],
    field_types_by_class: dict[str, dict[str, str]],
) -> tuple[list[ClassInfo], list[FunctionInfo]]:
    if node is None:
        return [], []

    local_classes: list[ClassInfo] = []
    local_functions: list[FunctionInfo] = []
    for child in node.named_children:
        if child.type in _TYPE_NODE_KINDS:
            child_name_node = child.child_by_field_name("name")
            if child_name_node is None:
                continue
            child_name = _node_text(child_name_node, source_bytes)
            child_classes, child_functions = _extract_type_tree(
                child,
                package,
                _local_scope_qualified_name(scope_qualified_name, child_name).rsplit(".", 1)[0],
                lines,
                source_bytes,
                method_nodes,
                field_types_by_class,
            )
            local_classes.extend(child_classes)
            local_functions.extend(child_functions)
            continue

        child_classes, child_functions = _extract_local_type_trees(
            child,
            package,
            scope_qualified_name,
            lines,
            source_bytes,
            method_nodes,
            field_types_by_class,
        )
        local_classes.extend(child_classes)
        local_functions.extend(child_functions)

    return local_classes, local_functions


def _extract_type_tree(
    node: Node,
    package: str | None,
    parent_qualified_name: str | None,
    lines: list[str],
    source_bytes: bytes,
    method_nodes: dict[str, Node],
    field_types_by_class: dict[str, dict[str, str]],
) -> tuple[list[ClassInfo], list[FunctionInfo]]:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return [], []
    name = _node_text(name_node, source_bytes)
    qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else f"{package}.{name}" if package else name
    decorators, decorator_details, start_line_0, docstring = _declaration_metadata(
        node, lines, source_bytes
    )
    modifiers = next((child for child in node.children if child.type == "modifiers"), None)
    body_node = node.child_by_field_name("body")
    field_types_by_class[qualified_name] = _collect_field_types(body_node, source_bytes)

    direct_methods: list[FunctionInfo] = []
    nested_classes: list[ClassInfo] = []
    nested_functions: list[FunctionInfo] = []
    for child in _body_children(body_node):
        if child.type in _TYPE_NODE_KINDS:
            child_classes, child_functions = _extract_type_tree(
                child,
                package,
                qualified_name,
                lines,
                source_bytes,
                method_nodes,
                field_types_by_class,
            )
            nested_classes.extend(child_classes)
            nested_functions.extend(child_functions)
        elif child.type in _METHOD_NODE_KINDS:
            method = _method_info(child, name, qualified_name, lines, source_bytes)
            if method is not None:
                direct_methods.append(method)
                method_nodes[method.qualified_name] = child
                child_classes, child_functions = _extract_local_type_trees(
                    _method_body_node(child),
                    package,
                    method.qualified_name,
                    lines,
                    source_bytes,
                    method_nodes,
                    field_types_by_class,
                )
                nested_classes.extend(child_classes)
                nested_functions.extend(child_functions)

    class_info = ClassInfo(
        name=name,
        line_range=LineRange(start=start_line_0 + 1, end=node.end_point.row + 1),
        base_classes=_type_base_classes(node, source_bytes),
        methods=direct_methods,
        decorators=decorators,
        docstring=docstring,
        qualified_name=qualified_name,
        decorator_details=decorator_details,
        visibility=_visibility_from_modifiers(modifiers, source_bytes),
    )
    return [class_info] + nested_classes, direct_methods + nested_functions


def _build_visible_types(
    imports: list[ImportInfo], classes: list[ClassInfo]
) -> tuple[dict[str, str], dict[str, str]]:
    visible_types: dict[str, str] = {}
    imported_static_members: dict[str, str] = {}

    for cls in classes:
        if cls.qualified_name:
            visible_types.setdefault(cls.name, cls.qualified_name)

    for imp in imports:
        if imp.is_from_import:
            if imp.module:
                owner_simple = imp.module.rsplit(".", 1)[-1]
                visible_types.setdefault(owner_simple, imp.module)
                for name in imp.names:
                    if name != "*":
                        imported_static_members[name] = imp.module
            continue
        if not imp.names or imp.names == ["*"]:
            continue
        visible_types.setdefault(imp.names[0], imp.module)

    return visible_types, imported_static_members


def _resolve_dep_name(name: str, visible_types: dict[str, str]) -> str:
    return visible_types.get(name, name)


def _strip_generic_arguments(text: str) -> str:
    result: list[str] = []
    depth = 0
    for ch in text:
        if ch == "<":
            depth += 1
            continue
        if ch == ">":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            result.append(ch)
    return "".join(result)


def _extract_type_reference(text: str) -> str | None:
    cleaned = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", text).strip()
    if not cleaned:
        return None
    cleaned = _strip_generic_arguments(cleaned)
    cleaned = cleaned.replace("...", "").replace("[]", "")
    tokens = re.findall(r"[A-Za-z_][\w.]*", cleaned)
    if not tokens:
        return None
    for token in reversed(tokens):
        simple = token.rsplit(".", 1)[-1]
        if simple and simple[0].isupper() and simple not in _COMMON_JAVA_TYPES:
            return token
    return None


def _resolve_type_reference(type_name: str | None, visible_types: dict[str, str]) -> str | None:
    if not type_name:
        return None
    if type_name in visible_types:
        return visible_types[type_name]
    if "." in type_name:
        return visible_types.get(type_name.rsplit(".", 1)[-1], type_name)
    return type_name


def _iter_descendants(node: Node | None) -> list[Node]:
    if node is None:
        return []
    nodes: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        nodes.append(current)
        stack.extend(reversed(current.named_children))
    return nodes


def _count_argument_nodes(arguments_node: Node | None) -> int | None:
    if arguments_node is None:
        return None
    return len(arguments_node.named_children)


def _resolve_method_target(
    owner_name: str,
    method_name: str,
    arity: int | None,
    methods_by_owner: dict[str, dict[str, dict[int, list[str]]]],
) -> str | None:
    overloads = methods_by_owner.get(owner_name, {}).get(method_name)
    if overloads:
        if arity is not None:
            matches = overloads.get(arity, [])
            if len(matches) == 1:
                return matches[0]
        all_matches = [match for matches in overloads.values() for match in matches]
        if len(all_matches) == 1:
            return all_matches[0]
    return f"{owner_name}.{method_name}"


def _collect_field_types(body_node: Node | None, source_bytes: bytes) -> dict[str, str]:
    field_types: dict[str, str] = {}
    if body_node is None:
        return field_types

    for child in _body_children(body_node):
        if child.type != "field_declaration":
            continue
        type_node = next(
            (
                grandchild
                for grandchild in child.named_children
                if grandchild.type not in {"modifiers", "variable_declarator"}
            ),
            None,
        )
        type_name = _extract_type_reference(_node_text(type_node, source_bytes)) if type_node else None
        if not type_name:
            continue
        for declarator in child.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            if name_node is None:
                continue
            field_types[_node_text(name_node, source_bytes)] = type_name
    return field_types


def _collect_method_variable_types(
    method_node: Node,
    inherited_types: dict[str, str],
    source_bytes: bytes,
) -> dict[str, str]:
    variable_types = dict(inherited_types)

    parameters_node = method_node.child_by_field_name("parameters")
    if parameters_node is not None:
        for param in parameters_node.named_children:
            if param.type not in {"formal_parameter", "spread_parameter"}:
                continue
            type_node = next(
                (
                    child
                    for child in param.named_children
                    if child.type not in {"identifier", "variable_declarator_id", "dimensions"}
                ),
                None,
            )
            name_node = param.child_by_field_name("name") or next(
                (child for child in param.named_children if child.type == "identifier"),
                None,
            )
            type_name = _extract_type_reference(_node_text(type_node, source_bytes)) if type_node else None
            if type_name and name_node is not None:
                variable_types[_node_text(name_node, source_bytes)] = type_name

    for node in _iter_descendants(_method_body_node(method_node)):
        if node.type != "local_variable_declaration":
            continue
        type_node = next(
            (
                child
                for child in node.named_children
                if child.type not in {"modifiers", "variable_declarator"}
            ),
            None,
        )
        type_name = _extract_type_reference(_node_text(type_node, source_bytes)) if type_node else None
        if not type_name:
            continue
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            if name_node is not None:
                variable_types[_node_text(name_node, source_bytes)] = type_name

    return variable_types


def _resolve_receiver_owner(
    object_node: Node | None,
    current_owner: str,
    variable_types: dict[str, str],
    visible_types: dict[str, str],
    source_bytes: bytes,
) -> str | None:
    if object_node is None:
        return current_owner
    if object_node.type in {"this", "super"}:
        return current_owner
    if object_node.type == "object_creation_expression":
        type_node = object_node.child_by_field_name("type")
        type_name = _extract_type_reference(_node_text(type_node, source_bytes)) if type_node else None
        return _resolve_type_reference(type_name, visible_types)

    if object_node.type == "identifier":
        object_text = _node_text(object_node, source_bytes)
        if object_text in variable_types:
            return _resolve_type_reference(variable_types[object_text], visible_types)
        if object_text and object_text[0].isupper():
            return _resolve_type_reference(object_text, visible_types)

    type_name = _extract_type_reference(_node_text(object_node, source_bytes))
    return _resolve_type_reference(type_name, visible_types)


def _collect_method_body_dependencies(
    func: FunctionInfo,
    method_node: Node,
    visible_types: dict[str, str],
    methods_by_owner: dict[str, dict[str, dict[int, list[str]]]],
    field_types_by_class: dict[str, dict[str, str]],
    source_bytes: bytes,
) -> set[str]:
    deps: set[str] = set()
    current_owner = func.qualified_name.rsplit(".", 1)[0]
    variable_types = _collect_method_variable_types(
        method_node,
        field_types_by_class.get(current_owner, {}),
        source_bytes,
    )

    for node in _iter_descendants(_method_body_node(method_node)):
        if node.type == "method_invocation":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            method_name = _node_text(name_node, source_bytes)
            owner_name = _resolve_receiver_owner(
                node.child_by_field_name("object"),
                current_owner,
                variable_types,
                visible_types,
                source_bytes,
            )
            if not owner_name:
                continue
            target = _resolve_method_target(
                owner_name,
                method_name,
                _count_argument_nodes(node.child_by_field_name("arguments")),
                methods_by_owner,
            )
            if target and target != func.qualified_name:
                deps.add(target)
            owner_simple = owner_name.rsplit(".", 1)[-1]
            arguments_node = node.child_by_field_name("arguments")
            if method_name == "start" and owner_simple.endswith("Thread"):
                run_target = _run_target_for_type(owner_name, methods_by_owner)
                if run_target and run_target != func.qualified_name:
                    deps.add(run_target)
                if owner_simple == "Thread":
                    deps.update(
                        target
                        for target in _resolve_launch_argument_targets(
                            arguments_node,
                            variable_types,
                            visible_types,
                            methods_by_owner,
                            source_bytes,
                        )
                        if target != func.qualified_name
                    )
            elif method_name in {"execute", "scheduleAtFixedRate", "scheduleWithFixedDelay"} and (
                owner_simple.endswith(_JAVA_EXECUTOR_TYPE_SUFFIXES)
            ):
                deps.update(
                    target
                    for target in _resolve_launch_argument_targets(
                        arguments_node,
                        variable_types,
                        visible_types,
                        methods_by_owner,
                        source_bytes,
                    )
                    if target != func.qualified_name
                )
            elif method_name in {"submit", "schedule"} and owner_simple.endswith(_JAVA_EXECUTOR_TYPE_SUFFIXES):
                deps.update(
                    target
                    for target in _resolve_launch_argument_targets(
                        arguments_node,
                        variable_types,
                        visible_types,
                        methods_by_owner,
                        source_bytes,
                        prefer_call=True,
                    )
                    if target != func.qualified_name
                )
        elif node.type == "method_reference" and len(node.children) == 3:
            owner_name = _resolve_receiver_owner(
                node.children[0],
                current_owner,
                variable_types,
                visible_types,
                source_bytes,
            )
            if not owner_name:
                continue
            method_name = _node_text(node.children[2], source_bytes)
            if method_name == "new":
                deps.add(owner_name)
                continue
            target = _resolve_method_target(owner_name, method_name, None, methods_by_owner)
            deps.add(target or f"{owner_name}.{method_name}")
        elif node.type == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            type_name = _resolve_type_reference(
                _extract_type_reference(_node_text(type_node, source_bytes)) if type_node else None,
                visible_types,
            )
            if type_name:
                deps.add(type_name)

    return deps


def _run_target_for_type(
    owner_name: str,
    methods_by_owner: dict[str, dict[str, dict[int, list[str]]]],
) -> str | None:
    return _resolve_method_target(owner_name, "run", None, methods_by_owner) or f"{owner_name}.run"


def _call_target_for_type(
    owner_name: str,
    methods_by_owner: dict[str, dict[str, dict[int, list[str]]]],
) -> str | None:
    return _resolve_method_target(owner_name, "call", None, methods_by_owner) or f"{owner_name}.call"


def _resolve_launch_argument_targets(
    arguments_node: Node | None,
    variable_types: dict[str, str],
    visible_types: dict[str, str],
    methods_by_owner: dict[str, dict[str, dict[int, list[str]]]],
    source_bytes: bytes,
    *,
    prefer_call: bool = False,
) -> set[str]:
    targets: set[str] = set()
    if arguments_node is None:
        return targets
    for arg in arguments_node.named_children:
        owner_name: str | None = None
        if arg.type == "identifier":
            arg_text = _node_text(arg, source_bytes)
            if arg_text in variable_types:
                owner_name = _resolve_type_reference(variable_types[arg_text], visible_types)
        elif arg.type == "object_creation_expression":
            type_node = arg.child_by_field_name("type")
            owner_name = _resolve_type_reference(
                _extract_type_reference(_node_text(type_node, source_bytes)) if type_node else None,
                visible_types,
            )
        if not owner_name:
            continue
        if prefer_call:
            call_target = _call_target_for_type(owner_name, methods_by_owner)
            run_target = _run_target_for_type(owner_name, methods_by_owner)
            if call_target:
                targets.add(call_target)
            if run_target:
                targets.add(run_target)
        else:
            target = _run_target_for_type(owner_name, methods_by_owner)
            if target:
                targets.add(target)
    return targets


def _count_call_args(body_text: str, paren_start_idx: int) -> int | None:
    idx = paren_start_idx + 1
    depth = 1
    angle_depth = 0
    bracket_depth = 0
    arg_count = 0
    saw_non_ws = False

    while idx < len(body_text):
        ch = body_text[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                if not saw_non_ws:
                    return 0
                return arg_count + 1
        elif ch == "<":
            angle_depth += 1
        elif ch == ">":
            angle_depth = max(0, angle_depth - 1)
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif ch == "," and depth == 1 and angle_depth == 0 and bracket_depth == 0:
            arg_count += 1
        elif not ch.isspace():
            saw_non_ws = True
        idx += 1

    return None


def _iter_unqualified_calls(body_text: str) -> list[tuple[str, int | None]]:
    calls: list[tuple[str, int | None]] = []
    for match in re.finditer(r"(?<![\w.])([a-z_][A-Za-z0-9_]*)\s*\(", body_text):
        calls.append((match.group(1), _count_call_args(body_text, match.end() - 1)))
    return calls


def _build_dependency_graph(
    lines: list[str],
    classes: list[ClassInfo],
    functions: list[FunctionInfo],
    imports: list[ImportInfo],
    method_nodes: dict[str, Node],
    field_types_by_class: dict[str, dict[str, str]],
    source_bytes: bytes,
) -> dict[str, list[str]]:
    visible_types, imported_static_members = _build_visible_types(imports, classes)
    methods_by_owner: dict[str, dict[str, dict[int, list[str]]]] = {}
    for func in functions:
        owner_key = func.qualified_name.rsplit(".", 1)[0]
        owner_methods = methods_by_owner.setdefault(owner_key, {})
        owner_methods.setdefault(func.name, {}).setdefault(len(func.parameters), []).append(
            func.qualified_name
        )

    graph: dict[str, list[str]] = {}

    for cls in classes:
        owner_key = cls.qualified_name or cls.name
        deps = {
            _resolve_dep_name(base, visible_types)
            for base in cls.base_classes
            if base and _resolve_dep_name(base, visible_types) != owner_key
        }
        for decorator in cls.decorators:
            if decorator and decorator[0].isupper():
                deps.add(_resolve_dep_name(decorator, visible_types))
        graph[owner_key] = sorted(dep for dep in deps if dep != owner_key)

    for func in functions:
        owner_key = func.qualified_name.rsplit(".", 1)[0]
        overloads = methods_by_owner.get(owner_key, {})
        body_text = "\n".join(lines[func.line_range.start - 1 : func.line_range.end])
        deps: set[str] = set()

        for call_name, arity in _iter_unqualified_calls(body_text):
            if call_name in overloads:
                arity_map = overloads[call_name]
                matches = arity_map.get(arity or 0, []) if arity is not None else []
                if not matches and sum(len(values) for values in arity_map.values()) == 1:
                    matches = next(iter(arity_map.values()))
                if len(matches) == 1 and matches[0] != func.qualified_name:
                    deps.add(matches[0])
            elif call_name in imported_static_members:
                deps.add(imported_static_members[call_name])

        for match in re.finditer(r"\b([A-Z][A-Za-z0-9_]*)\s*\.", body_text):
            name = match.group(1)
            if name in _COMMON_JAVA_TYPES:
                continue
            deps.add(_resolve_dep_name(name, visible_types))

        for match in re.finditer(r"\bnew\s+([A-Z][A-Za-z0-9_]*)\b", body_text):
            name = match.group(1)
            if name in _COMMON_JAVA_TYPES:
                continue
            deps.add(_resolve_dep_name(name, visible_types))

        for decorator in func.decorators:
            if decorator and decorator[0].isupper():
                deps.add(_resolve_dep_name(decorator, visible_types))

        method_node = method_nodes.get(func.qualified_name)
        if method_node is not None:
            deps.update(
                _collect_method_body_dependencies(
                    func,
                    method_node,
                    visible_types,
                    methods_by_owner,
                    field_types_by_class,
                    source_bytes,
                )
            )

        graph[func.qualified_name] = sorted(dep for dep in deps if dep != func.qualified_name)

    return graph


def _annotate_java_module_info(
    source: str,
    source_name: str,
    lines: list[str],
    line_offsets: list[int],
    root: Node,
    source_bytes: bytes,
) -> StructuralMetadata:
    sections: list[SectionInfo] = []
    module_name: str | None = None

    for node in root.named_children:
        if node.type != "module_declaration":
            continue
        name_node = next((child for child in node.named_children if child.type == "scoped_identifier"), None)
        if name_node is not None:
            module_name = _node_text(name_node, source_bytes)
        title = _normalize_ws(_node_text(node, source_bytes).split("{", 1)[0])
        sections.append(
            SectionInfo(
                title=title,
                level=1,
                line_range=LineRange(start=node.start_point.row + 1, end=node.start_point.row + 1),
            )
        )
        body_node = node.child_by_field_name("body")
        if body_node is None:
            body_node = next((child for child in node.named_children if child.type == "module_body"), None)
        for child in body_node.named_children if body_node is not None else []:
            directive = _normalize_ws(_node_text(child, source_bytes)).rstrip(";")
            sections.append(
                SectionInfo(
                    title=directive,
                    level=2,
                    line_range=LineRange(start=child.start_point.row + 1, end=child.end_point.row + 1),
                )
            )

    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=len(source),
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
        module_name=module_name,
    )


def _annotate_java_package_info(
    source: str,
    source_name: str,
    lines: list[str],
    line_offsets: list[int],
    root: Node,
    source_bytes: bytes,
) -> StructuralMetadata:
    package = _parse_package_name(list(root.named_children), source_bytes)
    sections: list[SectionInfo] = []

    package_node = next((node for node in root.named_children if node.type == "package_declaration"), None)
    if package_node is not None and package:
        fallback_decorators, start_line_0, docstring = _collect_leading_metadata(lines, package_node.start_point.row)
        if docstring:
            sections.append(
                SectionInfo(
                    title=f"Javadoc {_normalize_ws(docstring.splitlines()[0])}",
                    level=1,
                    line_range=LineRange(start=start_line_0 + 1, end=package_node.start_point.row),
                )
            )
        decorators = _extract_annotation_names(package_node, source_bytes) or fallback_decorators
        for decorator in decorators:
            sections.append(
                SectionInfo(
                    title=f"@{decorator}",
                    level=2,
                    line_range=LineRange(
                        start=package_node.start_point.row + 1,
                        end=package_node.start_point.row + 1,
                    ),
                )
            )
        sections.append(
            SectionInfo(
                title=f"package {package}",
                level=1,
                line_range=LineRange(start=package_node.start_point.row + 1, end=package_node.end_point.row + 1),
            )
        )

    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=len(source),
        lines=lines,
        line_char_offsets=line_offsets,
        sections=sections,
        module_name=package,
    )


def annotate_java(source: str, source_name: str = "<source>") -> StructuralMetadata:
    """Parse Java source and extract structural metadata using tree-sitter."""
    lines = source.split("\n")
    total_lines = len(lines)
    total_chars = len(source)
    line_offsets = build_line_char_offsets(lines)
    source_bytes = source.encode("utf-8")

    parser = Parser(_JAVA_LANGUAGE)
    tree = parser.parse(source_bytes)
    root = tree.root_node
    children = list(root.named_children)

    source_basename = basename(source_name)
    if source_basename == "module-info.java":
        return _annotate_java_module_info(source, source_name, lines, line_offsets, root, source_bytes)
    if source_basename == "package-info.java":
        return _annotate_java_package_info(source, source_name, lines, line_offsets, root, source_bytes)

    package = _parse_package_name(children, source_bytes)
    imports = _parse_imports([node for node in children if node.type == "import_declaration"], source_bytes)

    classes: list[ClassInfo] = []
    functions: list[FunctionInfo] = []
    method_nodes: dict[str, Node] = {}
    field_types_by_class: dict[str, dict[str, str]] = {}
    for node in children:
        if node.type not in _TYPE_NODE_KINDS:
            continue
        found_classes, found_functions = _extract_type_tree(
            node,
            package,
            None,
            lines,
            source_bytes,
            method_nodes,
            field_types_by_class,
        )
        classes.extend(found_classes)
        functions.extend(found_functions)

    dependency_graph = _build_dependency_graph(
        lines,
        classes,
        functions,
        imports,
        method_nodes,
        field_types_by_class,
        source_bytes,
    )

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
        module_name=package,
    )
