"""Tests for config_analyzer.check_duplicates."""

import pytest

from token_savior.config_analyzer import check_duplicates, check_orphans, check_secrets
from token_savior.models import ConfigIssue, LineRange, SectionInfo, StructuralMetadata


def _make_meta(source_name, sections, lines=None):
    if lines is None:
        lines = [""] * (max((s.line_range.end for s in sections), default=0) + 1)
    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=sum(len(l) for l in lines),
        lines=lines,
        line_char_offsets=[0] * len(lines),
        sections=sections,
    )


# ---------------------------------------------------------------------------
# Exact duplicate keys at the same nesting level
# ---------------------------------------------------------------------------

class TestExactDuplicates:
    def test_exact_duplicate_same_file_same_level(self):
        """Two sections with the same key at level 1 in the same file → flagged."""
        sections = [
            SectionInfo(title="PORT", level=1, line_range=LineRange(1, 1)),
            SectionInfo(title="PORT", level=1, line_range=LineRange(3, 3)),
        ]
        meta = _make_meta("app.env", sections)
        issues = check_duplicates({"app.env": meta})
        dup = [i for i in issues if i.check == "duplicate"]
        assert len(dup) >= 1
        assert all(i.key == "PORT" for i in dup)
        assert all(i.file == "app.env" for i in dup)

    def test_exact_duplicate_deeper_level(self):
        """Exact duplicate at level 2 is also flagged."""
        sections = [
            SectionInfo(title="host", level=2, line_range=LineRange(2, 2)),
            SectionInfo(title="host", level=2, line_range=LineRange(5, 5)),
        ]
        meta = _make_meta("config.yml", sections)
        issues = check_duplicates({"config.yml": meta})
        dup = [i for i in issues if i.check == "duplicate"]
        assert len(dup) >= 1

    def test_no_false_positive_different_keys(self):
        """Different keys at same level should not be flagged as duplicates."""
        sections = [
            SectionInfo(title="HOST", level=1, line_range=LineRange(1, 1)),
            SectionInfo(title="PORT", level=1, line_range=LineRange(2, 2)),
        ]
        meta = _make_meta("app.env", sections)
        issues = check_duplicates({"app.env": meta})
        assert len(issues) == 0

    def test_no_false_positive_empty(self):
        """Empty config produces no issues."""
        meta = _make_meta("empty.env", [], lines=[""])
        issues = check_duplicates({"empty.env": meta})
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# Similar keys (typos) via Levenshtein distance
# ---------------------------------------------------------------------------

class TestSimilarKeys:
    def test_similar_key_typo_same_file(self):
        """db_host vs db_hsot (distance=2, both >3 chars) → flagged as similar."""
        sections = [
            SectionInfo(title="db_host", level=1, line_range=LineRange(1, 1)),
            SectionInfo(title="db_hsot", level=1, line_range=LineRange(2, 2)),
        ]
        meta = _make_meta("config.ini", sections)
        issues = check_duplicates({"config.ini": meta})
        sim = [i for i in issues if i.check == "duplicate" and "similar" in i.message.lower()]
        assert len(sim) >= 1

    def test_similar_key_distance_1(self):
        """DATABASE_URL vs DATABASE_ULR (distance=2) → flagged."""
        sections = [
            SectionInfo(title="DATABASE_URL", level=1, line_range=LineRange(1, 1)),
            SectionInfo(title="DATABASE_ULR", level=1, line_range=LineRange(2, 2)),
        ]
        meta = _make_meta("app.env", sections)
        issues = check_duplicates({"app.env": meta})
        sim = [i for i in issues if i.check == "duplicate"]
        assert len(sim) >= 1

    def test_short_keys_not_similar_flagged(self):
        """Keys <=3 chars (e.g. 'db' vs 'dc') should not be flagged as similar."""
        sections = [
            SectionInfo(title="db", level=1, line_range=LineRange(1, 1)),
            SectionInfo(title="dc", level=1, line_range=LineRange(2, 2)),
        ]
        meta = _make_meta("app.env", sections)
        issues = check_duplicates({"app.env": meta})
        # distance=1, but keys are <=3 chars → should not be flagged
        sim = [i for i in issues if i.check == "duplicate" and "similar" in i.message.lower()]
        assert len(sim) == 0

    def test_very_different_keys_not_flagged(self):
        """Keys with Levenshtein > 2 should not be flagged as similar."""
        sections = [
            SectionInfo(title="REDIS_HOST", level=1, line_range=LineRange(1, 1)),
            SectionInfo(title="DATABASE_URL", level=1, line_range=LineRange(2, 2)),
        ]
        meta = _make_meta("app.env", sections)
        issues = check_duplicates({"app.env": meta})
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# Different levels → NOT flagged
# ---------------------------------------------------------------------------

