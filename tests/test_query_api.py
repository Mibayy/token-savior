"""Tests for the structural query API (single-file and project-wide)."""

from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    ImportInfo,
    LineRange,
    ProjectIndex,
    SectionInfo,
    StructuralMetadata,
)
from token_savior.query_api import (
    ProjectQueryEngine,
    STRUCTURAL_QUERY_INSTRUCTIONS,
    create_file_query_functions,
    create_project_query_functions,
)


# ---------------------------------------------------------------------------
# Fixtures: build small in-memory metadata and project index
# ---------------------------------------------------------------------------

SAMPLE_SOURCE_A = """\
import os
from collections import OrderedDict

class Engine:
    \"\"\"The main engine.\"\"\"

    def __init__(self, config):
        self.config = config

    def run(self, task):
        result = helper(task)
        return result

def helper(x):
    \"\"\"A helper function.\"\"\"
    return x + 1
"""

SAMPLE_SOURCE_B = """\
from engine_mod import Engine

class Runner:
    \"\"\"Runs the engine.\"\"\"

    def __init__(self):
        self.engine = Engine({})

    def execute(self, task):
        return self.engine.run(task)

def main():
    r = Runner()
    r.execute("task1")
"""


def _make_metadata_a() -> StructuralMetadata:
    """Build StructuralMetadata for sample source A (engine module)."""
    lines = SAMPLE_SOURCE_A.split("\n")
    return StructuralMetadata(
        source_name="engine_mod.py",
        total_lines=len(lines),
        total_chars=len(SAMPLE_SOURCE_A),
        lines=lines,
        line_char_offsets=[],  # not needed for query tests
        functions=[
            FunctionInfo(
                name="__init__",
                qualified_name="Engine.__init__",
                line_range=LineRange(7, 8),
                parameters=["self", "config"],
                decorators=[],
                docstring=None,
                is_method=True,
                parent_class="Engine",
            ),
            FunctionInfo(
                name="run",
                qualified_name="Engine.run",
                line_range=LineRange(10, 12),
                parameters=["self", "task"],
                decorators=[],
                docstring=None,
                is_method=True,
                parent_class="Engine",
            ),
            FunctionInfo(
                name="helper",
                qualified_name="helper",
                line_range=LineRange(14, 16),
                parameters=["x"],
                decorators=[],
                docstring="A helper function.",
                is_method=False,
                parent_class=None,
            ),
        ],
        classes=[
            ClassInfo(
                name="Engine",
                line_range=LineRange(4, 12),
                base_classes=[],
                methods=[
                    FunctionInfo(
                        name="__init__",
                        qualified_name="Engine.__init__",
                        line_range=LineRange(7, 8),
                        parameters=["self", "config"],
                        decorators=[],
                        docstring=None,
                        is_method=True,
                        parent_class="Engine",
                    ),
                    FunctionInfo(
                        name="run",
                        qualified_name="Engine.run",
                        line_range=LineRange(10, 12),
                        parameters=["self", "task"],
                        decorators=[],
                        docstring=None,
                        is_method=True,
                        parent_class="Engine",
                    ),
                ],
                decorators=[],
                docstring="The main engine.",
            ),
        ],
        imports=[
            ImportInfo(
                module="os",
                names=[],
                alias=None,
                line_number=1,
                is_from_import=False,
            ),
            ImportInfo(
                module="collections",
                names=["OrderedDict"],
                alias=None,
                line_number=2,
                is_from_import=True,
            ),
        ],
        dependency_graph={
            "Engine.run": ["helper"],
            "helper": [],
        },
    )


def _make_metadata_b() -> StructuralMetadata:
    """Build StructuralMetadata for sample source B (runner module)."""
    lines = SAMPLE_SOURCE_B.split("\n")
    return StructuralMetadata(
        source_name="runner_mod.py",
        total_lines=len(lines),
        total_chars=len(SAMPLE_SOURCE_B),
        lines=lines,
        line_char_offsets=[],
        functions=[
            FunctionInfo(
                name="__init__",
                qualified_name="Runner.__init__",
                line_range=LineRange(6, 7),
                parameters=["self"],
                decorators=[],
                docstring=None,
                is_method=True,
                parent_class="Runner",
            ),
            FunctionInfo(
                name="execute",
                qualified_name="Runner.execute",
                line_range=LineRange(9, 10),
                parameters=["self", "task"],
                decorators=[],
                docstring=None,
                is_method=True,
                parent_class="Runner",
            ),
            FunctionInfo(
                name="main",
                qualified_name="main",
                line_range=LineRange(12, 14),
                parameters=[],
                decorators=[],
                docstring=None,
                is_method=False,
                parent_class=None,
            ),
        ],
        classes=[
            ClassInfo(
                name="Runner",
                line_range=LineRange(3, 10),
                base_classes=[],
                methods=[
                    FunctionInfo(
                        name="__init__",
                        qualified_name="Runner.__init__",
                        line_range=LineRange(6, 7),
                        parameters=["self"],
                        decorators=[],
                        docstring=None,
                        is_method=True,
                        parent_class="Runner",
                    ),
                    FunctionInfo(
                        name="execute",
                        qualified_name="Runner.execute",
                        line_range=LineRange(9, 10),
                        parameters=["self", "task"],
                        decorators=[],
                        docstring=None,
                        is_method=True,
                        parent_class="Runner",
                    ),
                ],
                decorators=[],
                docstring="Runs the engine.",
            ),
        ],
        imports=[
            ImportInfo(
                module="engine_mod",
                names=["Engine"],
                alias=None,
                line_number=1,
                is_from_import=True,
            ),
        ],
        dependency_graph={
            "Runner.execute": ["Engine.run"],
            "main": ["Runner"],
        },
    )


