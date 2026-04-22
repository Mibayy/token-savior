"""Tests for the per-project hint injection at switch_project."""

from __future__ import annotations

from token_savior.server_handlers.project import _read_project_hint


class TestReadProjectHint:
    def test_no_hint(self, tmp_path):
        assert _read_project_hint(str(tmp_path)) is None

    def test_hint_in_dotdir(self, tmp_path):
        hint_dir = tmp_path / ".token-savior"
        hint_dir.mkdir()
        (hint_dir / "hint.md").write_text(
            "Deploy with `git push`. Never use Docker locally."
        )
        result = _read_project_hint(str(tmp_path))
        assert result is not None
        assert "Deploy with `git push`" in result

    def test_hint_single_file(self, tmp_path):
        (tmp_path / ".token-savior.md").write_text(
            "Vercel-only. No Next.js middleware."
        )
        result = _read_project_hint(str(tmp_path))
        assert result is not None
        assert "Vercel-only" in result

    def test_dotdir_wins_over_single_file(self, tmp_path):
        hint_dir = tmp_path / ".token-savior"
        hint_dir.mkdir()
        (hint_dir / "hint.md").write_text("dotdir")
        (tmp_path / ".token-savior.md").write_text("singlefile")
        assert _read_project_hint(str(tmp_path)) == "dotdir"

    def test_empty_hint_ignored(self, tmp_path):
        (tmp_path / ".token-savior.md").write_text("   \n\n  ")
        assert _read_project_hint(str(tmp_path)) is None

    def test_long_hint_truncated(self, tmp_path):
        long_content = "x" * 5000
        (tmp_path / ".token-savior.md").write_text(long_content)
        result = _read_project_hint(str(tmp_path))
        assert result is not None
        assert len(result) < 5000
        assert "truncated" in result
