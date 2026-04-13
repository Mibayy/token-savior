"""Java-specific quality hotspot detection for Token Savior."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from token_savior.models import ClassInfo, FunctionInfo, ProjectIndex, StructuralMetadata

_STRING_LITERAL_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_CHAR_LITERAL_RE = re.compile(r"'(?:\\.|[^'\\])'")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//.*$")

_JAVA_FILE_EXTENSIONS = frozenset({".java"})

_ALLOCATION_RULES: tuple[tuple[str, re.Pattern[str], float], ...] = (
    ("object construction", re.compile(r"\bnew\s+[A-Z_][A-Za-z0-9_$.<>]*\s*\("), 5.0),
    (
        "collection/buffer construction",
        re.compile(
            r"\bnew\s+(?:ArrayList|LinkedList|HashMap|LinkedHashMap|ConcurrentHashMap|"
            r"HashSet|LinkedHashSet|TreeMap|TreeSet|StringBuilder|StringBuffer|BigDecimal|BigInteger)"
            r"(?:<[^>]*>)?\s*\("
        ),
        7.0,
    ),
    ("array allocation", re.compile(r"\bnew\s+[A-Za-z_][A-Za-z0-9_$.<>?,\s]*\["), 4.0),
    ("string formatting", re.compile(r"\bString\.format\s*\(|\.formatted\s*\("), 6.0),
    ("stream pipeline", re.compile(r"\.(?:stream|parallelStream)\s*\(|\bCollectors\."), 5.0),
    ("optional wrapper", re.compile(r"\bOptional\.(?:of|ofNullable|empty)\s*\("), 4.0),
    (
        "boxing helper",
        re.compile(r"\b(?:Integer|Long|Double|Float|Short|Byte|Boolean|Character)\.valueOf\s*\("),
        3.0,
    ),
    ("regex compilation", re.compile(r"\bPattern\.compile\s*\("), 5.0),
    ("exception allocation", re.compile(r"\bnew\s+[A-Z][A-Za-z0-9_]*(?:Exception|Error)\s*\("), 4.0),
)

_PERFORMANCE_RULES: tuple[tuple[str, re.Pattern[str], float], ...] = (
    ("synchronized block/method", re.compile(r"\bsynchronized\b"), 7.0),
    (
        "explicit lock",
        re.compile(r"\b(?:ReentrantLock|ReadWriteLock|StampedLock)\b|(?:^|[^\w])lock\s*\(|(?:^|[^\w])unlock\s*\("),
        6.0,
    ),
    (
        "blocking I/O",
        re.compile(
            r"\bFiles\.(?:read|write|readAllBytes|lines|newBufferedReader|newBufferedWriter)\s*\(|"
            r"\b(?:FileInputStream|FileOutputStream|Socket|ServerSocket|HttpClient)\b"
        ),
        6.0,
    ),
    ("monitor wait/notify", re.compile(r"\b(?:wait|notify|notifyAll)\s*\("), 7.0),
)

_MUTABLE_FIELD_RE = re.compile(
    r"\b(?:private|protected|public)?\s*"
    r"(?:volatile\s+)?(?:long|int|short|byte|double|float|boolean|AtomicLong|AtomicInteger|AtomicReference)\s+"
    r"[A-Za-z_][A-Za-z0-9_]*\b"
)
_CACHE_PADDING_HINT_RE = re.compile(
    r"@Contended|\bcacheLine\b|\bpadding\b|\bleftPadding\b|\brightPadding\b|\bpad\d+\b"
)
_BLOCKING_WAIT_RE = re.compile(
    r"\bThread\.sleep\s*\(|\.join\s*\(|\.await\s*\(|\.take\s*\(|\.put\s*\(|\.park\s*\(|\.block\s*\("
)
_FUTURE_DECL_RE = re.compile(
    r"\b(?:Future|CompletableFuture)(?:<[^;=)]*>)?\s+([A-Za-z_][A-Za-z0-9_]*)\b"
)
_FUTURE_GET_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.get\s*\(")


@dataclass(frozen=True)
class _Hotspot:
    score: float
    hits: int
    qualified_name: str
    file_path: str
    line: int
    reasons: tuple[str, ...]


def _strip_java_noise(source: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", source)
    stripped_lines: list[str] = []
    for raw_line in text.splitlines():
        line = _LINE_COMMENT_RE.sub("", raw_line)
        line = _STRING_LITERAL_RE.sub('""', line)
        line = _CHAR_LITERAL_RE.sub("''", line)
        stripped_lines.append(line)
    return "\n".join(stripped_lines)


def _find_enclosing_class(meta: StructuralMetadata, func: FunctionInfo) -> ClassInfo | None:
    if not func.parent_class:
        return None
    candidates = [
        cls
        for cls in meta.classes
        if cls.name == func.parent_class and cls.line_range.start <= func.line_range.start <= cls.line_range.end
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda cls: cls.line_range.end - cls.line_range.start)


def _class_source(meta: StructuralMetadata, cls: ClassInfo | None) -> str:
    if cls is None:
        return ""
    return "\n".join(meta.lines[cls.line_range.start - 1 : cls.line_range.end])


def _function_source(meta: StructuralMetadata, func: FunctionInfo) -> str:
    start = max(1, func.line_range.start)
    end = min(len(meta.lines), func.line_range.end)
    name_pattern = re.compile(rf"\b{re.escape(func.name)}\s*\(")
    for line_no in range(start, end + 1):
        line = meta.lines[line_no - 1]
        stripped = line.strip()
        if not stripped or stripped.startswith("@"):
            continue
        if name_pattern.search(line):
            start = line_no
            break

    if not str(meta.source_name).endswith(".java"):
        return "\n".join(meta.lines[start - 1 : end])

    seen_body = False
    brace_depth = 0
    effective_end = end
    for line_no in range(start, end + 1):
        line = meta.lines[line_no - 1]
        stripped = line.strip()
        if not stripped or stripped.startswith("@"):
            continue
        if not seen_body and "{" not in line and stripped.endswith(";"):
            effective_end = line_no
            break
        for ch in line:
            if ch == "{":
                brace_depth += 1
                seen_body = True
            elif ch == "}":
                brace_depth = max(0, brace_depth - 1)
        if seen_body and brace_depth == 0:
            effective_end = line_no
            break

    return "\n".join(meta.lines[start - 1 : effective_end])


def _shared_state_penalty(meta: StructuralMetadata, func: FunctionInfo) -> tuple[int, float, str] | None:
    cls = _find_enclosing_class(meta, func)
    if cls is None:
        return None
    class_source = _strip_java_noise(_class_source(meta, cls))
    if _CACHE_PADDING_HINT_RE.search(class_source):
        return None
    mutable_field_count = 0
    for line in class_source.splitlines():
        stripped = line.strip()
        if not stripped or "(" in stripped or ")" in stripped:
            continue
        if " final " in f" {stripped} " or stripped.startswith("final "):
            continue
        if " static " in f" {stripped} " or stripped.startswith("static "):
            continue
        if _MUTABLE_FIELD_RE.search(stripped):
            mutable_field_count += 1
    if mutable_field_count < 2:
        return None
    return (1, 4.0, "shared mutable fields without cache-line padding")


def _blocking_wait_signal(source: str) -> tuple[int, float, str] | None:
    direct_hits = len(_BLOCKING_WAIT_RE.findall(source))
    future_names = set(_FUTURE_DECL_RE.findall(source))
    future_get_hits = sum(
        1 for candidate in _FUTURE_GET_RE.findall(source) if candidate in future_names
    )
    hits = direct_hits + future_get_hits
    if hits <= 0:
        return None
    return (hits, hits * 8.0, f"blocking wait x{hits}")


def _scan_rules(source: str, rules: tuple[tuple[str, re.Pattern[str], float], ...]) -> tuple[float, int, list[str]]:
    score = 0.0
    hits = 0
    reasons: list[str] = []
    for label, pattern, weight in rules:
        match_count = len(pattern.findall(source))
        if match_count <= 0:
            continue
        hits += match_count
        score += match_count * weight
        reasons.append(f"{label} x{match_count}")
    return score, hits, reasons


def _collect_java_hotspots(
    index: ProjectIndex,
    rules: tuple[tuple[str, re.Pattern[str], float], ...],
    *,
    include_shared_state: bool = False,
    min_score: float = 1.0,
) -> list[_Hotspot]:
    hotspots: list[_Hotspot] = []
    for file_path, meta in index.files.items():
        if os.path.splitext(file_path)[1].lower() not in _JAVA_FILE_EXTENSIONS:
            continue
        for func in meta.functions:
            body = _strip_java_noise(_function_source(meta, func))
            score, hits, reasons = _scan_rules(body, rules)
            if rules is _PERFORMANCE_RULES:
                blocking_wait = _blocking_wait_signal(body)
                if blocking_wait is not None:
                    extra_hits, extra_score, extra_reason = blocking_wait
                    hits += extra_hits
                    score += extra_score
                    reasons.append(extra_reason)
            if include_shared_state:
                shared_state = _shared_state_penalty(meta, func)
                if shared_state is not None:
                    extra_hits, extra_score, extra_reason = shared_state
                    hits += extra_hits
                    score += extra_score
                    reasons.append(extra_reason)
            if score < min_score:
                continue
            hotspots.append(
                _Hotspot(
                    score=score,
                    hits=hits,
                    qualified_name=func.qualified_name,
                    file_path=file_path,
                    line=func.line_range.start,
                    reasons=tuple(reasons),
                )
            )
    hotspots.sort(key=lambda item: (item.score, item.hits, item.qualified_name), reverse=True)
    return hotspots


def _format_hotspots(
    hotspots: list[_Hotspot],
    *,
    max_results: int,
    title_prefix: str,
    empty_message: str,
) -> str:
    if not hotspots:
        return empty_message
    rows = hotspots[:max_results]
    lines = [
        f"{title_prefix} -- top {len(rows)} function{'s' if len(rows) != 1 else ''}",
        "",
        "Score | Hits | Function | Reasons",
        "------+------|----------|--------",
    ]
    for item in rows:
        location = f"{item.file_path}:{item.line} {item.qualified_name}"
        lines.append(
            f"{item.score:5.1f} | {item.hits:4d} | {location} | {', '.join(item.reasons)}"
        )
    return "\n".join(lines)


def find_allocation_hotspots(
    index: ProjectIndex,
    max_results: int = 20,
    min_score: float = 1.0,
) -> str:
    """Rank Java functions by allocation-heavy patterns."""
    hotspots = _collect_java_hotspots(index, _ALLOCATION_RULES, min_score=min_score)
    return _format_hotspots(
        hotspots,
        max_results=max_results,
        title_prefix="Java Allocation Hotspots",
        empty_message="No Java allocation hotspots found.",
    )


def find_performance_hotspots(
    index: ProjectIndex,
    max_results: int = 20,
    min_score: float = 1.0,
) -> str:
    """Rank Java functions by non-allocation ULL performance risks."""
    hotspots = _collect_java_hotspots(
        index,
        _PERFORMANCE_RULES,
        include_shared_state=True,
        min_score=min_score,
    )
    return _format_hotspots(
        hotspots,
        max_results=max_results,
        title_prefix="Java Performance Hotspots",
        empty_message="No Java performance hotspots found.",
    )
