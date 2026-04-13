"""Symbol-level hashing + light semantic analysis.

Shared by:
- project_indexer: symbol-level reindex (prompt 1)
- breaking_changes: two-level breaking detection (prompt 2)
- server:          session symbol cache / CSC (prompt 3)
- query_api:       abstraction levels L0-L3 (prompt 4)
"""

from __future__ import annotations

import hashlib
import re

from token_savior.models import ClassInfo, FunctionInfo


_COMMENT_RE = re.compile(r"#.*$", flags=re.MULTILINE)
_WHITESPACE_RE = re.compile(r"\s+")


def _short(h: bytes) -> str:
    return hashlib.sha256(h).hexdigest()[:16]


def compute_signature_hash(func: FunctionInfo) -> str:
    """Hash of the public signature only (name + params + decorators).

    Stable across pure body refactors. Changes only when the public contract
    changes (rename, param added/removed, decorator added).
    """
    parts = [func.name, "(", ",".join(func.parameters), ")"]
    if func.decorators:
        parts = [*sorted(func.decorators), "|", *parts]
    return _short("\u0001".join(parts).encode("utf-8"))


def _normalize_body(body: str) -> str:
    """Strip comments + collapse whitespace so cosmetic edits don't re-hash."""
    body = _COMMENT_RE.sub("", body)
    body = _WHITESPACE_RE.sub(" ", body).strip()
    return body


def compute_body_hash(lines, start: int, end: int) -> str:
    """Hash of a [start, end] line range (1-indexed, inclusive on both ends)."""
    if start <= 0 or end < start:
        return ""
    try:
        # lines are 0-indexed internally; line_range is 1-indexed.
        slice_ = lines[start - 1 : end]
    except (IndexError, TypeError):
        return ""
    body = "\n".join(slice_)
    return _short(_normalize_body(body).encode("utf-8"))


def cache_token(body_hash: str) -> str:
    """Shorter token (8 chars) used in CSC compact responses."""
    return body_hash[:8] if body_hash else ""


# ---------------------------------------------------------------------------
# L2 semantic analysis (prompt 4). Pure static, no LLM.
# ---------------------------------------------------------------------------


_SIDE_EFFECT_PATTERNS = [
    re.compile(r"\bopen\s*\("),
    re.compile(r"\bprint\s*\("),
    re.compile(r"\.execute\s*\("),
    re.compile(r"\brequests\."),
    re.compile(r"\burllib\."),
    re.compile(r"\bsocket\."),
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bsubprocess\."),
    re.compile(r"\.write\s*\("),
]
_RAISE_RE = re.compile(r"\braise\s+([A-Za-z_][A-Za-z0-9_]*)")
_RETURN_RE = re.compile(r"\breturn\s+(.{1,60})")


def analyze_symbol_semantics(source: str) -> dict:
    """Light static analysis of a symbol body. No LLM, regex-only."""
    raises = sorted(set(_RAISE_RE.findall(source)))
    returns = [m.strip() for m in _RETURN_RE.findall(source)[:3]]
    has_side_effects = any(p.search(source) for p in _SIDE_EFFECT_PATTERNS)
    return {
        "raises": raises,
        "returns": returns,
        "has_side_effects": has_side_effects,
    }


# ---------------------------------------------------------------------------
# Helpers to fill hashes on already-annotated FunctionInfo / ClassInfo.
# Used by project_indexer after each annotate() call.
# ---------------------------------------------------------------------------


def _replace_func_hashes(func: FunctionInfo, lines) -> FunctionInfo:
    from dataclasses import replace

    sig = compute_signature_hash(func)
    body = compute_body_hash(lines, func.line_range.start, func.line_range.end)
    return replace(func, signature_hash=sig, body_hash=body)


def _replace_class_hashes(cls: ClassInfo, lines) -> ClassInfo:
    from dataclasses import replace

    body = compute_body_hash(lines, cls.line_range.start, cls.line_range.end)
    new_methods = [_replace_func_hashes(m, lines) for m in cls.methods]
    return replace(cls, body_hash=body, methods=new_methods)


def fill_hashes(metadata, lines) -> None:
    """Replace metadata.functions and metadata.classes with hashed versions.

    Mutates metadata in place (StructuralMetadata is not frozen). FunctionInfo
    and ClassInfo are frozen so we rebuild via dataclasses.replace.
    """
    metadata.functions = [_replace_func_hashes(f, lines) for f in metadata.functions]
    metadata.classes = [_replace_class_hashes(c, lines) for c in metadata.classes]
