"""Tests for community detection."""

import tempfile
from pathlib import Path

from token_savior.community import compute_communities, get_cluster_for_symbol
from token_savior.models import ClassInfo, FunctionInfo, LineRange, ProjectIndex, StructuralMetadata
from token_savior.project_indexer import ProjectIndexer
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
        all_syms = set(self._index.global_dependency_graph.keys()) | set(
            self._index.reverse_dependency_graph.keys()
        )
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

    def test_get_cluster_resolves_class_members_as_classes(self):
        index = ProjectIndex(
            root_path="/project",
            files={
                "src/node.py": StructuralMetadata(
                    source_name="node.py",
                    total_lines=4,
                    total_chars=60,
                    lines=["class SampleAggregationNode:", "    pass", "", ""],
                    line_char_offsets=[],
                    classes=[
                        ClassInfo(
                            name="SampleAggregationNode",
                            line_range=LineRange(1, 2),
                            base_classes=[],
                            methods=[],
                            decorators=[],
                            docstring=None,
                        )
                    ],
                ),
                "src/service.py": StructuralMetadata(
                    source_name="service.py",
                    total_lines=5,
                    total_chars=90,
                    lines=["class SampleQueryService:", "    def build(self):", "        return None", "", ""],
                    line_char_offsets=[],
                    functions=[
                        FunctionInfo(
                            name="build",
                            qualified_name="SampleQueryService.build",
                            line_range=LineRange(2, 3),
                            parameters=["self"],
                            decorators=[],
                            docstring=None,
                            is_method=True,
                            parent_class="SampleQueryService",
                        )
                    ],
                    classes=[
                        ClassInfo(
                            name="SampleQueryService",
                            line_range=LineRange(1, 3),
                            base_classes=[],
                            methods=[],
                            decorators=[],
                            docstring=None,
                        )
                    ],
                ),
            },
            global_dependency_graph={},
            reverse_dependency_graph={},
            symbol_table={
                "SampleAggregationNode": "src/node.py",
                "SampleQueryService": "src/service.py",
                "SampleQueryService.build": "src/service.py",
            },
        )
        communities = {
            "SampleAggregationNode": "SampleQueryService",
            "SampleQueryService": "SampleQueryService",
            "SampleQueryService.build": "SampleQueryService",
        }

        result = get_cluster_for_symbol("SampleAggregationNode", communities, index)

        assert result["community_id"] == "SampleQueryService"
        members = {member["name"]: member for member in result["members"]}
        assert members["SampleAggregationNode"]["type"] == "class"
        assert members["SampleAggregationNode"]["file"] == "src/node.py"
        assert members["SampleQueryService"]["type"] == "class"
        assert members["SampleQueryService.build"]["type"] == "method"
