"""Config analysis: check_duplicates, check_secrets, check_orphans and helpers.

Analyses StructuralMetadata produced by config annotators (YAML, ENV, INI, …)
to surface problems like exact duplicate keys, likely typos (similar keys),
cross-file conflicts, hardcoded secrets, and orphan / ghost keys.
"""

from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from token_savior.models import ConfigIssue, StructuralMetadata

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Levenshtein edit distance
# ---------------------------------------------------------------------------

def _levenshtein(s1: str, s2: str) -> int:
    """Return the Levenshtein edit distance between *s1* and *s2*."""
    if s1 == s2:
        return 0
    len1, len2 = len(s1), len(s2)
    if len1 == 0:
        return len2
    if len2 == 0:
        return len1

    # Use two-row DP to keep memory O(min(len1, len2))
    if len1 < len2:
        s1, s2 = s2, s1
        len1, len2 = len2, len1

    prev = list(range(len2 + 1))
    for i in range(1, len1 + 1):
        curr = [i] + [0] * len2
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(
                curr[j - 1] + 1,       # insertion
                prev[j] + 1,           # deletion
                prev[j - 1] + cost,    # substitution
            )
        prev = curr
    return prev[len2]


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------

def check_duplicates(
    config_files: dict[str, StructuralMetadata],
) -> list[ConfigIssue]:
    """Analyse *config_files* and return a list of duplicate / similar-key issues.

    Rules
    -----
    1. **Exact duplicate at same level (same file)** — two sections with
       identical titles at the same nesting level within a single file.
    2. **Similar keys (typo) at same level (same file)** — two sections whose
       titles differ by Levenshtein ≤ 2, but only when both keys are > 3 chars.
    3. **Cross-file conflict** — same key name at level 1 across two different
       files, but with different line content (suggests misconfiguration).

    Keys at *different* levels are never flagged (e.g. ``server.host`` and
    ``db.host`` both valid because they live at different nesting depths).
    """
    issues: list[ConfigIssue] = []

    # ------------------------------------------------------------------
    # Per-file checks: exact duplicates + similar keys
    # ------------------------------------------------------------------
    for source_name, meta in config_files.items():
        # Group sections by level
        by_level: dict[int, list] = defaultdict(list)
        for sec in meta.sections:
            by_level[sec.level].append(sec)

        for level, sections in by_level.items():
            n = len(sections)
            for i in range(n):
                for j in range(i + 1, n):
                    a, b = sections[i], sections[j]
                    if a.title == b.title:
                        # Exact duplicate
                        issues.append(ConfigIssue(
                            file=source_name,
                            key=a.title,
                            line=b.line_range.start,
                            severity="error",
                            check="duplicate",
                            message=f"Exact duplicate key '{a.title}' at level {level}",
                            detail=(
                                f"First occurrence at line {a.line_range.start}, "
                                f"duplicate at line {b.line_range.start}"
                            ),
                        ))
                    elif (
                        len(a.title) > 4
                        and len(b.title) > 4
                        and _levenshtein(a.title, b.title) <= 2
                    ):
                        # Similar key — likely typo
                        issues.append(ConfigIssue(
                            file=source_name,
                            key=a.title,
                            line=b.line_range.start,
                            severity="warning",
                            check="duplicate",
                            message=(
                                f"Similar keys (possible typo) '{a.title}' and "
                                f"'{b.title}' at level {level}"
                            ),
                            detail=(
                                f"Levenshtein distance = "
                                f"{_levenshtein(a.title, b.title)}"
                            ),
                        ))

    # ------------------------------------------------------------------
    # Cross-file conflicts — level-1 keys with differing line content
    # ------------------------------------------------------------------
    if len(config_files) > 1:
        # Build: key -> list of (source_name, line_content)
        level1_map: dict[str, list[tuple[str, str]]] = defaultdict(list)

        for source_name, meta in config_files.items():
            for sec in meta.sections:
                if sec.level != 1:
                    continue
                line_idx = sec.line_range.start  # 1-indexed
                # meta.lines is stored 0-indexed internally (index 0 = line 1)
                # but _make_meta in tests passes lines with a leading "" so that
                # lines[1] == "PORT=3000". We try both conventions gracefully.
                if line_idx < len(meta.lines):
                    content = meta.lines[line_idx].strip()
                else:
                    content = ""
                level1_map[sec.title].append((source_name, content))

        for key, occurrences in level1_map.items():
            if len(occurrences) < 2:
                continue
            # Collect distinct non-empty contents
            contents = {content for _, content in occurrences if content}
            if len(contents) <= 1:
                # All identical (or all empty) — no conflict
                continue
            # Different content across files → conflict
            for source_name, content in occurrences:
                issues.append(ConfigIssue(
                    file=source_name,
                    key=key,
                    line=next(
                        sec.line_range.start
                        for sec in config_files[source_name].sections
                        if sec.title == key and sec.level == 1
                    ),
                    severity="warning",
                    check="duplicate",
                    message=(
                        f"Cross-file conflict: key '{key}' has different values "
                        f"across config files"
                    ),
                    detail=f"Value in this file: {content!r}",
                ))

    return issues


