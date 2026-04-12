"""Regex-based TypeScript annotator (v2, improved for Next.js/React).

This is NOT a full TypeScript parser. It handles common patterns for
function declarations, class/interface/type declarations, and import
statements using regular expressions and brace counting. Edge cases
(e.g. functions inside template literals, deeply nested generics) may
be missed, and that is acceptable.

v2 improvements over v1:
  - export default function / export default async function
  - Multi-line function parameters (join lines until closing paren)
  - Arrow functions with type annotations (const x: Type = () =>)
  - Better destructured parameter handling
"""

import re
from typing import Optional

from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    ImportInfo,
    LineRange,
    StructuralMetadata,
    build_line_char_offsets,
)


def _find_brace_end(lines: list[str], start_line_0: int) -> int:
    """Starting from *start_line_0*, find the 0-based line index where
    the outermost opening brace is closed.  Returns *start_line_0* if
    no brace is found on that line (one-liner without braces)."""
    depth = 0
    found_open = False
    for idx in range(start_line_0, len(lines)):
        for ch in lines[idx]:
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
                if found_open and depth == 0:
                    return idx
    # If we never found a closing brace, return last line
    return len(lines) - 1


def _join_until_paren_close(lines: list[str], start_0: int) -> tuple[str, int]:
    """Join lines starting from start_0 until we find a balanced closing paren.
    Returns (joined_string, last_line_0_consumed).
    Handles multi-line function parameters."""
    depth = 0
    parts = []
    for idx in range(start_0, min(start_0 + 20, len(lines))):  # max 20 lines lookahead
        line = lines[idx]
        parts.append(line)
        for ch in line:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return " ".join(parts), idx
    return " ".join(parts), start_0


def _find_arrow_body_start(lines: list[str], start_0: int) -> tuple[int, bool]:
    """Find the start line of an arrow-function body.

    Returns (line_index, has_block_body).
    """
    seen_arrow = False
    arrow_line = -1
    for idx in range(start_0, min(start_0 + 20, len(lines))):
        line = lines[idx]
        if "=>" in line:
            seen_arrow = True
            arrow_line = idx
        if not seen_arrow:
            continue
        if "{" in line:
            return idx, True
        if idx == arrow_line:
            continue
        if line.strip():
            return idx, False
    return start_0, False


# ---------------------------------------------------------------------------
# Import detection
# ---------------------------------------------------------------------------

_IMPORT_RE = re.compile(
    r"""^import\s+"""
    r"""(?:"""
    r"""(?:type\s+)?"""  # optional 'type' keyword
    r"""\{([^}]*)\}\s+from\s+"""  # named imports  { A, B }
    r"""|"""
    r"""(\*\s+as\s+\w+)\s+from\s+"""  # namespace import  * as X
    r"""|"""
    r"""(\w+)\s+from\s+"""  # default import   Foo
    r"""|"""
    r"""(\w+)\s*,\s*\{([^}]*)\}\s+from\s+"""  # default + named
    r""")"""
    r"""['"]([^'"]+)['"]""",  # module path
    re.MULTILINE,
)