class TestDifferentLevels:
    def test_same_key_different_levels_not_flagged(self):
        """server.host and db.host share 'host' but at different levels → NOT flagged."""
        sections = [
            SectionInfo(title="host", level=2, line_range=LineRange(2, 2)),
            SectionInfo(title="host", level=3, line_range=LineRange(6, 6)),
        ]
        meta = _make_meta("config.yml", sections)
        issues = check_duplicates({"config.yml": meta})
        assert len(issues) == 0

    def test_same_key_level1_and_level2_not_flagged(self):
        """Same key at level 1 and level 2 should not be flagged."""
        sections = [
            SectionInfo(title="host", level=1, line_range=LineRange(1, 1)),
            SectionInfo(title="host", level=2, line_range=LineRange(3, 3)),
        ]
        meta = _make_meta("config.yml", sections)
        issues = check_duplicates({"config.yml": meta})
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# Cross-file conflicts
# ---------------------------------------------------------------------------

class TestCrossFileConflicts:
    def test_cross_file_conflict_different_line_content(self):
        """PORT=3000 in file A and PORT=8080 in file B → cross-file conflict."""
        sections_a = [SectionInfo(title="PORT", level=1, line_range=LineRange(1, 1))]
        sections_b = [SectionInfo(title="PORT", level=1, line_range=LineRange(1, 1))]
        lines_a = ["PORT=3000"]
        lines_b = ["PORT=8080"]
        meta_a = _make_meta(".env.dev", sections_a, lines=[""] + lines_a)
        meta_b = _make_meta(".env.prod", sections_b, lines=[""] + lines_b)
        issues = check_duplicates({".env.dev": meta_a, ".env.prod": meta_b})
        cross = [i for i in issues if i.check == "duplicate" and i.key == "PORT"]
        assert len(cross) >= 1

    def test_cross_file_same_content_no_conflict(self):
        """Same key with the same line content across files → no conflict."""
        sections_a = [SectionInfo(title="NODE_ENV", level=1, line_range=LineRange(1, 1))]
        sections_b = [SectionInfo(title="NODE_ENV", level=1, line_range=LineRange(1, 1))]
        lines = ["NODE_ENV=production"]
        meta_a = _make_meta(".env.staging", sections_a, lines=[""] + lines)
        meta_b = _make_meta(".env.prod", sections_b, lines=[""] + lines)
        issues = check_duplicates({".env.staging": meta_a, ".env.prod": meta_b})
        cross = [i for i in issues if i.check == "duplicate" and i.key == "NODE_ENV"]
        assert len(cross) == 0

    def test_cross_file_single_file_no_conflict(self):
        """Single file with no duplicate keys → no cross-file issues."""
        sections = [
            SectionInfo(title="HOST", level=1, line_range=LineRange(1, 1)),
            SectionInfo(title="PORT", level=1, line_range=LineRange(2, 2)),
        ]
        meta = _make_meta("config.env", sections, lines=["", "HOST=localhost", "PORT=3000"])
        issues = check_duplicates({"config.env": meta})
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# TestCheckSecrets
# ---------------------------------------------------------------------------

def _make_simple_meta(source_name: str, lines: list[str]):
    """Build a minimal StructuralMetadata from a list of raw lines.

    Lines are stored with a leading empty string so that lines[1] == first
    line of content (matching the convention used by _make_meta above).
    """
    stored = [""] + lines  # index 0 unused, index N == line N
    return _make_meta(source_name, sections=[], lines=stored)


