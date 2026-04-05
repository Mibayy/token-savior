"""Tests for config_analyzer.check_duplicates."""

import pytest

from token_savior.config_analyzer import check_duplicates
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