MARKDOWN_SOURCE = """\
# Introduction
This is the intro.

## Getting Started
Follow these steps.

## API Reference
Detailed API docs here.
"""


def _make_metadata_md() -> StructuralMetadata:
    """Build StructuralMetadata for a markdown document."""
    lines = MARKDOWN_SOURCE.split("\n")
    return StructuralMetadata(
        source_name="README.md",
        total_lines=len(lines),
        total_chars=len(MARKDOWN_SOURCE),
        lines=lines,
        line_char_offsets=[],
        sections=[
            SectionInfo(title="Introduction", level=1, line_range=LineRange(1, 2)),
            SectionInfo(title="Getting Started", level=2, line_range=LineRange(4, 5)),
            SectionInfo(title="API Reference", level=2, line_range=LineRange(7, 8)),
        ],
    )


def _make_project_index() -> ProjectIndex:
    """Build a small in-memory ProjectIndex with 2 code files and 1 markdown."""
    meta_a = _make_metadata_a()
    meta_b = _make_metadata_b()
    meta_md = _make_metadata_md()

    return ProjectIndex(
        root_path="/project",
        files={
            "src/engine_mod.py": meta_a,
            "src/runner_mod.py": meta_b,
            "docs/README.md": meta_md,
        },
        global_dependency_graph={
            "Engine.run": {"helper"},
            "helper": set(),
            "Runner.execute": {"Engine.run"},
            "main": {"Runner"},
        },
        reverse_dependency_graph={
            "helper": {"Engine.run"},
            "Engine.run": {"Runner.execute"},
            "Runner": {"main"},
        },
        import_graph={
            "src/engine_mod.py": set(),
            "src/runner_mod.py": {"src/engine_mod.py"},
        },
        reverse_import_graph={
            "src/engine_mod.py": {"src/runner_mod.py"},
            "src/runner_mod.py": set(),
        },
        symbol_table={
            "Engine": "src/engine_mod.py",
            "Engine.__init__": "src/engine_mod.py",
            "Engine.run": "src/engine_mod.py",
            "helper": "src/engine_mod.py",
            "Runner": "src/runner_mod.py",
            "Runner.__init__": "src/runner_mod.py",
            "Runner.execute": "src/runner_mod.py",
            "main": "src/runner_mod.py",
        },
        total_files=3,
        total_lines=meta_a.total_lines + meta_b.total_lines + meta_md.total_lines,
        total_functions=6,  # 3 in A + 3 in B
        total_classes=2,
    )


# ---------------------------------------------------------------------------
# Single-file query tests
# ---------------------------------------------------------------------------


class TestFileQueryFunctions:
    """Tests for create_file_query_functions."""

    def setup_method(self):
        self.meta = _make_metadata_a()
        self.funcs = create_file_query_functions(self.meta)

    def test_get_structure_summary(self):
        summary = self.funcs["get_structure_summary"]()
        assert "engine_mod.py" in summary
        assert "Engine" in summary
        assert "helper" in summary
        assert "lines" in summary.lower() or "line" in summary.lower()

    def test_get_lines(self):
        result = self.funcs["get_lines"](1, 2)
        assert "import os" in result
        assert "from collections" in result

    def test_get_lines_clamped(self):
        # End beyond total_lines should be clamped
        result = self.funcs["get_lines"](1, 99999)
        assert "import os" in result

    def test_get_lines_invalid_range(self):
        result = self.funcs["get_lines"](10, 5)
        assert "Error" in result

    def test_get_lines_start_below_one(self):
        result = self.funcs["get_lines"](0, 5)
        assert "Error" in result

    def test_get_line_count(self):
        assert self.funcs["get_line_count"]() == self.meta.total_lines

    def test_get_functions(self):
        funcs = self.funcs["get_functions"]()
        names = [f["name"] for f in funcs]
        assert "helper" in names
        assert "run" in names
        assert "__init__" in names
        # Check structure
        helper = [f for f in funcs if f["name"] == "helper"][0]
        assert helper["qualified_name"] == "helper"
        assert helper["params"] == ["x"]
        assert helper["is_method"] is False

    def test_get_classes(self):
        classes = self.funcs["get_classes"]()
        assert len(classes) == 1
        assert classes[0]["name"] == "Engine"
        assert "run" in classes[0]["methods"]
        assert "__init__" in classes[0]["methods"]
        assert "method_signatures" in classes[0]

    def test_get_imports(self):
        imports = self.funcs["get_imports"]()
        assert len(imports) == 2
        modules = [i["module"] for i in imports]
        assert "os" in modules
        assert "collections" in modules

    def test_get_function_source_by_name(self):
        src = self.funcs["get_function_source"]("helper")
        assert "def helper" in src
        assert "return x + 1" in src

    def test_get_function_source_by_qualified_name(self):
        src = self.funcs["get_function_source"]("Engine.run")
        assert "def run" in src

    def test_get_function_source_not_found(self):
        result = self.funcs["get_function_source"]("nonexistent")
        assert "Error" in result

    def test_get_class_source(self):
        src = self.funcs["get_class_source"]("Engine")
        assert "class Engine" in src
        assert "def run" in src

    def test_get_class_source_not_found(self):
        result = self.funcs["get_class_source"]("Nonexistent")
        assert "Error" in result

    def test_get_dependencies(self):
        deps = self.funcs["get_dependencies"]("Engine.run")
        assert any(d.get("name") == "helper" for d in deps)

    def test_get_dependencies_for_class_aggregates_method_dependencies(self):
        deps = self.funcs["get_dependencies"]("Engine")
        assert any(d.get("name") == "helper" for d in deps)

    def test_get_dependencies_empty(self):
        deps = self.funcs["get_dependencies"]("helper")
        assert deps == []

    def test_get_dependencies_not_found(self):
        deps = self.funcs["get_dependencies"]("nonexistent")
        assert any("error" in str(d).lower() for d in deps)

    def test_get_dependents(self):
        dependents = self.funcs["get_dependents"]("helper")
        assert any(d.get("name") == "Engine.run" for d in dependents)

    def test_search_lines(self):
        results = self.funcs["search_lines"]("def ")
        assert len(results) >= 2
        assert all("line_number" in r for r in results)
        assert all("content" in r for r in results)

    def test_search_lines_invalid_regex(self):
        results = self.funcs["search_lines"]("[invalid")
        assert "error" in results[0]


