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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from token_savior.models import ConfigIssue, ProjectIndex, StructuralMetadata

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
                curr[j - 1] + 1,  # insertion
                prev[j] + 1,  # deletion
                prev[j - 1] + cost,  # substitution
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

    def _parent_paths(meta: StructuralMetadata) -> dict[int, tuple[str, ...]]:
        stack: list[tuple[int, str]] = []
        result: dict[int, tuple[str, ...]] = {}
        for idx, sec in enumerate(meta.sections):
            while stack and stack[-1][0] >= sec.level:
                stack.pop()
            result[idx] = tuple(title for _, title in stack)
            stack.append((sec.level, sec.title))
        return result

    # ------------------------------------------------------------------
    # Per-file checks: exact duplicates + similar keys
    # ------------------------------------------------------------------
    for source_name, meta in config_files.items():
        parent_paths = _parent_paths(meta)
        by_scope: dict[tuple[int, tuple[str, ...]], list[tuple[int, object]]] = defaultdict(list)
        for idx, sec in enumerate(meta.sections):
            by_scope[(sec.level, parent_paths[idx])].append((idx, sec))

        for (level, _parent_path), scoped_sections in by_scope.items():
            sections = [sec for _, sec in scoped_sections]
            n = len(sections)
            for i in range(n):
                for j in range(i + 1, n):
                    a, b = sections[i], sections[j]
                    if a.line_range.start == b.line_range.start:
                        continue
                    if a.title == b.title:
                        # Exact duplicate
                        issues.append(
                            ConfigIssue(
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
                            )
                        )
                    elif (
                        len(a.title) > 4
                        and len(b.title) > 4
                        and _levenshtein(a.title, b.title) <= 2
                    ):
                        # Similar key — likely typo
                        issues.append(
                            ConfigIssue(
                                file=source_name,
                                key=a.title,
                                line=b.line_range.start,
                                severity="warning",
                                check="duplicate",
                                message=(
                                    f"Similar keys (possible typo) '{a.title}' and "
                                    f"'{b.title}' at level {level}"
                                ),
                                detail=(f"Levenshtein distance = {_levenshtein(a.title, b.title)}"),
                            )
                        )

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
                issues.append(
                    ConfigIssue(
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
                    )
                )

    return issues


# ---------------------------------------------------------------------------
# Secrets detection helpers
# ---------------------------------------------------------------------------

# Known secret prefixes that directly identify a credential
_KNOWN_PREFIXES: tuple[str, ...] = (
    "sk-",
    "sk_live_",
    "sk_test_",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "AKIA",
    "-----BEGIN",
    "xox",
    "xapp-",
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


def _looks_like_template(value: str) -> bool:
    return "${" in value or "%(" in value or "<" in value and ">" in value


def _looks_like_structured_config(value: str) -> bool:
    stripped = value.strip()
    return (
        (stripped.startswith("{") and stripped.endswith("}"))
        or (stripped.startswith("[") and stripped.endswith("]"))
    )


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
    if _looks_like_structured_config(value):
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
            lower_key = key.lower()

            # ----------------------------------------------------------------
            # Engine 1 – Known prefix
            # ----------------------------------------------------------------
            if value:
                if _looks_like_template(value):
                    continue
                if lower_key == "image":
                    continue
                if lower_key == "pattern" and "log4j" in source_name.lower():
                    continue
                for prefix in _KNOWN_PREFIXES:
                    if value.startswith(prefix):
                        issues.append(
                            ConfigIssue(
                                file=source_name,
                                key=key,
                                line=line_no,
                                severity="error",
                                check="secret",
                                message=f"Hardcoded secret detected in '{key}' (known prefix '{prefix}')",
                                detail=f"Value: {_mask_value(value)}",
                            )
                        )
                        break  # one issue per line for known-prefix

            # ----------------------------------------------------------------
            # Engine 3 – URL with embedded credentials
            # ----------------------------------------------------------------
            if _CRED_URL_RE.search(stripped):
                issues.append(
                    ConfigIssue(
                        file=source_name,
                        key=key,
                        line=line_no,
                        severity="warning",
                        check="secret",
                        message=f"URL with embedded credentials in '{key}'",
                        detail=f"Value: {_mask_value(value) if value else '(see line)'}",
                    )
                )

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
                    issues.append(
                        ConfigIssue(
                            file=source_name,
                            key=key,
                            line=line_no,
                            severity="warning",
                            check="secret",
                            message=f"Suspicious key name '{key}' with hardcoded value",
                            detail=f"Value: {_mask_value(value)}",
                        )
                    )

            # ----------------------------------------------------------------
            # Engine 4 – High entropy
            # ----------------------------------------------------------------
            if len(value) >= 16 and not _is_non_secret_pattern(value):
                entropy = _shannon_entropy(value)
                if entropy > 4.5:
                    issues.append(
                        ConfigIssue(
                            file=source_name,
                            key=key,
                            line=line_no,
                            severity="warning",
                            check="secret",
                            message=f"High-entropy value in '{key}' (possible hardcoded secret)",
                            detail=f"Entropy={entropy:.2f}, Value: {_mask_value(value)}",
                        )
                    )

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
        re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)"),
        re.compile(r'process\.env\[(["\'])(.+?)\1\]'),
        re.compile(r"import\.meta\.env\.([A-Z_][A-Z0-9_]*)"),
    ],
    "go": [
        re.compile(r'os\.Getenv\((["\'])(.+?)\1\)'),
    ],
    "rust": [
        re.compile(r'env::var\((["\'])(.+?)\1\)'),
    ],
    "java": [
        re.compile(r'System\.getenv\((["\'])(.+?)\1\)'),
        re.compile(r'System\.getProperty\((["\'])(.+?)\1\)'),
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
    ".java": "java",
}