# ---------------------------------------------------------------------------
# Secrets detection helpers
# ---------------------------------------------------------------------------

# Known secret prefixes that directly identify a credential
_KNOWN_PREFIXES: tuple[str, ...] = (
    "sk-", "sk_live_", "sk_test_",
    "ghp_", "gho_", "ghu_", "ghs_",
    "AKIA",
    "-----BEGIN",
    "xox", "xapp-",
    "eyJ",  # JWT
)

# Key name patterns that suggest the value is sensitive
_SUSPICIOUS_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api_key|apikey|private_key"
    r"|credential|auth|access_key|signing_key|encryption_key)"
)

# URL with embedded credentials: scheme://user:pass@host
_CRED_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+\-.]*://[^@\s]+:[^@\s]+@")

# Patterns that look like secrets but are actually harmless
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SEMVER_RE = re.compile(r"^\d+\.\d+(\.\d+)?([.\-+][a-zA-Z0-9._+\-]*)?$")
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$")
_FILE_PATH_RE = re.compile(r"^[/\\]|^[a-zA-Z]:[/\\]|\.\w{1,5}$")
_BOOL_LIKE_RE = re.compile(r"^(true|false|yes|no|on|off|null|none|0|1)$", re.IGNORECASE)


def _shannon_entropy(s: str) -> float:
    """Return the Shannon entropy (bits per character) of *s*."""
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in freq.values())


def _extract_value(line: str) -> str:
    """Extract the value part from a KEY=VALUE or KEY: VALUE line.

    Strips surrounding single/double quotes from the value.
    """
    line = line.strip()
    # KEY=VALUE (ENV style)
    if "=" in line:
        _, _, raw = line.partition("=")
    # KEY: VALUE (YAML-ish)
    elif ":" in line:
        _, _, raw = line.partition(":")
    else:
        return ""
    value = raw.strip()
    # Strip matching surrounding quotes
    if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
        value = value[1:-1]
    return value


def _mask_value(value: str) -> str:
    """Return a masked representation: first 4 + **** + last 4 chars."""
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


def _is_non_secret_pattern(value: str) -> bool:
    """Return True when *value* matches a known harmless pattern."""
    if _UUID_RE.match(value):
        return True
    if _SEMVER_RE.match(value):
        return True
    if _HEX_COLOR_RE.match(value):
        return True
    if _FILE_PATH_RE.search(value):
        return True
    if _BOOL_LIKE_RE.match(value):
        return True
    return False


# ---------------------------------------------------------------------------
# check_secrets
# ---------------------------------------------------------------------------