class TestFileQuerySections:
    """Tests for section-related single-file queries (text/markdown)."""

    def setup_method(self):
        self.meta = _make_metadata_md()
        self.funcs = create_file_query_functions(self.meta)

    def test_get_sections(self):
        sections = self.funcs["get_sections"]()
        assert len(sections) == 3
        titles = [s["title"] for s in sections]
        assert "Introduction" in titles
        assert "Getting Started" in titles
        assert "API Reference" in titles

    def test_get_section_content(self):
        content = self.funcs["get_section_content"]("Introduction")
        assert "intro" in content.lower()

    def test_get_section_content_not_found(self):
        result = self.funcs["get_section_content"]("Nonexistent")
        assert "Error" in result

    def test_structure_summary_shows_sections(self):
        summary = self.funcs["get_structure_summary"]()
        assert "Section" in summary
        assert "Introduction" in summary


# ---------------------------------------------------------------------------
# Project-wide query tests
# ---------------------------------------------------------------------------


class TestProjectQueryFunctions:
    """Tests for create_project_query_functions."""

    def setup_method(self):
        self.index = _make_project_index()
        self.funcs = create_project_query_functions(self.index)

    def test_get_project_summary(self):
        summary = self.funcs["get_project_summary"]()
        assert "/project" in summary
        assert "3" in summary  # total files
        assert "Classes:" in summary
        assert "functions:" in summary.lower()

    def test_list_files_all(self):
        files = self.funcs["list_files"]()
        assert len(files) == 3
        assert "src/engine_mod.py" in files

    def test_list_files_with_pattern(self):
        files = self.funcs["list_files"]("*.py")
        assert len(files) == 2
        assert "docs/README.md" not in files

    def test_list_files_with_subdir_pattern(self):
        files = self.funcs["list_files"]("src/*")
        assert len(files) == 2

    def test_get_structure_summary_project(self):
        # No file_path => project summary
        summary = self.funcs["get_structure_summary"]()
        assert "Project Structure Summary: /project" in summary
        assert "Top directories:" in summary
        assert "src" in summary
        assert summary != self.funcs["get_project_summary"]()

    def test_get_structure_summary_file(self):
        summary = self.funcs["get_structure_summary"]("src/engine_mod.py")
        assert "engine_mod.py" in summary
        assert "Engine" in summary

    def test_get_structure_summary_not_found(self):
        result = self.funcs["get_structure_summary"]("nonexistent.py")
        assert "Error" in result

    def test_get_lines(self):
        result = self.funcs["get_lines"]("src/engine_mod.py", 1, 2)
        assert "import os" in result

    def test_get_functions_all(self):
        funcs = self.funcs["get_functions"]()
        assert len(funcs) == 6  # 3 in A + 3 in B
        assert all("file" in f for f in funcs)

    def test_get_functions_per_file(self):
        funcs = self.funcs["get_functions"]("src/runner_mod.py")
        names = [f["name"] for f in funcs]
        assert "main" in names
        assert "execute" in names

    def test_get_classes_all(self):
        classes = self.funcs["get_classes"]()
        assert len(classes) == 2
        names = [c["name"] for c in classes]
        assert "Engine" in names
        assert "Runner" in names

    def test_get_classes_per_file(self):
        classes = self.funcs["get_classes"]("src/engine_mod.py")
        assert len(classes) == 1
        assert classes[0]["name"] == "Engine"
        assert classes[0]["method_signatures"] == ["Engine.__init__", "Engine.run"]

    def test_get_classes_dedupes_duplicate_method_names(self):
        meta = StructuralMetadata(
            source_name="factories.java",
            total_lines=10,
            total_chars=100,
            lines=[""] * 10,
            line_char_offsets=[],
            classes=[
                ClassInfo(
                    name="Factories",
                    line_range=LineRange(1, 10),
                    base_classes=[],
                    methods=[
                        FunctionInfo(
                            name="optionFlowImbalanceFactory",
                            qualified_name="Factories.optionFlowImbalanceFactory(String)",
                            line_range=LineRange(2, 3),
                            parameters=["name"],
                            decorators=[],
                            docstring=None,
                            is_method=True,
                            parent_class="Factories",
                        ),
                        FunctionInfo(
                            name="optionFlowImbalanceFactory",
                            qualified_name="Factories.optionFlowImbalanceFactory(String,int)",
                            line_range=LineRange(5, 6),
                            parameters=["name", "window"],
                            decorators=[],
                            docstring=None,
                            is_method=True,
                            parent_class="Factories",
                        ),
                    ],
                    decorators=[],
                    docstring=None,
                )
            ],
        )

        classes = create_file_query_functions(meta)["get_classes"]()

        assert classes[0]["methods"] == ["optionFlowImbalanceFactory"]
        assert classes[0]["method_signatures"] == [
            "Factories.optionFlowImbalanceFactory(String)",
            "Factories.optionFlowImbalanceFactory(String,int)",
        ]

    def test_get_imports_all(self):
        imports = self.funcs["get_imports"]()
        assert len(imports) == 3  # 2 in A + 1 in B

    def test_get_imports_per_file(self):
        imports = self.funcs["get_imports"]("src/runner_mod.py")
        assert len(imports) == 1
        assert imports[0]["module"] == "engine_mod"

    def test_get_function_source_by_name(self):
        src = self.funcs["get_function_source"]("helper")
        assert "def helper" in src

    def test_get_function_source_by_qualified_name(self):
        src = self.funcs["get_function_source"]("Engine.run")
        assert "def run" in src

    def test_get_function_source_with_file(self):
        src = self.funcs["get_function_source"]("main", "src/runner_mod.py")
        assert "def main" in src

    def test_get_function_source_not_found(self):
        result = self.funcs["get_function_source"]("nonexistent")
        assert "Error" in result

    def test_get_class_source(self):
        src = self.funcs["get_class_source"]("Runner")
        assert "class Runner" in src

    def test_get_class_source_not_found(self):
        result = self.funcs["get_class_source"]("Nonexistent")
        assert "Error" in result

    def test_find_symbol_function(self):
        result = self.funcs["find_symbol"]("helper")
        assert result["file"] == "src/engine_mod.py"
        assert result["type"] == "function"
        assert "line" in result

    def test_find_symbol_class(self):
        result = self.funcs["find_symbol"]("Engine")
        assert result["file"] == "src/engine_mod.py"
        assert result["type"] == "class"

    def test_find_symbol_method(self):
        result = self.funcs["find_symbol"]("Engine.run")
        assert result["file"] == "src/engine_mod.py"
        assert result["type"] == "method"

    def test_find_symbol_not_found(self):
        result = self.funcs["find_symbol"]("nonexistent")
        assert "error" in result

    def test_get_dependencies(self):
        deps = self.funcs["get_dependencies"]("Engine.run")
        assert any(d.get("name") == "helper" for d in deps)

    def test_get_dependents(self):
        deps = self.funcs["get_dependents"]("Engine.run")
        assert any(d.get("name") == "Runner.execute" for d in deps)

    def test_get_dependents_for_class_aggregates_method_dependents(self):
        deps = self.funcs["get_dependents"]("Engine")
        assert any(d.get("name") == "Runner.execute" for d in deps)

    def test_get_call_chain(self):
        result = self.funcs["get_call_chain"]("Runner.execute", "helper")
        assert "chain" in result
        names = [s.get("name", s) for s in result["chain"]]
        assert names == ["Runner.execute", "Engine.run", "helper"]

    def test_get_call_chain_direct(self):
        result = self.funcs["get_call_chain"]("Engine.run", "helper")
        assert "chain" in result
        names = [s.get("name", s) for s in result["chain"]]
        assert names == ["Engine.run", "helper"]

    def test_get_call_chain_from_class_aggregates_method_dependencies(self):
        result = self.funcs["get_call_chain"]("Engine", "helper")
        assert "chain" in result
        names = [s.get("name", s) for s in result["chain"]]
        assert names == ["Engine", "Engine.run", "helper"]

    def test_get_call_chain_same(self):
        result = self.funcs["get_call_chain"]("helper", "helper")
        assert "chain" in result
        names = [s.get("name", s) for s in result["chain"]]
        assert names == ["helper"]

    def test_get_call_chain_no_path(self):
        result = self.funcs["get_call_chain"]("helper", "main")
        assert "error" in result

    def test_get_call_chain_unknown_source(self):
        result = self.funcs["get_call_chain"]("nonexistent", "helper")
        assert "error" in result

    def test_get_call_chain_bridges_method_to_class_aliases(self):
        index = ProjectIndex(
            root_path="/project",
            files={
                "src/app.py": StructuralMetadata(
                    source_name="app.py",
                    total_lines=4,
                    total_chars=40,
                    lines=[""] * 4,
                    line_char_offsets=[],
                    functions=[
                        FunctionInfo(
                            name="start",
                            qualified_name="App.start",
                            line_range=LineRange(1, 2),
                            parameters=[],
                            decorators=[],
                            docstring=None,
                            is_method=True,
                            parent_class="App",
                        )
                    ],
                    classes=[
                        ClassInfo(
                            name="App",
                            line_range=LineRange(1, 2),
                            base_classes=[],
                            methods=[
                                FunctionInfo(
                                    name="start",
                                    qualified_name="App.start",
                                    line_range=LineRange(1, 2),
                                    parameters=[],
                                    decorators=[],
                                    docstring=None,
                                    is_method=True,
                                    parent_class="App",
                                )
                            ],
                            decorators=[],
                            docstring=None,
                        )
                    ],
                ),
                "src/factories.py": StructuralMetadata(
                    source_name="factories.py",
                    total_lines=6,
                    total_chars=60,
                    lines=[""] * 6,
                    line_char_offsets=[],
                    functions=[
                        FunctionInfo(
                            name="make",
                            qualified_name="Factories.make",
                            line_range=LineRange(2, 3),
                            parameters=[],
                            decorators=[],
                            docstring=None,
                            is_method=True,
                            parent_class="Factories",
                        )
                    ],
                    classes=[
                        ClassInfo(
                            name="Factories",
                            line_range=LineRange(1, 4),
                            base_classes=[],
                            methods=[
                                FunctionInfo(
                                    name="make",
                                    qualified_name="Factories.make",
                                    line_range=LineRange(2, 3),
                                    parameters=[],
                                    decorators=[],
                                    docstring=None,
                                    is_method=True,
                                    parent_class="Factories",
                                )
                            ],
                            decorators=[],
                            docstring=None,
                        )
                    ],
                ),
                "src/node.py": StructuralMetadata(
                    source_name="node.py",
                    total_lines=2,
                    total_chars=20,
                    lines=[""] * 2,
                    line_char_offsets=[],
                    classes=[
                        ClassInfo(
                            name="Node",
                            line_range=LineRange(1, 1),
                            base_classes=[],
                            methods=[],
                            decorators=[],
                            docstring=None,
                        )
                    ],
                ),
            },
            global_dependency_graph={
                "App.start": {"Factories.make"},
                "Factories": {"Node"},
                "Factories.make": set(),
                "Node": set(),
            },
            reverse_dependency_graph={
                "Factories.make": {"App.start"},
                "Node": {"Factories"},
            },
            symbol_table={
                "App": "src/app.py",
                "App.start": "src/app.py",
                "Factories": "src/factories.py",
                "Factories.make": "src/factories.py",
                "Node": "src/node.py",
            },
        )
        funcs = create_project_query_functions(index)

        result = funcs["get_call_chain"]("App.start", "Node")

        assert "chain" in result
        names = [step["name"] for step in result["chain"]]
        assert names == ["App.start", "Factories.make", "Factories", "Node"]

    def test_get_file_dependencies(self):
        deps = self.funcs["get_file_dependencies"]("src/runner_mod.py")
        assert "src/engine_mod.py" in deps

    def test_get_file_dependencies_none(self):
        deps = self.funcs["get_file_dependencies"]("src/engine_mod.py")
        assert deps == []

    def test_get_file_dependents(self):
        deps = self.funcs["get_file_dependents"]("src/engine_mod.py")
        assert "src/runner_mod.py" in deps

    def test_get_file_dependents_not_found(self):
        deps = self.funcs["get_file_dependents"]("nonexistent.py")
        assert "Error" in deps[0]

    def test_search_codebase(self):
        results = self.funcs["search_codebase"]("class ")
        assert len(results) >= 2
        files = {r["file"] for r in results}
        assert "src/engine_mod.py" in files
        assert "src/runner_mod.py" in files

    def test_search_codebase_no_results(self):
        results = self.funcs["search_codebase"]("zzz_nonexistent_zzz")
        assert results == []

    def test_search_codebase_invalid_regex(self):
        results = self.funcs["search_codebase"]("[invalid")
        assert "error" in results[0]

    def test_get_change_impact(self):
        impact = self.funcs["get_change_impact"]("helper")
        direct_names = [d.get("name", d) if isinstance(d, dict) else d for d in impact["direct"]]
        transitive_names = [
            d.get("name", d) if isinstance(d, dict) else d for d in impact["transitive"]
        ]
        assert "Engine.run" in direct_names
        # Transitive: Runner.execute depends on Engine.run
        assert "Runner.execute" in transitive_names
        # Each direct entry must have confidence == 1.0 and depth == 1
        for entry in impact["direct"]:
            assert isinstance(entry, dict), "direct entry should be a dict"
            assert entry["confidence"] == 1.0, f"expected confidence 1.0, got {entry['confidence']}"
            assert entry["depth"] == 1, f"expected depth 1, got {entry['depth']}"
        # Each transitive entry must have confidence < 1.0 and depth >= 2
        for entry in impact["transitive"]:
            assert isinstance(entry, dict), "transitive entry should be a dict"
            assert entry["confidence"] < 1.0, (
                f"expected confidence < 1.0, got {entry['confidence']}"
            )
            assert entry["depth"] >= 2, f"expected depth >= 2, got {entry['depth']}"

    def test_get_change_impact_for_class_aggregates_method_dependents(self):
        impact = self.funcs["get_change_impact"]("Engine")
        direct_names = [d.get("name", d) if isinstance(d, dict) else d for d in impact["direct"]]
        assert "Runner.execute" in direct_names

    def test_get_change_impact_no_dependents(self):
        # main has no reverse dependents in our graph
        impact = self.funcs["get_change_impact"]("main")
        assert "error" in impact

    def test_get_change_impact_not_found(self):
        impact = self.funcs["get_change_impact"]("nonexistent")
        assert "error" in impact