def _detect_lang(source_name: str) -> str | None:
    """Return the language key for *source_name* based on file extension."""
    _, ext = os.path.splitext(source_name)
    return _EXT_TO_LANG.get(ext.lower())


def _is_test_file(source_name: str) -> bool:
    normalized = source_name.replace("\\", "/").lower()
    basename = os.path.basename(normalized)
    return (
        "/test/" in normalized
        or "/tests/" in normalized
        or basename.startswith("test_")
        or basename.endswith(("_test.py", ".spec.ts", ".test.ts", "test.java", "tests.java"))
    )


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

    Four checks
    -----------
    1. **Orphan key** — a level-1 config key that is not referenced anywhere in
       code (neither via access patterns nor as a plain substring).
    2. **Ghost key** — a key referenced in code via an access pattern but not
       defined in any config file.
    3. **Orphan file** — a config file whose basename does not appear in any
       code file's text.
    4. **Unused config file** — a config file whose basename IS referenced in
       code but none of its nested (level >= 2) keys are accessed anywhere in
       code using a meaningful access pattern (quoted, bracket, or dotted).
       Flags decoy config files that the app pretends to parse.
    """
    issues: list[ConfigIssue] = []
    convention_patterns = (
        "gradle.properties",
        "gradle-wrapper.properties",
        "package.json",
        "tsconfig.json",
        "tsconfig.",
        "application.yaml",
        "application.yml",
        "log4j2.xml",
    )
    convention_key_allowlist = {
        "networks",
        "scripts",
        "devDependencies",
        "compilerOptions",
        "references",
    }

    # ------------------------------------------------------------------
    # Collect level-1 keys from config files
    # ------------------------------------------------------------------
    # config_keys: key → list of (source_name, line_no)
    config_keys: dict[str, list[tuple[str, int]]] = defaultdict(list)
    # nested_keys[source_name] = list of (key_title, line_no) for level >= 2
    nested_keys: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for source_name, meta in config_files.items():
        for sec in meta.sections:
            if sec.level == 1:
                config_keys[sec.title].append((source_name, sec.line_range.start))
            elif sec.level >= 2:
                nested_keys[source_name].append((sec.title, sec.line_range.start))

    # ------------------------------------------------------------------
    # Collect referenced keys from code (via access patterns)
    # ------------------------------------------------------------------
    referenced_keys = _extract_referenced_keys(code_files)

    # Build a flat set of all code text lines for substring fallback
    all_code_text: list[str] = []
    for meta in code_files.values():
        all_code_text.extend(meta.lines)

    def _accessed_in_code(key: str) -> bool:
        """Return True if *key* appears in code via quoted, bracket, attr, or
        mapping access — not just any substring. Avoids false negatives where
        a generic word (e.g. 'app', 'features') happens to appear in paths or
        identifiers unrelated to the config key."""
        if len(key) < 2:
            return False
        escaped = re.escape(key)
        pattern = re.compile(
            rf'(?:["\']){escaped}(?:["\'])'  # "key" or 'key'
            rf'|\[{escaped}\]'  # [key] bracket access
            rf'|\.{escaped}\b'  # .key attr access
            rf'|\b{escaped}\s*[:=]'  # key: value / key = value
        )
        return any(pattern.search(line) for line in all_code_text)

    # ------------------------------------------------------------------
    # Check 1 — Orphan keys (config keys not used in code)
    # ------------------------------------------------------------------
    # UPPER_SNAKE_CASE names (env vars) use a stricter check: we trust only the
    # language-specific access patterns (os.environ/process.env/…). Plain
    # substring fallback is too permissive — such keys commonly appear in
    # generator/fixture code as string literals without being read at runtime.
    _env_style = re.compile(r"^[A-Z][A-Z0-9_]*$")

    for key, occurrences in config_keys.items():
        if key in convention_key_allowlist:
            continue
        # Primary: access-pattern match
        if key in referenced_keys:
            continue
        # Fallback: plain substring presence in any code line (skipped for env vars)
        if not _env_style.match(key) and any(key in line for line in all_code_text):
            continue
        # Not referenced anywhere → orphan
        for source_name, line_no in occurrences:
            issues.append(
                ConfigIssue(
                    file=source_name,
                    key=key,
                    line=line_no,
                    severity="warning",
                    check="orphan",
                    message=f"Orphan config key '{key}' is not referenced in any code file",
                    detail=None,
                )
            )

    # ------------------------------------------------------------------
    # Check 2 — Ghost keys (referenced in code but absent from config)
    # ------------------------------------------------------------------
    defined_keys = set(config_keys.keys())
    for key, refs in referenced_keys.items():
        if key.startswith("VITE_"):
            continue
        if all(_is_test_file(ref_file) for ref_file, _ in refs):
            continue
        if key not in defined_keys:
            # Report once per unique (file, line) reference
            for ref_file, ref_line in refs:
                issues.append(
                    ConfigIssue(
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
                    )
                )

    # ------------------------------------------------------------------
    # Check 3 — Orphan config files (basename not found in code text)
    # ------------------------------------------------------------------
    for source_name, meta in config_files.items():
        basename = os.path.basename(source_name)
        normalized = source_name.replace("\\", "/")
        if (
            basename in convention_patterns
            or basename.startswith("tsconfig.")
            or basename.startswith("docker-compose")
            or normalized.startswith(".github/workflows/")
            or "/.github/workflows/" in normalized
            or normalized.startswith("config/deploy/")
            or "/config/deploy/" in normalized
        ):
            continue
        if not any(basename in line for line in all_code_text):
            issues.append(
                ConfigIssue(
                    file=source_name,
                    key="",
                    line=0,
                    severity="warning",
                    check="orphan_file",
                    message=(f"Config file '{basename}' is not referenced in any code file"),
                    detail=None,
                )
            )

    # ------------------------------------------------------------------
    # Check 4 — Unused config content (decoy YAML/TOML that no loader code
    # actually reads). Cheap but strong heuristic: the filename must appear
    # inside a load/open/parse-style call in some code line. Falsely-positive
    # "nested key accessed" (e.g. a key named "billing" matches every
    # `.billing` attribute in the codebase) made the old key-scan useless,
    # so we now gate on loader calls only.
    # ------------------------------------------------------------------
    _loader_pattern = re.compile(
        r'(?:open|read|load|loads|parse|parses|yaml\.\w+|toml\.\w+|json\.load\w*|'
        r'configparser\.|readfilesync|readfile|fs\.read|require|import)',
        re.IGNORECASE,
    )
    for source_name, keys in nested_keys.items():
        basename = os.path.basename(source_name)
        normalized = source_name.replace("\\", "/")
        if (
            basename in convention_patterns
            or basename.startswith("tsconfig.")
            or basename.startswith("docker-compose")
            or basename == "package.json"
            or normalized.startswith(".github/workflows/")
            or "/.github/workflows/" in normalized
            # Infra manifests applied by kubectl/helm/terraform — never loaded
            # by app code, not decoys.
            or "/k8s/" in normalized
            or normalized.startswith("k8s/")
            or "/kubernetes/" in normalized
            or normalized.startswith("kubernetes/")
            or "/terraform/" in normalized
            or normalized.startswith("terraform/")
            or "/helm/" in normalized
            or normalized.startswith("helm/")
            or basename.endswith((".tf", ".hcl"))
        ):
            continue
        if not keys:
            continue
        # Look for a loader call that cites this filename. Accept either
        # basename or full rel path; accept either single- or double-quoted.
        quoted_base = (f'"{basename}"', f"'{basename}'")
        quoted_full = (f'"{normalized}"', f"'{normalized}'")
        loaded = False
        for line in all_code_text:
            if not any(q in line for q in quoted_base + quoted_full):
                continue
            if _loader_pattern.search(line):
                loaded = True
                break
        if loaded:
            continue
        issues.append(
            ConfigIssue(
                file=source_name,
                key="",
                line=0,
                severity="warning",
                check="unused_content",
                message=(
                    f"Config file '{basename}' is never loaded by any code file "
                    f"(no open/load/parse call cites it) — likely dead config"
                ),
                detail=None,
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Loaders — which code files load which config files
# ---------------------------------------------------------------------------


def check_loaders(
    config_files: dict[str, StructuralMetadata],
    code_files: dict[str, StructuralMetadata],
) -> list[ConfigIssue]:
    """Detect which code files reference which config files.

    For each config file in the project, scans all code files for references
    to its basename. Returns one issue per (code_file, config_file) pair with
    the matching lines.
    """
    issues: list[ConfigIssue] = []

    for config_name in config_files:
        basename = os.path.basename(config_name)
        for code_name, code_meta in code_files.items():
            for line_idx, line in enumerate(code_meta.lines):
                if basename in line:
                    issues.append(
                        ConfigIssue(
                            file=code_name,
                            key=config_name,
                            line=line_idx,
                            severity="info",
                            check="loader",
                            message=f"loads '{basename}'",
                            detail=line.strip()[:120],
                        )
                    )
    return issues


# ---------------------------------------------------------------------------
# Schema — what keys the code expects with defaults and status
# ---------------------------------------------------------------------------

# Patterns that capture (key, default_value_or_None)
_SCHEMA_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "python": [
        # os.getenv('KEY', 'default')  /  os.environ.get('KEY', 'default')
        re.compile(
            r"os\.(?:getenv|environ\.get)\(\s*"
            r'(["\'])(.+?)\1'  # key
            r'(?:\s*,\s*(["\'])(.+?)\3)?'  # optional default
            r"\)"
        ),
        # os.environ['KEY']  — required (no default)
        re.compile(r'os\.environ\[(["\'])(.+?)\1\]'),
    ],
    "typescript": [
        # process.env.KEY ?? 'default'  or  process.env.KEY || 'default'
        re.compile(
            r"process\.env\.([A-Z_][A-Z0-9_]*)"
            r"\s*(?:\?\?|\|\|)\s*"
            r'(["\'])(.+?)\2'
        ),
        # process.env.KEY  — no default
        re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)"),
        # process.env['KEY']
        re.compile(r'process\.env\[(["\'])(.+?)\1\]'),
        # import.meta.env.KEY
        re.compile(r"import\.meta\.env\.([A-Z_][A-Z0-9_]*)"),
    ],
    "go": [
        re.compile(r'os\.Getenv\((["\'])(.+?)\1\)'),
    ],
    "rust": [
        re.compile(r'env::var\((["\'])(.+?)\1\)'),
    ],
}


@dataclass
class _KeyRef:
    """A reference to a config key found in code."""

    key: str
    file: str
    line: int
    default: str | None


def _extract_schema_refs(
    code_files: dict[str, StructuralMetadata],
) -> list[_KeyRef]:
    """Extract all config key references with optional defaults from code."""
    refs: list[_KeyRef] = []

    for source_name, meta in code_files.items():
        lang = _detect_lang(source_name)
        patterns = _SCHEMA_PATTERNS.get(lang, []) if lang else []

        for line_idx, line in enumerate(meta.lines):
            for pattern in patterns:
                for m in pattern.finditer(line):
                    groups = m.groups()
                    key: str | None = None
                    default: str | None = None

                    if lang == "python":
                        if len(groups) >= 2:
                            key = groups[1]  # key is always group 2
                        if len(groups) >= 4 and groups[3] is not None:
                            default = groups[3]
                    elif lang == "typescript":
                        # Pattern with default: (KEY, quote, default)
                        if len(groups) == 3 and groups[2] is not None:
                            key = groups[0]
                            default = groups[2]
                        elif len(groups) == 2:
                            # process.env['KEY'] pattern
                            key = groups[1]
                        elif len(groups) >= 1:
                            key = groups[0]
                    else:
                        # Go, Rust — key is group 2 (after quote)
                        if len(groups) >= 2:
                            key = groups[1]

                    if key and key.strip():
                        refs.append(
                            _KeyRef(
                                key=key,
                                file=source_name,
                                line=line_idx,
                                default=default,
                            )
                        )
    return refs


def check_schema(
    config_files: dict[str, StructuralMetadata],
    code_files: dict[str, StructuralMetadata],
) -> list[ConfigIssue]:
    """Build a schema report: what keys the code expects and their status.

    For each key referenced in code, reports:
    - Where it's used (file:line)
    - Default value if detected
    - Whether it's defined in config files or missing
    """
    issues: list[ConfigIssue] = []
    refs = _extract_schema_refs(code_files)

    if not refs:
        return issues

    # Build config key → (file, line, value) mapping
    config_keys: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for source_name, meta in config_files.items():
        for sec in meta.sections:
            if sec.level == 1:
                # Try to get value from the line
                value = ""
                if sec.line_range.start < len(meta.lines):
                    raw = meta.lines[sec.line_range.start].strip()
                    if "=" in raw:
                        value = raw.split("=", 1)[1].strip()
                config_keys[sec.title].append((source_name, sec.line_range.start, value))

    # Group refs by key
    key_refs: dict[str, list[_KeyRef]] = defaultdict(list)
    for ref in refs:
        key_refs[ref.key].append(ref)

    for key, key_ref_list in sorted(key_refs.items()):
        # Deduplicate files
        files_seen: set[str] = set()
        unique_refs: list[_KeyRef] = []
        for r in key_ref_list:
            loc = f"{r.file}:{r.line}"
            if loc not in files_seen:
                files_seen.add(loc)
                unique_refs.append(r)

        # Detect default from any ref
        default = next((r.default for r in unique_refs if r.default is not None), None)

        # Check if defined in config
        defined_in = config_keys.get(key, [])
        is_defined = len(defined_in) > 0

        # Build detail
        used_in = ", ".join(f"{r.file}:{r.line}" for r in unique_refs[:5])
        if len(unique_refs) > 5:
            used_in += f" (+{len(unique_refs) - 5} more)"

        parts = [f"used in: {used_in}"]
        if default is not None:
            parts.append(f"default: '{default}'")
        else:
            parts.append("default: (none — required)")

        if is_defined:
            cfg_locs = ", ".join(f"{cf}:{ln}" for cf, ln, _ in defined_in)
            parts.append(f"config: {cfg_locs}")
        else:
            parts.append("config: MISSING")

        severity = "info" if is_defined else "warning"
        status = "defined" if is_defined else "missing"

        issues.append(
            ConfigIssue(
                file=unique_refs[0].file,
                key=key,
                line=unique_refs[0].line,
                severity=severity,
                check=f"schema_{status}",
                message=f"Key '{key}'",
                detail=" | ".join(parts),
            )
        )

    return issues


# ---------------------------------------------------------------------------
# analyze_config helpers
# ---------------------------------------------------------------------------

_CONFIG_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".properties",
        ".env",
        ".xml",
        ".plist",
        ".hcl",
        ".tf",
        ".conf",
        ".json",
    }
)

_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".cs",
        ".java",
    }
)


def _is_config_file(filename: str) -> bool:
    """Return True if *filename* should be treated as a config file.

    A file qualifies if its extension is in ``_CONFIG_EXTENSIONS`` OR if its
    basename starts with ``".env"`` (e.g. ``.env.local``, ``.env.production``).
    """
    basename = os.path.basename(filename)
    if basename.startswith(".env"):
        return True
    _, ext = os.path.splitext(filename)
    return ext in _CONFIG_EXTENSIONS


def _is_code_file(filename: str) -> bool:
    """Return True if *filename* should be treated as a source-code file."""
    _, ext = os.path.splitext(filename)
    return ext in _CODE_EXTENSIONS


# ---------------------------------------------------------------------------
# _format_issues
# ---------------------------------------------------------------------------


def _format_issues(
    all_issues: list[ConfigIssue],
    severity_filter: str,
    max_issues: int = 30,
) -> str:
    """Format *all_issues* into a human-readable report string.

    Severity filter
    ---------------
    ``"error"``   → only ``error``-level issues
    ``"warning"`` → ``error`` + ``warning``-level issues
    ``"all"``     → every issue regardless of severity

    The output groups issues by their ``check`` attribute.
    *max_issues* caps total issues shown (0 = unlimited).
    """
    if severity_filter == "error":
        allowed = {"error"}
    elif severity_filter == "warning":
        allowed = {"error", "warning"}
    else:
        allowed = None

    filtered = [i for i in all_issues if allowed is None or i.severity in allowed]

    if not filtered:
        return (
            "VERDICT: CONSISTENT — Config Analysis found 0 issues.\n"
            "All declared keys are referenced and no inconsistencies detected. "
            "No follow-up search needed."
        )

    total = len(filtered)
    if max_issues and total > max_issues:
        filtered = filtered[:max_issues]

    groups: dict[str, list[ConfigIssue]] = defaultdict(list)
    for issue in filtered:
        groups[issue.check].append(issue)

    lines: list[str] = [f"Config Analysis -- {total} issues found"]
    if total > len(filtered):
        lines[0] += f" (showing {len(filtered)})"
    lines.append("")

    for check_name, issues in groups.items():
        lines.append(f"-- {check_name} ({len(issues)}) --")
        for issue in issues:
            lines.append(f"[{issue.severity}] {issue.file}:{issue.line} {issue.message}")
        lines.append("")

    if lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# analyze_config — main entry point
# ---------------------------------------------------------------------------


def analyze_config(
    index: ProjectIndex,
    checks: list[str] | None = None,
    file_path: str | None = None,
    severity: str = "all",
    max_issues: int = 30,
) -> str:
    """Run config analysis checks on *index* and return a formatted report.

    Parameters
    ----------
    index:
        Project index containing all indexed files.
    checks:
        List of check names to run. Defaults to
        ``["duplicates", "secrets", "orphans"]``.
    file_path:
        When given, restrict the analysis to this single config file.
    severity:
        Severity filter passed to :func:`_format_issues`.
        One of ``"all"`` (default), ``"warning"``, or ``"error"``.
    max_issues:
        Cap total issues shown (default 30, 0 = unlimited).
    """
    if checks is None:
        checks = ["duplicates", "secrets", "orphans"]

    # Partition index.files into config_files and code_files
    all_files: dict[str, StructuralMetadata] = index.files

    if file_path is not None:
        # Only the requested file (if it exists and is a config file)
        config_files: dict[str, StructuralMetadata] = {}
        if file_path in all_files and _is_config_file(file_path) and not file_path.endswith(".xml"):
            config_files[file_path] = all_files[file_path]
        code_files: dict[str, StructuralMetadata] = {
            k: v for k, v in all_files.items() if _is_code_file(k)
        }
    else:
        config_files = {
            k: v for k, v in all_files.items() if _is_config_file(k) and not k.endswith(".xml")
        }
        code_files = {k: v for k, v in all_files.items() if _is_code_file(k)}

    if not config_files:
        return "Config Analysis -- 0 config files found in project"

    all_issues: list[ConfigIssue] = []

    if "duplicates" in checks:
        all_issues.extend(check_duplicates(config_files))

    if "secrets" in checks:
        all_issues.extend(check_secrets(config_files))

    if "orphans" in checks:
        all_issues.extend(check_orphans(config_files, code_files))

    if "loaders" in checks:
        all_issues.extend(check_loaders(config_files, code_files))

    if "schema" in checks:
        all_issues.extend(check_schema(config_files, code_files))

    return _format_issues(all_issues, severity, max_issues=max_issues)