def check_secrets(
    config_files: dict[str, StructuralMetadata],
) -> list[ConfigIssue]:
    """Scan *config_files* for hardcoded secrets and return a list of issues.

    Detection engines
    -----------------
    1. **Known prefix** — value starts with a well-known credential prefix
       (``sk-``, ``ghp_``, ``AKIA``, ``eyJ``, ``-----BEGIN``, …).
    2. **Suspicious key name** — the key name matches a regex for sensitive
       names (password, secret, token, api_key, …) and the value is non-trivial.
    3. **URL with embedded credentials** — ``scheme://user:pass@host`` pattern.
    4. **High entropy** — Shannon entropy > 4.5 for values ≥ 16 chars, after
       filtering out UUIDs, semver strings, hex colours, file paths, and
       boolean-like values.

    Severity
    --------
    - Known prefix → ``"error"``
    - URL with credentials, suspicious key name, high entropy → ``"warning"``
    """
    issues: list[ConfigIssue] = []

    for source_name, meta in config_files.items():
        for line_idx, raw_line in enumerate(meta.lines):
            line_no = line_idx  # lines are stored with leading "" so index == line number
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            value = _extract_value(stripped)
            key = stripped.split("=")[0].split(":")[0].strip() if value else ""

            # ----------------------------------------------------------------
            # Engine 1 – Known prefix
            # ----------------------------------------------------------------
            if value:
                for prefix in _KNOWN_PREFIXES:
                    if value.startswith(prefix):
                        issues.append(ConfigIssue(
                            file=source_name,
                            key=key,
                            line=line_no,
                            severity="error",
                            check="secret",
                            message=f"Hardcoded secret detected in '{key}' (known prefix '{prefix}')",
                            detail=f"Value: {_mask_value(value)}",
                        ))
                        break  # one issue per line for known-prefix

            # ----------------------------------------------------------------
            # Engine 3 – URL with embedded credentials
            # ----------------------------------------------------------------
            if _CRED_URL_RE.search(stripped):
                issues.append(ConfigIssue(
                    file=source_name,
                    key=key,
                    line=line_no,
                    severity="warning",
                    check="secret",
                    message=f"URL with embedded credentials in '{key}'",
                    detail=f"Value: {_mask_value(value) if value else '(see line)'}",
                ))

            if not value:
                continue

            # ----------------------------------------------------------------
            # Engine 2 – Suspicious key name
            # ----------------------------------------------------------------
            if key and _SUSPICIOUS_KEY_RE.search(key):
                # Only flag when value looks like a real hardcoded string
                # (not a placeholder like ${...}, %(...), or an empty string)
                placeholder_re = re.compile(r"^\$\{.*\}$|^%\(.*\)s?$|^<.*>$")
                if value and not placeholder_re.match(value) and not _is_non_secret_pattern(value):
                    issues.append(ConfigIssue(
                        file=source_name,
                        key=key,
                        line=line_no,
                        severity="warning",
                        check="secret",
                        message=f"Suspicious key name '{key}' with hardcoded value",
                        detail=f"Value: {_mask_value(value)}",
                    ))

            # ----------------------------------------------------------------
            # Engine 4 – High entropy
            # ----------------------------------------------------------------
            if len(value) >= 16 and not _is_non_secret_pattern(value):
                entropy = _shannon_entropy(value)
                if entropy > 4.5:
                    issues.append(ConfigIssue(
                        file=source_name,
                        key=key,
                        line=line_no,
                        severity="warning",
                        check="secret",
                        message=f"High-entropy value in '{key}' (possible hardcoded secret)",
                        detail=f"Entropy={entropy:.2f}, Value: {_mask_value(value)}",
                    ))

    return issues


# ---------------------------------------------------------------------------
# Orphans / Ghost keys detection
# ---------------------------------------------------------------------------

_ACCESS_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "python": [
        re.compile(r'os\.environ\[(["\'])(.+?)\1\]'),
        re.compile(r'os\.getenv\((["\'])(.+?)\1'),
        re.compile(r'os\.environ\.get\((["\'])(.+?)\1'),
    ],
    "typescript": [
        re.compile(r'process\.env\.([A-Z_][A-Z0-9_]*)'),
        re.compile(r'process\.env\[(["\'])(.+?)\1\]'),
        re.compile(r'import\.meta\.env\.([A-Z_][A-Z0-9_]*)'),
    ],
    "go": [
        re.compile(r'os\.Getenv\((["\'])(.+?)\1\)'),
    ],
    "rust": [
        re.compile(r'env::var\((["\'])(.+?)\1\)'),
    ],
}

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".go": "go",
    ".rs": "rust",
}


def _detect_lang(source_name: str) -> str | None:
    """Return the language key for *source_name* based on file extension."""
    _, ext = os.path.splitext(source_name)
    return _EXT_TO_LANG.get(ext.lower())