class TestCheckSecrets:
    # ------------------------------------------------------------------ #
    # Known prefixes                                                       #
    # ------------------------------------------------------------------ #

    def test_sk_prefix_is_error(self):
        """sk- prefix → severity error."""
        meta = _make_simple_meta("app.env", ["OPENAI_KEY=sk-abcdefghijklmnopqrstuvwxyz1234567890"])
        issues = check_secrets({"app.env": meta})
        secrets = [i for i in issues if i.check == "secret"]
        assert len(secrets) >= 1
        assert any(i.severity == "error" for i in secrets)

    def test_ghp_prefix_detected(self):
        """ghp_ prefix → detected."""
        meta = _make_simple_meta("app.env", ["GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"])
        issues = check_secrets({"app.env": meta})
        secrets = [i for i in issues if i.check == "secret"]
        assert len(secrets) >= 1

    def test_begin_private_key_detected(self):
        """-----BEGIN prefix → detected."""
        meta = _make_simple_meta(
            "secrets.env",
            ["PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----MIIEowIBAAKCAQEA"],
        )
        issues = check_secrets({"secrets.env": meta})
        secrets = [i for i in issues if i.check == "secret"]
        assert len(secrets) >= 1
        assert any(i.severity == "error" for i in secrets)

    # ------------------------------------------------------------------ #
    # Suspicious key names                                                 #
    # ------------------------------------------------------------------ #

    def test_suspicious_key_password(self):
        """Key named 'password' with a value → detected."""
        meta = _make_simple_meta("config.env", ["password=s3cr3tP@ssw0rd!"])
        issues = check_secrets({"config.env": meta})
        secrets = [i for i in issues if i.check == "secret"]
        assert len(secrets) >= 1

    # ------------------------------------------------------------------ #
    # URL with embedded credentials                                        #
    # ------------------------------------------------------------------ #

    def test_url_with_credentials(self):
        """DATABASE_URL with user:pass@ → detected as warning."""
        meta = _make_simple_meta(
            "app.env",
            ["DATABASE_URL=postgres://admin:supersecret@db.example.com:5432/mydb"],
        )
        issues = check_secrets({"app.env": meta})
        secrets = [i for i in issues if i.check == "secret"]
        assert len(secrets) >= 1
        assert any(i.severity == "warning" for i in secrets)

    # ------------------------------------------------------------------ #
    # High entropy                                                         #
    # ------------------------------------------------------------------ #

    def test_high_entropy_value(self):
        """A high-entropy 32-char string → detected."""
        # This string has mixed case + digits + symbols → high entropy
        meta = _make_simple_meta(
            "app.env",
            ["SESSION_SECRET=aB3$kX9!mQ2#nZ7@pR5&wT1^yU8*vS4%"],
        )
        issues = check_secrets({"app.env": meta})
        secrets = [i for i in issues if i.check == "secret"]
        assert len(secrets) >= 1

    # ------------------------------------------------------------------ #
    # Normal values — must NOT be flagged                                  #
    # ------------------------------------------------------------------ #

    def test_normal_port_not_flagged(self):
        """PORT=8080 should not be flagged."""
        meta = _make_simple_meta("app.env", ["PORT=8080"])
        issues = check_secrets({"app.env": meta})
        assert len(issues) == 0

    def test_normal_host_not_flagged(self):
        """HOST=localhost should not be flagged."""
        meta = _make_simple_meta("app.env", ["HOST=localhost"])
        issues = check_secrets({"app.env": meta})
        assert len(issues) == 0

    def test_debug_true_not_flagged(self):
        """DEBUG=true should not be flagged."""
        meta = _make_simple_meta("app.env", ["DEBUG=true"])
        issues = check_secrets({"app.env": meta})
        assert len(issues) == 0

    # ------------------------------------------------------------------ #
    # UUID not flagged (even though it is long / looks random)             #
    # ------------------------------------------------------------------ #

    def test_uuid_not_flagged(self):
        """A UUID value should be excluded from entropy check."""
        meta = _make_simple_meta(
            "app.env",
            ["APP_ID=550e8400-e29b-41d4-a716-446655440000"],
        )
        issues = check_secrets({"app.env": meta})
        # UUID must not trigger high-entropy warning
        entropy_issues = [
            i for i in issues
            if i.check == "secret" and "entropy" in i.message.lower()
        ]
        assert len(entropy_issues) == 0

    # ------------------------------------------------------------------ #
    # Masked value in detail                                               #
    # ------------------------------------------------------------------ #

    def test_masked_value_in_detail(self):
        """The full secret must NOT appear in the detail field — only masked."""
        secret = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
        meta = _make_simple_meta("app.env", [f"OPENAI_KEY={secret}"])
        issues = check_secrets({"app.env": meta})
        secrets = [i for i in issues if i.check == "secret" and i.severity == "error"]
        assert len(secrets) >= 1
        issue = secrets[0]
        # The full secret must not be in the detail
        assert secret not in (issue.detail or "")
        # But the masked form (****) must be present
        assert "****" in (issue.detail or "")


# ---------------------------------------------------------------------------
# TestCheckOrphans
# ---------------------------------------------------------------------------