# ---------------------------------------------------------------------------
# Truncation / output size control tests
# ---------------------------------------------------------------------------


class TestOutputSizeControls:
    """Tests for max_results and max_lines truncation parameters."""

    def setup_method(self):
        self.index = _make_project_index()
        self.funcs = create_project_query_functions(self.index)

    def test_get_functions_max_results(self):
        all_funcs = self.funcs["get_functions"]()
        assert len(all_funcs) == 6
        limited = self.funcs["get_functions"](max_results=2)
        assert len(limited) == 2

    def test_get_functions_max_results_zero_unlimited(self):
        result = self.funcs["get_functions"](max_results=0)
        assert len(result) == 6

    def test_get_classes_max_results(self):
        limited = self.funcs["get_classes"](max_results=1)
        assert len(limited) == 1

    def test_get_imports_max_results(self):
        all_imports = self.funcs["get_imports"]()
        assert len(all_imports) == 3
        limited = self.funcs["get_imports"](max_results=2)
        assert len(limited) == 2

    def test_list_files_max_results(self):
        all_files = self.funcs["list_files"]()
        assert len(all_files) == 3
        limited = self.funcs["list_files"](max_results=1)
        assert len(limited) == 1

    def test_search_codebase_max_results(self):
        limited = self.funcs["search_codebase"]("def ", max_results=2)
        assert len(limited) <= 2

    def test_get_change_impact_max_direct(self):
        impact = self.funcs["get_change_impact"]("helper", max_direct=1)
        assert len(impact["direct"]) <= 1

    def test_get_change_impact_max_transitive(self):
        impact = self.funcs["get_change_impact"]("helper", max_transitive=1)
        assert len(impact["transitive"]) <= 1

    def test_get_function_source_max_lines(self):
        full = self.funcs["get_function_source"]("helper")
        assert "truncated" not in full
        truncated = self.funcs["get_function_source"]("helper", max_lines=1)
        assert "truncated" in truncated
        # Should have at most 1 line of actual source + the truncation message
        lines = truncated.split("\n")
        assert lines[-1].startswith("... (truncated to 1 lines)")

    def test_get_class_source_max_lines(self):
        full = self.funcs["get_class_source"]("Engine")
        full_lines = full.split("\n")
        assert len(full_lines) > 3
        truncated = self.funcs["get_class_source"]("Engine", max_lines=3)
        assert "truncated" in truncated
        lines = truncated.split("\n")
        # 3 source lines + 1 truncation message
        assert len(lines) == 4

    def test_get_function_source_max_lines_no_truncation_needed(self):
        # If max_lines >= actual lines, no truncation message
        src = self.funcs["get_function_source"]("helper", max_lines=100)
        assert "truncated" not in src