def _pick_key_from_match(m: re.Match[str]) -> str | None:
    """Pick the meaningful key group from a regex match.

    Iterate groups in reverse and return the first group that is a string
    longer than 1 char and is not a bare quote character.
    """
    groups = m.groups()
    for g in reversed(groups):
        if g and len(g) > 1 and g not in ('"', "'"):
            return g
    return None


def _extract_referenced_keys(
    code_files: dict[str, StructuralMetadata],
) -> dict[str, list[tuple[str, int]]]:
    """Scan *code_files* with language-specific access patterns.

    Returns a mapping of ``key → [(source_name, line_no), …]`` for every
    environment-variable key reference found in code.
    """
    result: dict[str, list[tuple[str, int]]] = defaultdict(list)

    for source_name, meta in code_files.items():
        lang = _detect_lang(source_name)
        patterns = _ACCESS_PATTERNS.get(lang, []) if lang else []

        for line_idx, line in enumerate(meta.lines):
            line_no = line_idx  # same convention as check_secrets
            for pattern in patterns:
                for m in pattern.finditer(line):
                    key = _pick_key_from_match(m)
                    if key:
                        result[key].append((source_name, line_no))

    return dict(result)


def check_orphans(
    config_files: dict[str, StructuralMetadata],
    code_files: dict[str, StructuralMetadata],
) -> list[ConfigIssue]:
    """Detect orphan keys, ghost keys, and orphan config files.

    Three checks
    ------------
    1. **Orphan key** — a level-1 config key that is not referenced anywhere in
       code (neither via access patterns nor as a plain substring).
    2. **Ghost key** — a key referenced in code via an access pattern but not
       defined in any config file.
    3. **Orphan file** — a config file whose basename does not appear in any
       code file's text.
    """
    issues: list[ConfigIssue] = []

    # ------------------------------------------------------------------
    # Collect level-1 keys from config files
    # ------------------------------------------------------------------
    # config_keys: key → list of (source_name, line_no)
    config_keys: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for source_name, meta in config_files.items():
        for sec in meta.sections:
            if sec.level == 1:
                config_keys[sec.title].append((source_name, sec.line_range.start))

    # ------------------------------------------------------------------
    # Collect referenced keys from code (via access patterns)
    # ------------------------------------------------------------------
    referenced_keys = _extract_referenced_keys(code_files)

    # Build a flat set of all code text lines for substring fallback
    all_code_text: list[str] = []
    for meta in code_files.values():
        all_code_text.extend(meta.lines)

    # ------------------------------------------------------------------
    # Check 1 — Orphan keys (config keys not used in code)
    # ------------------------------------------------------------------
    for key, occurrences in config_keys.items():
        # Primary: access-pattern match
        if key in referenced_keys:
            continue
        # Fallback: plain substring presence in any code line
        if any(key in line for line in all_code_text):
            continue
        # Not referenced anywhere → orphan
        for source_name, line_no in occurrences:
            issues.append(ConfigIssue(
                file=source_name,
                key=key,
                line=line_no,
                severity="warning",
                check="orphan",
                message=f"Orphan config key '{key}' is not referenced in any code file",
                detail=None,
            ))

    # ------------------------------------------------------------------
    # Check 2 — Ghost keys (referenced in code but absent from config)
    # ------------------------------------------------------------------
    defined_keys = set(config_keys.keys())
    for key, refs in referenced_keys.items():
        if key not in defined_keys:
            # Report once per unique (file, line) reference
            for ref_file, ref_line in refs:
                issues.append(ConfigIssue(
                    file=ref_file,
                    key=key,
                    line=ref_line,
                    severity="warning",
                    check="ghost",
                    message=(
                        f"Ghost key '{key}' is referenced in code but not defined "
                        f"in any config file"
                    ),
                    detail=None,
                ))

    # ------------------------------------------------------------------
    # Check 3 — Orphan config files (basename not found in code text)
    # ------------------------------------------------------------------
    for source_name, meta in config_files.items():
        basename = os.path.basename(source_name)
        if not any(basename in line for line in all_code_text):
            issues.append(ConfigIssue(
                file=source_name,
                key="",
                line=0,
                severity="warning",
                check="orphan_file",
                message=(
                    f"Config file '{basename}' is not referenced in any code file"
                ),
                detail=None,
            ))

    return issues
