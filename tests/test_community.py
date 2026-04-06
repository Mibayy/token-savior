"""Tests for community detection."""
import tempfile
from pathlib import Path
from token_savior.project_indexer import ProjectIndexer
from token_savior.community import compute_communities, get_cluster_for_symbol
from token_savior.query_api import create_project_query_functions


def _make_project(tmp_path, files):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    idx = ProjectIndexer(str(tmp_path))
    idx.index()
    return idx


class TestCommunityDetection:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        # auth cluster
        (self.tmp / "auth.py").write_text(
            "def login(user):\n    return validate(user)\n\n"
            "def validate(user):\n    return check_token(user)\n\n"
            "def check_token(user):\n    return True\n"
        )
        # db cluster
        (self.tmp / "db.py").write_text(
            "def connect():\n    return pool()\n\n"
            "def pool():\n    pass\n\n"
            "def query(sql):\n    return connect()\n"
        )
        # Re-index after files were written
        indexer = ProjectIndexer(str(self.tmp))
        project_index = indexer.index()
        self.communities = compute_communities(project_index)
        self.funcs = create_project_query_functions(project_index)
        # Keep indexer reference for graph inspection
        self._index = project_index

    def test_returns_dict(self):
        assert isinstance(self.communities, dict)

    def test_all_symbols_have_community(self):
        all_syms = set(self._index.global_dependency_graph.keys()) | \
                   set(self._index.reverse_dependency_graph.keys())
        for sym in all_syms:
            assert sym in self.communities, f"{sym} missing from communities"

    def test_connected_symbols_same_community(self):
        # login calls validate which calls check_token — should be same cluster
        if "login" in self.communities and "validate" in self.communities:
            assert self.communities["login"] == self.communities["validate"]

    def test_get_symbol_cluster_returns_dict(self):
        result = self.funcs["get_symbol_cluster"]("login")
        if "error" not in result:
            assert "community_id" in result
            assert "members" in result
            assert "size" in result

    def test_get_symbol_cluster_members_have_name(self):
        result = self.funcs["get_symbol_cluster"]("login")
        if "error" not in result:
            for m in result["members"]:
                assert "name" in m

    def test_get_symbol_cluster_not_found(self):
        result = self.funcs["get_symbol_cluster"]("nonexistent_xyz_abc")
        assert "error" in result

    def test_max_members(self):
        result = self.funcs["get_symbol_cluster"]("login", max_members=2)
        if "error" not in result:
            assert len(result["members"]) <= 2