class TestComponentQueries:
    def test_get_components_strips_destructured_marker(self):
        index = ProjectIndex(
            root_path="/project",
            files={
                "ui/components/Card.tsx": StructuralMetadata(
                    source_name="Card.tsx",
                    total_lines=10,
                    total_chars=120,
                    lines=["export function Card({ title }) {", "  return <div />", "}"],
                    line_char_offsets=[],
                    functions=[
                        FunctionInfo(
                            name="Card",
                            qualified_name="Card",
                            line_range=LineRange(5, 9),
                            parameters=["destructured", "title"],
                            decorators=[],
                            docstring=None,
                            is_method=False,
                            parent_class=None,
                        )
                    ],
                )
            },
        )
        funcs = create_project_query_functions(index)

        components = funcs["get_components"]()

        assert components == [
            {
                "name": "Card",
                "file": "ui/components/Card.tsx",
                "line_range": "5-9",
                "params": ["title"],
                "type": "component",
            }
        ]

    def test_get_components_repairs_collapsed_arrow_component_range(self):
        index = ProjectIndex(
            root_path="/project",
            files={
                "ui/components/CTAButton.tsx": StructuralMetadata(
                    source_name="CTAButton.tsx",
                    total_lines=8,
                    total_chars=160,
                    lines=[
                        "export const CTAButton = ({ title }: Props) => (",
                        "  <button>",
                        "    <span>{title}</span>",
                        "  </button>",
                        ");",
                        "",
                        "",
                        "",
                    ],
                    line_char_offsets=[],
                    functions=[
                        FunctionInfo(
                            name="CTAButton",
                            qualified_name="CTAButton",
                            line_range=LineRange(1, 1),
                            parameters=["destructured", "title"],
                            decorators=[],
                            docstring=None,
                            is_method=False,
                            parent_class=None,
                        )
                    ],
                )
            },
        )
        funcs = create_project_query_functions(index)

        components = funcs["get_components"]()

        assert components[0]["line_range"] == "1-5"