# Simpler fallback: import '...' (side-effect import)
_SIDE_EFFECT_IMPORT_RE = re.compile(
    r"""^import\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def _parse_imports(lines: list[str]) -> list[ImportInfo]:
    imports: list[ImportInfo] = []
    for line_0, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("import"):
            continue

        m = _IMPORT_RE.match(stripped)
        if m:
            named_group = m.group(1)
            namespace_group = m.group(2)
            default_group = m.group(3)
            default_plus_named_default = m.group(4)
            default_plus_named_names = m.group(5)
            module = m.group(6)

            names: list[str] = []
            alias: Optional[str] = None

            if named_group is not None:
                names = [
                    n.strip().split(" as ")[0].strip() for n in named_group.split(",") if n.strip()
                ]
            elif namespace_group is not None:
                # * as X
                alias = namespace_group.split("as")[-1].strip()
            elif default_group is not None:
                alias = default_group
            elif default_plus_named_default is not None:
                alias = default_plus_named_default
                if default_plus_named_names is not None:
                    names = [
                        n.strip().split(" as ")[0].strip()
                        for n in default_plus_named_names.split(",")
                        if n.strip()
                    ]

            imports.append(
                ImportInfo(
                    module=module,
                    names=names,
                    alias=alias,
                    line_number=line_0 + 1,
                    is_from_import=True,
                )
            )
            continue

        m2 = _SIDE_EFFECT_IMPORT_RE.match(stripped)
        if m2:
            imports.append(
                ImportInfo(
                    module=m2.group(1),
                    names=[],
                    alias=None,
                    line_number=line_0 + 1,
                    is_from_import=False,
                )
            )

    return imports


# ---------------------------------------------------------------------------
# Function detection
# ---------------------------------------------------------------------------

# Patterns for standalone / exported / default-exported functions
_FUNC_DECL_RE = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(")

# Anonymous default export: export default function(
_FUNC_DEFAULT_ANON_RE = re.compile(r"^export\s+default\s+(?:async\s+)?function\s*\(")

# Arrow function assigned to const/let/var — with optional type annotation
# Handles: const foo = () =>, const foo: Type = () =>, export const foo = async () =>
_ARROW_FUNC_RE = re.compile(
    r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::\s*[^=]+?)?\s*=\s*(?:async\s+)?\("
)

# Arrow function single-expression (no parens on single param)
# const foo = x => x + 1
_ARROW_SINGLE_RE = re.compile(
    r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::\s*[^=]+?)?\s*=\s*(?:async\s+)?(\w+)\s*=>"
)

# Method inside a class body (indented)
_METHOD_RE = re.compile(
    r"^\s+(?:(?:public|private|protected|static|async|readonly|abstract|override|get|set)\s+)*(\w+)\s*\("
)


def _extract_params(raw: str) -> list[str]:
    """Extract parameter names from a raw parameter string."""
    params: list[str] = []
    # Handle the content between the outermost parens
    # Remove nested generics/types to avoid confusion
    depth = 0
    cleaned = []
    for ch in raw:
        if ch in "<({":
            depth += 1
            if depth == 1 and ch == "(":
                continue  # skip outer paren
            cleaned.append(ch)
        elif ch in ">)}":
            if depth == 1 and ch == ")":
                depth -= 1
                continue  # skip outer paren
            depth -= 1
            cleaned.append(ch)
        else:
            cleaned.append(ch)
    raw = "".join(cleaned)

    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        # Remove type annotations, defaults, optional markers
        name = re.split(r"[:\s=?]", p)[0].strip()
        if name and name != "...":
            # Handle destructuring — extract a meaningful name
            if name.startswith("{") or name.startswith("["):
                params.append("destructured")
                continue
            if name.startswith("}") or name.startswith("]"):
                continue  # closing brace from destructured — skip
            params.append(name)
    return params


def _extract_params_from_joined(joined: str) -> list[str]:
    """Extract params from a joined multi-line string containing the function signature."""
    # Find content between first ( and its matching )
    depth = 0
    start = -1
    end = -1
    for i, ch in enumerate(joined):
        if ch == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if start >= 0 and end > start:
        return _extract_params(joined[start:end])
    return []


# ---------------------------------------------------------------------------
# Class / interface / type detection
# ---------------------------------------------------------------------------

_CLASS_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)(?:\s+extends\s+([\w.]+))?(?:\s+implements\s+([\w.,\s]+))?"
)

_INTERFACE_RE = re.compile(r"^(?:export\s+)?interface\s+(\w+)(?:\s+extends\s+([\w.,\s]+))?")

_TYPE_ALIAS_RE = re.compile(r"^(?:export\s+)?type\s+(\w+)\s*(?:<[^>]*>)?\s*=")


# ---------------------------------------------------------------------------
# Main annotator
# ---------------------------------------------------------------------------


def annotate_typescript(source: str, source_name: str = "<source>") -> StructuralMetadata:
    """Parse TypeScript source and extract structural metadata using regex.

    Detects:
      - function declarations (function foo, export default function foo)
      - arrow functions (const foo = () =>, export const foo: Type = async () =>)
      - class declarations (class Foo, export class Foo extends Bar)
      - interface declarations (treated as ClassInfo with no methods by default)
      - type alias declarations (treated as ClassInfo with empty body)
      - import statements
      - Methods inside classes (is_method=True, parent_class set)

    Uses brace counting to determine line ranges of functions and classes.
    """
    lines = source.split("\n")
    total_lines = len(lines)
    total_chars = len(source)
    line_offsets = build_line_char_offsets(lines)

    imports = _parse_imports(lines)

    functions: list[FunctionInfo] = []
    classes: list[ClassInfo] = []

    # Track which lines are consumed by class bodies so we can tag methods.
    # We'll do two passes:
    #   1. Detect top-level classes/interfaces/types
    #   2. Detect top-level functions (not inside a class)
    #   3. Detect methods inside class bodies

    # Pass 1: classes, interfaces, type aliases
    class_ranges: list[tuple[str, int, int, list[str]]] = []  # (name, start_0, end_0, bases)

    i = 0
    while i < total_lines:
        stripped = lines[i].strip()

        # Class
        cm = _CLASS_RE.match(stripped)
        if cm:
            name = cm.group(1)
            bases: list[str] = []
            if cm.group(2):
                bases.append(cm.group(2).strip())
            if cm.group(3):
                bases.extend(b.strip() for b in cm.group(3).split(",") if b.strip())
            end_0 = _find_brace_end(lines, i)
            class_ranges.append((name, i, end_0, bases))
            i = end_0 + 1
            continue

        # Interface
        im = _INTERFACE_RE.match(stripped)
        if im:
            name = im.group(1)
            bases = []
            if im.group(2):
                bases = [b.strip() for b in im.group(2).split(",") if b.strip()]
            end_0 = _find_brace_end(lines, i)
            class_ranges.append((name, i, end_0, bases))
            i = end_0 + 1
            continue

        # Type alias (single line or multi-line)
        tm = _TYPE_ALIAS_RE.match(stripped)
        if tm:
            name = tm.group(1)
            # Type aliases may span multiple lines if they use unions etc.
            # Simple heuristic: if the line has a '{', find the brace end
            if "{" in stripped:
                end_0 = _find_brace_end(lines, i)
            else:
                # Scan until we find a line ending with ';' or a non-continuation
                end_0 = i
                for j in range(i, total_lines):
                    if ";" in lines[j] or (
                        j > i
                        and not lines[j].strip().startswith("|")
                        and not lines[j].strip().startswith("&")
                    ):
                        end_0 = j
                        break
                else:
                    end_0 = total_lines - 1
            class_ranges.append((name, i, end_0, []))
            i = end_0 + 1
            continue

        i += 1

    # Pass 2: detect methods inside each class body
    class_methods: dict[str, list[FunctionInfo]] = {name: [] for name, *_ in class_ranges}

    for class_name, cls_start_0, cls_end_0, _ in class_ranges:
        for j in range(cls_start_0 + 1, cls_end_0 + 1):
            line = lines[j]
            mm = _METHOD_RE.match(line)
            if mm:
                method_name = mm.group(1)
                # Skip things that look like keywords used as property names
                if method_name in (
                    "if",
                    "else",
                    "for",
                    "while",
                    "switch",
                    "return",
                    "new",
                    "throw",
                    "import",
                    "export",
                    "const",
                    "let",
                    "var",
                ):
                    continue
                # Join multi-line params
                joined, last_line = _join_until_paren_close(lines, j)
                params = _extract_params_from_joined(joined)
                # Find end of method via brace counting
                if "{" in lines[j] or (
                    last_line > j and any("{" in lines[k] for k in range(j, last_line + 1))
                ):
                    mend_0 = _find_brace_end(lines, j)
                else:
                    mend_0 = last_line  # abstract method or interface member

                func_info = FunctionInfo(
                    name=method_name,
                    qualified_name=f"{class_name}.{method_name}",
                    line_range=LineRange(start=j + 1, end=mend_0 + 1),
                    parameters=params,
                    decorators=[],
                    docstring=None,
                    is_method=True,
                    parent_class=class_name,
                )
                class_methods[class_name].append(func_info)
                functions.append(func_info)

    # Build ClassInfo objects
    for class_name, cls_start_0, cls_end_0, bases in class_ranges:
        classes.append(
            ClassInfo(
                name=class_name,
                line_range=LineRange(start=cls_start_0 + 1, end=cls_end_0 + 1),
                base_classes=bases,
                methods=class_methods[class_name],
                decorators=[],
                docstring=None,
            )
        )

    # Build a set of line ranges consumed by classes for excluding top-level functions
    class_line_set: set[int] = set()
    for _, cs0, ce0, _ in class_ranges:
        class_line_set.update(range(cs0, ce0 + 1))

    # Pass 3: top-level functions (not inside a class)
    i = 0
    while i < total_lines:
        if i in class_line_set:
            i += 1
            continue

        stripped = lines[i].strip()

        # Named function declarations (including export default)
        fm = _FUNC_DECL_RE.match(stripped)
        if fm:
            name = fm.group(1)
            joined, last_param_line = _join_until_paren_close(lines, i)
            params = _extract_params_from_joined(joined)
            # Find the brace start — may be on the param-closing line or the next
            brace_start = last_param_line
            for k in range(last_param_line, min(last_param_line + 3, total_lines)):
                if "{" in lines[k]:
                    brace_start = k
                    break
            end_0 = _find_brace_end(lines, brace_start)
            functions.append(
                FunctionInfo(
                    name=name,
                    qualified_name=name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    parameters=params,
                    decorators=[],
                    docstring=None,
                    is_method=False,
                    parent_class=None,
                )
            )
            i = end_0 + 1
            continue

        # Anonymous default export function
        fda = _FUNC_DEFAULT_ANON_RE.match(stripped)
        if fda:
            name = "default"
            joined, last_param_line = _join_until_paren_close(lines, i)
            params = _extract_params_from_joined(joined)
            brace_start = last_param_line
            for k in range(last_param_line, min(last_param_line + 3, total_lines)):
                if "{" in lines[k]:
                    brace_start = k
                    break
            end_0 = _find_brace_end(lines, brace_start)
            functions.append(
                FunctionInfo(
                    name=name,
                    qualified_name=name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    parameters=params,
                    decorators=[],
                    docstring=None,
                    is_method=False,
                    parent_class=None,
                )
            )
            i = end_0 + 1
            continue

        # Arrow functions (with optional type annotation before =)
        am = _ARROW_FUNC_RE.match(stripped)
        if am:
            name = am.group(1)
            joined, last_param_line = _join_until_paren_close(lines, i)
            params = _extract_params_from_joined(joined)
            body_start, found_brace = _find_arrow_body_start(lines, i)
            if found_brace:
                end_0 = _find_brace_end(lines, body_start)
            else:
                # Single-expression arrow — find the end
                end_0 = body_start
                for j in range(body_start, total_lines):
                    line_s = lines[j].strip()
                    if j > body_start and line_s and not line_s.endswith(","):
                        end_0 = j
                        break
                    if ";" in lines[j]:
                        end_0 = j
                        break
            functions.append(
                FunctionInfo(
                    name=name,
                    qualified_name=name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    parameters=params,
                    decorators=[],
                    docstring=None,
                    is_method=False,
                    parent_class=None,
                )
            )
            i = end_0 + 1
            continue

        # Single-param arrow (no parens): const fn = x => ...
        asm = _ARROW_SINGLE_RE.match(stripped)
        if asm:
            name = asm.group(1)
            params = [asm.group(2)]
            if "{" in stripped:
                end_0 = _find_brace_end(lines, i)
            else:
                end_0 = i
                for j in range(i, total_lines):
                    if ";" in lines[j]:
                        end_0 = j
                        break
            functions.append(
                FunctionInfo(
                    name=name,
                    qualified_name=name,
                    line_range=LineRange(start=i + 1, end=end_0 + 1),
                    parameters=params,
                    decorators=[],
                    docstring=None,
                    is_method=False,
                    parent_class=None,
                )
            )
            i = end_0 + 1
            continue

        i += 1

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
