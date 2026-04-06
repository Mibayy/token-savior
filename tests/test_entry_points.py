"""Tests for entry point detection."""
import pytest
from pathlib import Path
import tempfile
from token_savior.project_indexer import ProjectIndexer


def _make_indexer(tmp_path, files: dict[str, str]) -> ProjectIndexer:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    idx = ProjectIndexer(str(tmp_path))
    idx.index()
    return idx


class TestEntryPoints:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        from pathlib import Path
        tmp = Path(self.tmp)
        (tmp / "main.py").write_text(
            "def main():\n    run()\n\ndef run():\n    pass\n\ndef helper():\n    pass\n"
        )
        (tmp / "routes" / "users.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp / "routes" / "users.py").write_text(
            "def handle_get_user(req):\n    return {}\n\ndef internal():\n    pass\n"
        )
        idx = ProjectIndexer(self.tmp)
        project_index = idx.index()
        from token_savior.query_api import create_project_query_functions
        self.funcs = create_project_query_functions(project_index)

    def test_returns_list(self):
        result = self.funcs["get_entry_points"]()
        assert isinstance(result, list)

    def test_scores_between_0_and_1(self):
        result = self.funcs["get_entry_points"]()
        for r in result:
            assert 0.0 <= r["score"] <= 1.0

    def test_main_is_entry_point(self):
        result = self.funcs["get_entry_points"]()
        names = [r["name"] for r in result]
        assert "main" in names

    def test_handler_in_routes_scores_high(self):
        result = self.funcs["get_entry_points"]()
        handler = next((r for r in result if r["name"] == "handle_get_user"), None)
        assert handler is not None
        assert handler["score"] > 0.5

    def test_has_reasons(self):
        result = self.funcs["get_entry_points"]()
        for r in result:
            assert isinstance(r["reasons"], list)
            assert len(r["reasons"]) > 0

    def test_max_results(self):
        result = self.funcs["get_entry_points"](max_results=1)
        assert len(result) <= 1