class TestCallChainAliasResolution:
    def test_get_call_chain_resolves_signatureless_neighbors(self):
        index = ProjectIndex(
            root_path="/project",
            files={
                "src/app.java": StructuralMetadata(
                    source_name="app.java",
                    total_lines=1,
                    total_chars=0,
                    lines=[""],
                    line_char_offsets=[],
                    functions=[
                        FunctionInfo(
                            name="main",
                            qualified_name="com.acme.App.main(String[])",
                            line_range=LineRange(1, 1),
                            parameters=["String[]"],
                            decorators=[],
                            docstring=None,
                            is_method=True,
                            parent_class="App",
                        )
                    ],
                    classes=[
                        ClassInfo(
                            name="App",
                            qualified_name="com.acme.App",
                            line_range=LineRange(1, 1),
                            base_classes=[],
                            methods=[
                                FunctionInfo(
                                    name="main",
                                    qualified_name="com.acme.App.main(String[])",
                                    line_range=LineRange(1, 1),
                                    parameters=["String[]"],
                                    decorators=[],
                                    docstring=None,
                                    is_method=True,
                                    parent_class="App",
                                )
                            ],
                            decorators=[],
                            docstring=None,
                        )
                    ],
                ),
                "src/graphs.java": StructuralMetadata(
                    source_name="graphs.java",
                    total_lines=1,
                    total_chars=0,
                    lines=[""],
                    line_char_offsets=[],
                    functions=[
                        FunctionInfo(
                            name="register",
                            qualified_name="com.acme.GraphRegistry.register()",
                            line_range=LineRange(1, 1),
                            parameters=[],
                            decorators=[],
                            docstring=None,
                            is_method=True,
                            parent_class="GraphRegistry",
                        )
                    ],
                    classes=[
                        ClassInfo(
                            name="GraphRegistry",
                            qualified_name="com.acme.GraphRegistry",
                            line_range=LineRange(1, 1),
                            base_classes=[],
                            methods=[
                                FunctionInfo(
                                    name="register",
                                    qualified_name="com.acme.GraphRegistry.register()",
                                    line_range=LineRange(1, 1),
                                    parameters=[],
                                    decorators=[],
                                    docstring=None,
                                    is_method=True,
                                    parent_class="GraphRegistry",
                                )
                            ],
                            decorators=[],
                            docstring=None,
                        )
                    ],
                ),
                "src/factories.java": StructuralMetadata(
                    source_name="factories.java",
                    total_lines=1,
                    total_chars=0,
                    lines=[""],
                    line_char_offsets=[],
                    functions=[
                        FunctionInfo(
                            name="sampleAggregationFactory",
                            qualified_name="com.acme.Factories.sampleAggregationFactory()",
                            line_range=LineRange(1, 1),
                            parameters=[],
                            decorators=[],
                            docstring=None,
                            is_method=True,
                            parent_class="Factories",
                        )
                    ],
                    classes=[
                        ClassInfo(
                            name="Factories",
                            qualified_name="com.acme.Factories",
                            line_range=LineRange(1, 1),
                            base_classes=[],
                            methods=[
                                FunctionInfo(
                                    name="sampleAggregationFactory",
                                    qualified_name="com.acme.Factories.sampleAggregationFactory()",
                                    line_range=LineRange(1, 1),
                                    parameters=[],
                                    decorators=[],
                                    docstring=None,
                                    is_method=True,
                                    parent_class="Factories",
                                )
                            ],
                            decorators=[],
                            docstring=None,
                        )
                    ],
                ),
                "src/node.java": StructuralMetadata(
                    source_name="node.java",
                    total_lines=1,
                    total_chars=0,
                    lines=[""],
                    line_char_offsets=[],
                    classes=[
                        ClassInfo(
                            name="SampleAggregationNode",
                            qualified_name="com.acme.SampleAggregationNode",
                            line_range=LineRange(1, 1),
                            base_classes=[],
                            methods=[],
                            decorators=[],
                            docstring=None,
                        )
                    ],
                ),
            },
            global_dependency_graph={
                "com.acme.App.main(String[])": {"com.acme.GraphRegistry.register"},
                "com.acme.GraphRegistry.register()": {"com.acme.Factories.sampleAggregationFactory"},
                "com.acme.Factories.sampleAggregationFactory()": {"com.acme.SampleAggregationNode"},
            },
            reverse_dependency_graph={
                "com.acme.GraphRegistry.register": {"com.acme.App.main(String[])"},
                "com.acme.Factories.sampleAggregationFactory": {"com.acme.GraphRegistry.register()"},
                "com.acme.SampleAggregationNode": {"com.acme.Factories.sampleAggregationFactory()"},
            },
            symbol_table={
                "com.acme.App": "src/app.java",
                "com.acme.App.main(String[])": "src/app.java",
                "com.acme.GraphRegistry": "src/graphs.java",
                "com.acme.GraphRegistry.register()": "src/graphs.java",
                "com.acme.Factories": "src/factories.java",
                "com.acme.Factories.sampleAggregationFactory()": "src/factories.java",
                "com.acme.SampleAggregationNode": "src/node.java",
            },
        )
        funcs = create_project_query_functions(index)

        result = funcs["get_call_chain"]("com.acme.App", "com.acme.SampleAggregationNode")

        assert "chain" in result
        names = [step["name"] for step in result["chain"]]
        assert "com.acme.GraphRegistry.register()" in names
        assert "com.acme.Factories.sampleAggregationFactory()" in names

    def test_get_call_chain_matches_suffix_only_graph_nodes(self):
        index = ProjectIndex(
            root_path="/project",
            files={},
            global_dependency_graph={
                "SampleGraphApplication": {"RuntimeCoordinator.start"},
                "com.acme.runtime.RuntimeCoordinator.start()": {"GraphRegistry.register"},
                "com.acme.runtime.GraphRegistry.register()": {
                    "Factories.sampleAggregationFactory"
                },
                "com.acme.runtime.Factories.sampleAggregationFactory()": {
                    "com.acme.runtime.SampleAggregationNode"
                },
            },
            reverse_dependency_graph={},
            symbol_table={},
        )
        funcs = create_project_query_functions(index)

        result = funcs["get_call_chain"](
            "SampleGraphApplication",
            "SampleAggregationNode",
        )

        assert "chain" in result
        names = [step["name"] for step in result["chain"]]
        assert "com.acme.runtime.GraphRegistry.register()" in names
        assert "com.acme.runtime.Factories.sampleAggregationFactory()" in names

    def test_get_call_chain_does_not_jump_across_unrelated_sibling_methods(self):
        index = ProjectIndex(
            root_path="/project",
            files={
                "src/graphs.java": StructuralMetadata(
                    source_name="graphs.java",
                    total_lines=1,
                    total_chars=0,
                    lines=[""],
                    line_char_offsets=[],
                    functions=[
                        FunctionInfo(
                            name="register",
                            qualified_name="com.acme.GraphRegistry.register()",
                            line_range=LineRange(1, 1),
                            parameters=[],
                            decorators=[],
                            docstring=None,
                            is_method=True,
                            parent_class="GraphRegistry",
                        )
                    ],
                    classes=[
                        ClassInfo(
                            name="GraphRegistry",
                            qualified_name="com.acme.GraphRegistry",
                            line_range=LineRange(1, 1),
                            base_classes=[],
                            methods=[
                                FunctionInfo(
                                    name="register",
                                    qualified_name="com.acme.GraphRegistry.register()",
                                    line_range=LineRange(1, 1),
                                    parameters=[],
                                    decorators=[],
                                    docstring=None,
                                    is_method=True,
                                    parent_class="GraphRegistry",
                                )
                            ],
                            decorators=[],
                            docstring=None,
                        )
                    ],
                ),
                "src/factories.java": StructuralMetadata(
                    source_name="factories.java",
                    total_lines=1,
                    total_chars=0,
                    lines=[""],
                    line_char_offsets=[],
                    functions=[
                        FunctionInfo(
                            name="sampleAggregationFactory",
                            qualified_name="com.acme.Factories.sampleAggregationFactory()",
                            line_range=LineRange(1, 1),
                            parameters=[],
                            decorators=[],
                            docstring=None,
                            is_method=True,
                            parent_class="Factories",
                        ),
                        FunctionInfo(
                            name="alternateFactory",
                            qualified_name="com.acme.Factories.alternateFactory()",
                            line_range=LineRange(2, 2),
                            parameters=[],
                            decorators=[],
                            docstring=None,
                            is_method=True,
                            parent_class="Factories",
                        ),
                    ],
                    classes=[
                        ClassInfo(
                            name="Factories",
                            qualified_name="com.acme.Factories",
                            line_range=LineRange(1, 2),
                            base_classes=[],
                            methods=[
                                FunctionInfo(
                                    name="sampleAggregationFactory",
                                    qualified_name="com.acme.Factories.sampleAggregationFactory()",
                                    line_range=LineRange(1, 1),
                                    parameters=[],
                                    decorators=[],
                                    docstring=None,
                                    is_method=True,
                                    parent_class="Factories",
                                ),
                                FunctionInfo(
                                    name="alternateFactory",
                                    qualified_name="com.acme.Factories.alternateFactory()",
                                    line_range=LineRange(2, 2),
                                    parameters=[],
                                    decorators=[],
                                    docstring=None,
                                    is_method=True,
                                    parent_class="Factories",
                                ),
                            ],
                            decorators=[],
                            docstring=None,
                        )
                    ],
                ),
                "src/node.java": StructuralMetadata(
                    source_name="node.java",
                    total_lines=1,
                    total_chars=0,
                    lines=[""],
                    line_char_offsets=[],
                    classes=[
                        ClassInfo(
                            name="SampleAggregationNode",
                            qualified_name="com.acme.SampleAggregationNode",
                            line_range=LineRange(1, 1),
                            base_classes=[],
                            methods=[],
                            decorators=[],
                            docstring=None,
                        )
                    ],
                ),
            },
            global_dependency_graph={
                "com.acme.GraphRegistry.register()": {"com.acme.Factories.sampleAggregationFactory()"},
                "com.acme.Factories.sampleAggregationFactory()": {"com.acme.SampleAggregationNode"},
                "com.acme.Factories.alternateFactory()": {"com.acme.AlternateNode"},
            },
            reverse_dependency_graph={
                "com.acme.Factories.sampleAggregationFactory()": {"com.acme.GraphRegistry.register()"},
                "com.acme.SampleAggregationNode": {"com.acme.Factories.sampleAggregationFactory()"},
                "com.acme.AlternateNode": {"com.acme.Factories.alternateFactory()"},
            },
            symbol_table={
                "com.acme.GraphRegistry": "src/graphs.java",
                "com.acme.GraphRegistry.register()": "src/graphs.java",
                "com.acme.Factories": "src/factories.java",
                "com.acme.Factories.sampleAggregationFactory()": "src/factories.java",
                "com.acme.Factories.alternateFactory()": "src/factories.java",
                "com.acme.SampleAggregationNode": "src/node.java",
            },
        )
        funcs = create_project_query_functions(index)

        result = funcs["get_call_chain"](
            "com.acme.GraphRegistry",
            "com.acme.SampleAggregationNode",
        )

        assert "chain" in result
        names = [step["name"] for step in result["chain"]]
        assert names == [
            "com.acme.GraphRegistry",
            "com.acme.GraphRegistry.register()",
            "com.acme.Factories.sampleAggregationFactory()",
            "com.acme.SampleAggregationNode",
        ]

    def test_graph_candidate_names_do_not_treat_framework_sources_as_symbol_aliases(self):
        index = ProjectIndex(
            root_path="/project",
            files={},
            global_dependency_graph={
                "__framework__.spring.boot:application:com.acme.SampleGraphApplication": {
                    "com.acme.ReportService"
                },
                "com.acme.SampleGraphApplication.main(String[])": set(),
            },
            reverse_dependency_graph={},
            symbol_table={},
        )
        engine = ProjectQueryEngine(index)

        candidates = engine._resolve_graph_candidate_names("com.acme.SampleGraphApplication")

        assert "__framework__.spring.boot:application:com.acme.SampleGraphApplication" not in candidates


# ---------------------------------------------------------------------------
# System prompt instructions
# ---------------------------------------------------------------------------


class TestStructuralQueryInstructions:
    """Verify the system prompt constant is well-formed."""

    def test_instructions_is_nonempty_string(self):
        assert isinstance(STRUCTURAL_QUERY_INSTRUCTIONS, str)
        assert len(STRUCTURAL_QUERY_INSTRUCTIONS) > 100

    def test_instructions_mentions_key_functions(self):
        assert "get_project_summary" in STRUCTURAL_QUERY_INSTRUCTIONS
        assert "get_function_source" in STRUCTURAL_QUERY_INSTRUCTIONS
        assert "search_codebase" in STRUCTURAL_QUERY_INSTRUCTIONS
        assert "get_change_impact" in STRUCTURAL_QUERY_INSTRUCTIONS
        assert "find_symbol" in STRUCTURAL_QUERY_INSTRUCTIONS