def _make_code_meta(source_name: str, lines: list[str]) -> StructuralMetadata:
    """Build a minimal StructuralMetadata for a code file (no sections)."""
    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(lines),
        total_chars=sum(len(l) for l in lines),
        lines=lines,
        line_char_offsets=[0] * len(lines),
    )


def _make_config_with_key(source_name: str, key: str, line_no: int = 1) -> StructuralMetadata:
    """Build a config StructuralMetadata with a single level-1 section."""
    section = SectionInfo(
        title=key,
        level=1,
        line_range=LineRange(start=line_no, end=line_no),
    )
    lines = [""] + [f"{key}=value"]
    return _make_meta(source_name, sections=[section], lines=lines)


class TestCheckOrphans:

    def test_orphan_key_not_in_code_is_flagged(self):
        """A config key absent from all code → orphan warning."""
        config = {"app.env": _make_config_with_key("app.env", "DB_HOST")}
        code = {"main.py": _make_code_meta("main.py", ['x = 1', 'print("hello")'])}
        issues = check_orphans(config, code)
        orphans = [i for i in issues if i.check == "orphan"]
        assert any(i.key == "DB_HOST" for i in orphans), (
            f"Expected DB_HOST as orphan, got: {[i.key for i in issues]}"
        )

    def test_used_key_via_os_environ_not_flagged(self):
        """os.environ["DB_HOST"] in code → key should NOT be an orphan."""
        config = {"app.env": _make_config_with_key("app.env", "DB_HOST")}
        code = {"main.py": _make_code_meta("main.py", ['host = os.environ["DB_HOST"]'])}
        issues = check_orphans(config, code)
        orphans = [i for i in issues if i.check == "orphan" and i.key == "DB_HOST"]
        assert orphans == [], f"DB_HOST should not be orphan, got: {orphans}"

    def test_ghost_key_in_code_not_in_config_flagged(self):
        """STRIPE_KEY referenced via os.getenv but absent from config → ghost."""
        config: dict = {}
        code = {
            "billing.py": _make_code_meta(
                "billing.py", ["key = os.getenv('STRIPE_KEY', '')"]
            )
        }
        issues = check_orphans(config, code)
        ghosts = [i for i in issues if i.check == "ghost"]
        assert any(i.key == "STRIPE_KEY" for i in ghosts), (
            f"Expected STRIPE_KEY as ghost, got: {[i.key for i in issues]}"
        )

    def test_process_env_key_recognized_as_used(self):
        """process.env.API_KEY in TS code → key should NOT be an orphan."""
        config = {"app.env": _make_config_with_key("app.env", "API_KEY")}
        code = {
            "server.ts": _make_code_meta(
                "server.ts", ["const key = process.env.API_KEY;"]
            )
        }
        issues = check_orphans(config, code)
        orphans = [i for i in issues if i.check == "orphan" and i.key == "API_KEY"]
        assert orphans == [], f"API_KEY should not be orphan, got: {orphans}"

    def test_orphan_config_file_basename_not_in_code(self):
        """Config file whose basename never appears in code → orphan_file."""
        config = {"secrets.env": _make_config_with_key("secrets.env", "MY_KEY")}
        # Code mentions MY_KEY but never "secrets.env"
        code = {
            "app.py": _make_code_meta(
                "app.py", ['val = os.environ["MY_KEY"]']
            )
        }
        issues = check_orphans(config, code)
        file_issues = [i for i in issues if i.check == "orphan_file"]
        assert any("secrets.env" in i.message for i in file_issues), (
            f"Expected orphan_file for secrets.env, got: {[i.message for i in issues]}"
        )

    def test_referenced_config_file_not_flagged(self):
        """Config file basename present in code → no orphan_file."""
        config = {"app.env": _make_config_with_key("app.env", "PORT")}
        code = {
            "loader.py": _make_code_meta(
                "loader.py",
                ['load_dotenv("app.env")', 'port = os.environ["PORT"]'],
            )
        }
        issues = check_orphans(config, code)
        file_issues = [i for i in issues if i.check == "orphan_file"]
        assert file_issues == [], f"app.env should not be orphan_file, got: {file_issues}"

    def test_no_code_files_all_keys_orphan(self):
        """With no code files every config key is an orphan."""
        config = {
            "a.env": _make_config_with_key("a.env", "FOO"),
        }
        issues = check_orphans(config, {})
        orphans = [i for i in issues if i.check == "orphan"]
        assert any(i.key == "FOO" for i in orphans)

    def test_empty_config_and_code_no_issues(self):
        """Empty inputs produce no issues."""
        assert check_orphans({}, {}) == []
