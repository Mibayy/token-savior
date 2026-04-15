"""Tests for the ProjectIndexer.

Covers:
- File discovery with include/exclude patterns
- Symbol table population
- Import graph construction (Python and TypeScript)
- Dependency graph construction
- Reverse graphs
- Max file size filtering
- reindex_file incremental updates
- Integration test on the actual token-savior source directory
"""

import os
import textwrap

import pytest

from token_savior.project_indexer import ProjectIndexer, _parse_gitignore


# ---------------------------------------------------------------------------
# Fixtures: temporary project directory with interconnected Python files
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_project(tmp_path):
    """Create a small project with 4 Python files that import from each other.

    Structure:
        myproject/
            src/
                myproject/
                    __init__.py
                    core.py           (defines CoreEngine class)
                    utils.py          (defines helper, format_output functions)
                    models.py         (defines DataModel class, uses utils)
            tests/
                test_core.py         (imports core and models)
            __pycache__/
                cached.pyc           (should be excluded)
            README.md
    """
    root = tmp_path / "myproject"
    root.mkdir()

    # src/myproject/__init__.py
    src = root / "src" / "myproject"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")

    # src/myproject/core.py
    (src / "core.py").write_text(
        textwrap.dedent("""\
        from myproject.utils import helper
        from myproject.models import DataModel


        class CoreEngine:
            \"\"\"The main engine.\"\"\"

            def run(self, data):
                model = DataModel(data)
                return helper(model)

            def stop(self):
                pass


        def create_engine():
            return CoreEngine()
        """)
    )

    # src/myproject/utils.py
    (src / "utils.py").write_text(
        textwrap.dedent("""\
        import os


        def helper(obj):
            \"\"\"A helper function.\"\"\"
            return format_output(str(obj))


        def format_output(text):
            return text.strip()
        """)
    )

    # src/myproject/models.py
    (src / "models.py").write_text(
        textwrap.dedent("""\
        from myproject.utils import format_output


        class DataModel:
            \"\"\"A data model.\"\"\"

            def __init__(self, data):
                self.data = data

            def serialize(self):
                return format_output(str(self.data))
        """)
    )

    # tests/test_core.py
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_core.py").write_text(
        textwrap.dedent("""\
        from myproject.core import CoreEngine, create_engine
        from myproject.models import DataModel


        def test_engine():
            engine = create_engine()
            result = engine.run("test")
            assert result is not None


        def test_model():
            model = DataModel("hello")
            assert model.serialize() == "hello"
        """)
    )

    # __pycache__/cached.pyc (should be excluded)
    pycache = root / "__pycache__"
    pycache.mkdir()
    (pycache / "cached.cpython-311.pyc").write_bytes(b"\x00" * 100)

    # README.md
    (root / "README.md").write_text("# My Project\n\nA sample project.\n")

    return root


@pytest.fixture
def ts_project(tmp_path):
    """Create a small TypeScript project with imports."""
    root = tmp_path / "tsproject"
    root.mkdir()

    src = root / "src"
    src.mkdir()

    (src / "utils.ts").write_text(
        textwrap.dedent("""\
        export function formatName(name: string): string {
            return name.trim();
        }

        export function validate(input: string): boolean {
            return input.length > 0;
        }
        """)
    )

    (src / "app.ts").write_text(
        textwrap.dedent("""\
        import { formatName, validate } from './utils';

        export class App {
            run(name: string): string {
                if (validate(name)) {
                    return formatName(name);
                }
                return '';
            }
        }
        """)
    )

    return root


# ---------------------------------------------------------------------------
# Test: file discovery
# ---------------------------------------------------------------------------


class TestFileDiscovery:
    def test_discovers_python_files(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        # Should find __init__.py, core.py, utils.py, models.py, test_core.py
        py_files = [f for f in idx.files if f.endswith(".py")]
        assert len(py_files) >= 5

    def test_discovers_markdown_files(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        md_files = [f for f in idx.files if f.endswith(".md")]
        assert len(md_files) == 1
        assert any("README.md" in f for f in md_files)

    def test_excludes_pycache(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        for f in idx.files:
            assert "__pycache__" not in f, f"__pycache__ file should be excluded: {f}"

    def test_exclude_patterns_work(self, sample_project):
        # Exclude tests directory as well
        indexer = ProjectIndexer(
            str(sample_project),
            exclude_patterns=["**/__pycache__/**", "**/tests/**"],
        )
        idx = indexer.index()

        for f in idx.files:
            assert "tests/" not in f, f"tests file should be excluded: {f}"

    def test_excludes_root_level_gradle_cache_dirs(self, tmp_path):
        (tmp_path / ".gradle/8.10.2/dependencies-accessors/foo").mkdir(parents=True)
        (tmp_path / ".gradle/8.10.2/dependencies-accessors/foo/LibrariesForLibs.java").write_text(
            "public final class LibrariesForLibs {}\n",
            encoding="utf-8",
        )
        (tmp_path / "src/main/java/com/acme/App.java").parent.mkdir(parents=True)
        (tmp_path / "src/main/java/com/acme/App.java").write_text(
            "package com.acme;\npublic final class App {}\n",
            encoding="utf-8",
        )

        idx = ProjectIndexer(str(tmp_path)).index()

        assert "src/main/java/com/acme/App.java" in idx.files
        assert not any(path.startswith(".gradle/") for path in idx.files)

    def test_max_file_size_filtering(self, sample_project):
        # Create a large file
        large_file = sample_project / "big_file.py"
        large_file.write_text("x = 1\n" * 100_000)  # ~600KB

        indexer = ProjectIndexer(str(sample_project), max_file_size_bytes=500_000)
        idx = indexer.index()

        assert "big_file.py" not in idx.files

    def test_include_patterns_filter(self, sample_project):
        # Only include Python files
        indexer = ProjectIndexer(
            str(sample_project),
            include_patterns=["**/*.py"],
        )
        idx = indexer.index()

        for f in idx.files:
            assert f.endswith(".py"), f"Non-Python file included: {f}"


# ---------------------------------------------------------------------------
# Test: symbol table
# ---------------------------------------------------------------------------


class TestSymbolTable:
    def test_functions_in_symbol_table(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        # Top-level functions should be in the symbol table
        assert "helper" in idx.symbol_table
        assert "format_output" in idx.symbol_table
        assert "create_engine" in idx.symbol_table

    def test_classes_in_symbol_table(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        assert "CoreEngine" in idx.symbol_table
        assert "DataModel" in idx.symbol_table

    def test_methods_in_symbol_table(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        # Methods should be registered with qualified names
        assert "CoreEngine.run" in idx.symbol_table
        assert "CoreEngine.stop" in idx.symbol_table
        assert "DataModel.serialize" in idx.symbol_table

    def test_symbol_table_maps_to_correct_file(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        assert idx.symbol_table["CoreEngine"].endswith("core.py")
        assert idx.symbol_table["helper"].endswith("utils.py")
        assert idx.symbol_table["DataModel"].endswith("models.py")


# ---------------------------------------------------------------------------
# Test: import graph
# ---------------------------------------------------------------------------


class TestImportGraph:
    def test_python_import_resolution(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        # core.py imports from utils.py and models.py
        core_path = None
        utils_path = None
        models_path = None
        for f in idx.files:
            if f.endswith("core.py") and "test" not in f:
                core_path = f
            elif f.endswith("utils.py"):
                utils_path = f
            elif f.endswith("models.py") and "test" not in f:
                models_path = f

        assert core_path is not None
        assert core_path in idx.import_graph
        assert utils_path in idx.import_graph[core_path]
        assert models_path in idx.import_graph[core_path]

    def test_reverse_import_graph(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        utils_path = None
        for f in idx.files:
            if f.endswith("utils.py"):
                utils_path = f
                break

        assert utils_path is not None
        # utils.py should be imported by core.py and models.py
        assert utils_path in idx.reverse_import_graph
        importers = idx.reverse_import_graph[utils_path]
        # At least core.py and models.py import from utils
        assert len(importers) >= 2

    def test_from_package_import_submodule_resolves_to_submodule(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "mod_a.py").write_text("from pkg import mod_b\n\ndef a():\n    return mod_b.b()\n")
        (pkg / "mod_b.py").write_text("from pkg import mod_a\n\ndef b():\n    return 1\n")

        idx = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"]).index()

        mod_a = "pkg/mod_a.py"
        mod_b = "pkg/mod_b.py"
        assert mod_a in idx.import_graph
        assert mod_b in idx.import_graph[mod_a], (
            f"expected mod_a to depend on mod_b, got {idx.import_graph[mod_a]}"
        )
        assert mod_a in idx.import_graph[mod_b]

    def test_from_package_import_package_itself_unchanged(self, tmp_path):
        pkg = tmp_path / "pkg"
        sub = pkg / "sub"
        pkg.mkdir()
        sub.mkdir()
        (pkg / "__init__.py").write_text("")
        (sub / "__init__.py").write_text("VALUE = 1\n")
        (pkg / "consumer.py").write_text("from pkg import sub\n\ndef f():\n    return sub.VALUE\n")

        idx = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"]).index()

        consumer = "pkg/consumer.py"
        sub_init = "pkg/sub/__init__.py"
        assert consumer in idx.import_graph
        assert sub_init in idx.import_graph[consumer]

    def test_typescript_relative_import(self, ts_project):
        indexer = ProjectIndexer(
            str(ts_project),
            include_patterns=["**/*.ts"],
        )
        idx = indexer.index()

        app_path = None
        utils_path = None
        for f in idx.files:
            if f.endswith("app.ts"):
                app_path = f
            elif f.endswith("utils.ts"):
                utils_path = f

        assert app_path is not None
        assert utils_path is not None
        assert app_path in idx.import_graph
        assert utils_path in idx.import_graph[app_path]


# ---------------------------------------------------------------------------
# Test: dependency graph
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    def test_intra_file_dependencies(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        # In utils.py, helper calls format_output
        assert "helper" in idx.global_dependency_graph
        assert "format_output" in idx.global_dependency_graph["helper"]

    def test_cross_file_dependencies(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        # In models.py, DataModel.serialize calls format_output (imported from utils)
        if "DataModel" in idx.global_dependency_graph:
            deps = idx.global_dependency_graph["DataModel"]
            assert "format_output" in deps

    def test_reverse_dependency_graph(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        # format_output should have dependents
        if "format_output" in idx.reverse_dependency_graph:
            dependents = idx.reverse_dependency_graph["format_output"]
            assert len(dependents) >= 1


# ---------------------------------------------------------------------------
# Test: reindex_file
# ---------------------------------------------------------------------------


class TestReindexFile:
    def test_reindex_updates_metadata(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        utils_path = None
        for f in idx.files:
            if f.endswith("utils.py"):
                utils_path = f
                break

        assert utils_path is not None
        old_line_count = idx.files[utils_path].total_lines

        # Modify the file: add a new function
        abs_utils = os.path.join(str(sample_project), utils_path)
        with open(abs_utils, "a") as f:
            f.write("\n\ndef new_function():\n    return 42\n")

        indexer.reindex_file(utils_path)

        assert idx.files[utils_path].total_lines > old_line_count
        assert "new_function" in idx.symbol_table

    def test_reindex_updates_symbol_table(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        assert "create_engine" in idx.symbol_table

        # Rewrite core.py without create_engine
        core_path = None
        for f in idx.files:
            if f.endswith("core.py"):
                core_path = f
                break

        abs_core = os.path.join(str(sample_project), core_path)
        with open(abs_core, "w") as f:
            f.write(
                textwrap.dedent("""\
                class CoreEngine:
                    def run(self):
                        pass
                """)
            )

        indexer.reindex_file(core_path)

        # create_engine should be removed from symbol table
        # (unless it also exists in another file)
        if "create_engine" in idx.symbol_table:
            assert idx.symbol_table["create_engine"] != core_path

    def test_reindex_raises_without_initial_index(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))

        with pytest.raises(RuntimeError, match="Cannot reindex"):
            indexer.reindex_file("some/file.py")


# ---------------------------------------------------------------------------
# Test: stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_populated(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        assert idx.total_files > 0
        assert idx.total_lines > 0
        assert idx.total_functions > 0
        assert idx.total_classes > 0
        assert idx.index_build_time_seconds > 0

    def test_file_count_matches(self, sample_project):
        indexer = ProjectIndexer(str(sample_project))
        idx = indexer.index()

        assert idx.total_files == len(idx.files)


# ---------------------------------------------------------------------------
# Integration test: index the actual token-savior source
# ---------------------------------------------------------------------------


@pytest.fixture
def go_project(tmp_path):
    """Create a small Go project with imports."""
    root = tmp_path / "goproject"
    root.mkdir()

    pkg = root / "pkg" / "utils"
    pkg.mkdir(parents=True)

    (pkg / "utils.go").write_text(
        textwrap.dedent("""\
        package utils

        // FormatName formats a name string.
        func FormatName(name string) string {
            return name
        }

        // Validate checks input validity.
        func Validate(input string) bool {
            return len(input) > 0
        }
        """)
    )

    cmd = root / "cmd"
    cmd.mkdir()

    (cmd / "main.go").write_text(
        textwrap.dedent("""\
        package main

        import "goproject/pkg/utils"

        // App is the main application struct.
        type App struct {
            Name string
        }

        func (a *App) Run() {
            utils.FormatName(a.Name)
        }

        func main() {
            app := &App{Name: "test"}
            app.Run()
        }
        """)
    )

    return root


@pytest.fixture
def rust_project(tmp_path):
    """Create a small Rust project with imports."""
    root = tmp_path / "rustproject"
    root.mkdir()

    src = root / "src"
    src.mkdir()

    (src / "lib.rs").write_text(
        textwrap.dedent("""\
        pub mod models;
        pub mod utils;

        use crate::models::Config;
        use crate::utils::format_name;

        /// Create a default config.
        pub fn default_config() -> Config {
            Config { name: format_name("default") }
        }
        """)
    )

    (src / "models.rs").write_text(
        textwrap.dedent("""\
        /// A configuration struct.
        #[derive(Debug, Clone)]
        pub struct Config {
            pub name: String,
        }

        impl Config {
            pub fn new(name: String) -> Self {
                Config { name }
            }
        }
        """)
    )

    (src / "utils.rs").write_text(
        textwrap.dedent("""\
        /// Format a name string.
        pub fn format_name(name: &str) -> String {
            name.trim().to_string()
        }
        """)
    )

    return root


class TestGoProject:
    def test_discovers_go_files(self, go_project):
        indexer = ProjectIndexer(
            str(go_project),
            include_patterns=["**/*.go"],
        )
        idx = indexer.index()
        go_files = [f for f in idx.files if f.endswith(".go")]
        assert len(go_files) == 2

    def test_go_symbol_table(self, go_project):
        indexer = ProjectIndexer(
            str(go_project),
            include_patterns=["**/*.go"],
        )
        idx = indexer.index()
        assert "FormatName" in idx.symbol_table
        assert "Validate" in idx.symbol_table
        assert "App" in idx.symbol_table
        assert "main" in idx.symbol_table

    def test_go_methods_detected(self, go_project):
        indexer = ProjectIndexer(
            str(go_project),
            include_patterns=["**/*.go"],
        )
        idx = indexer.index()
        assert "App.Run" in idx.symbol_table


class TestRustProject:
    def test_discovers_rust_files(self, rust_project):
        indexer = ProjectIndexer(
            str(rust_project),
            include_patterns=["**/*.rs"],
        )
        idx = indexer.index()
        rs_files = [f for f in idx.files if f.endswith(".rs")]
        assert len(rs_files) == 3

    def test_rust_symbol_table(self, rust_project):
        indexer = ProjectIndexer(
            str(rust_project),
            include_patterns=["**/*.rs"],
        )
        idx = indexer.index()
        assert "Config" in idx.symbol_table
        assert "format_name" in idx.symbol_table
        assert "default_config" in idx.symbol_table

    def test_rust_impl_methods(self, rust_project):
        indexer = ProjectIndexer(
            str(rust_project),
            include_patterns=["**/*.rs"],
        )
        idx = indexer.index()
        assert "Config.new" in idx.symbol_table

    def test_rust_import_resolution(self, rust_project):
        indexer = ProjectIndexer(
            str(rust_project),
            include_patterns=["**/*.rs"],
        )
        idx = indexer.index()

        lib_path = None
        models_path = None
        utils_path = None
        for f in idx.files:
            if f.endswith("lib.rs"):
                lib_path = f
            elif f.endswith("models.rs"):
                models_path = f
            elif f.endswith("utils.rs"):
                utils_path = f

        assert lib_path is not None
        if lib_path in idx.import_graph:
            imports = idx.import_graph[lib_path]
            assert models_path in imports or utils_path in imports


class TestParseGitignore:
    """Tests for the _parse_gitignore helper."""

    def test_returns_empty_when_no_gitignore(self, tmp_path):
        patterns = _parse_gitignore(str(tmp_path))
        assert patterns == []

    def test_skips_comments_and_blanks(self, tmp_path):
        (tmp_path / ".gitignore").write_text("# comment\n\n*.log\n")
        patterns = _parse_gitignore(str(tmp_path))
        assert any("*.log" in p for p in patterns)
        # comment and blank lines should not produce extra patterns
        assert not any("comment" in p for p in patterns)

    def test_directory_pattern_produces_glob(self, tmp_path):
        (tmp_path / ".gitignore").write_text(".worktrees/\n")
        patterns = _parse_gitignore(str(tmp_path))
        assert "**/.worktrees/**" in patterns

    def test_file_pattern_produces_anywhere_glob(self, tmp_path):
        (tmp_path / ".gitignore").write_text("*.log\n")
        patterns = _parse_gitignore(str(tmp_path))
        assert "**/*.log" in patterns

    def test_negation_patterns_skipped(self, tmp_path):
        (tmp_path / ".gitignore").write_text("!important.log\n")
        patterns = _parse_gitignore(str(tmp_path))
        assert patterns == []

    def test_leading_slash_stripped(self, tmp_path):
        (tmp_path / ".gitignore").write_text("/build/\n")
        patterns = _parse_gitignore(str(tmp_path))
        assert "**/.worktrees/**" not in patterns
        assert any("build" in p for p in patterns)


class TestGitignoreExclusion:
    """Tests for file discovery respecting .gitignore patterns."""

    def test_gitignored_directory_excluded_from_index(self, tmp_path):
        """Files inside a gitignored directory should not appear in the index."""
        # Create .gitignore that ignores .worktrees/
        (tmp_path / ".gitignore").write_text(".worktrees/\n")

        # Create source files: one real, one in ignored dir
        src = tmp_path / "main.py"
        src.write_text("def hello(): pass\n")

        wt_dir = tmp_path / ".worktrees" / "feat-branch" / "app"
        wt_dir.mkdir(parents=True)
        (wt_dir / "server.py").write_text("def serve(): pass\n")

        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        idx = indexer.index()

        indexed_paths = list(idx.files.keys())
        assert any("main.py" in p for p in indexed_paths)
        assert not any(".worktrees" in p for p in indexed_paths)

    def test_gitignore_file_pattern_excludes_matching_files(self, tmp_path):
        """Files matching a .gitignore glob should be excluded."""
        (tmp_path / ".gitignore").write_text("*.generated.py\n")

        (tmp_path / "real.py").write_text("def real(): pass\n")
        (tmp_path / "auto.generated.py").write_text("def generated(): pass\n")

        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        idx = indexer.index()

        indexed_paths = list(idx.files.keys())
        assert any("real.py" in p for p in indexed_paths)
        assert not any("auto.generated.py" in p for p in indexed_paths)

    def test_custom_exclude_patterns_override_gitignore(self, tmp_path):
        """Explicitly passed exclude_patterns skip .gitignore loading."""
        (tmp_path / ".gitignore").write_text("ignored_dir/\n")

        (tmp_path / "file.py").write_text("x = 1\n")
        ignored = tmp_path / "ignored_dir"
        ignored.mkdir()
        (ignored / "other.py").write_text("y = 2\n")

        # Pass custom exclude_patterns — .gitignore should NOT be applied
        indexer = ProjectIndexer(
            str(tmp_path),
            include_patterns=["**/*.py"],
            exclude_patterns=["**/node_modules/**"],  # custom, doesn't exclude ignored_dir
        )
        idx = indexer.index()

        indexed_paths = list(idx.files.keys())
        # ignored_dir/other.py should be indexed because gitignore was not applied
        assert any("other.py" in p for p in indexed_paths)

    def test_worktrees_excluded_by_default(self, tmp_path):
        """The .worktrees/ directory is excluded even without a .gitignore."""
        (tmp_path / "app.py").write_text("def app(): pass\n")

        wt = tmp_path / ".worktrees" / "my-branch"
        wt.mkdir(parents=True)
        (wt / "app.py").write_text("def app(): pass\n")

        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        idx = indexer.index()

        indexed_paths = list(idx.files.keys())
        assert not any(".worktrees" in p for p in indexed_paths)


class TestExcludePatternsEnvVar:
    """Tests for TOKEN_SAVIOR_EXCLUDE_PATTERNS environment variable."""

    def test_env_var_patterns_applied(self, tmp_path, monkeypatch):
        """Patterns from TOKEN_SAVIOR_EXCLUDE_PATTERNS are excluded."""
        monkeypatch.setenv("TOKEN_SAVIOR_EXCLUDE_PATTERNS", "**/secret/**")

        (tmp_path / "public.py").write_text("def pub(): pass\n")
        sec = tmp_path / "secret"
        sec.mkdir()
        (sec / "private.py").write_text("def priv(): pass\n")

        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        idx = indexer.index()

        indexed_paths = list(idx.files.keys())
        assert any("public.py" in p for p in indexed_paths)
        assert not any("private.py" in p for p in indexed_paths)

    def test_env_var_colon_separated(self, tmp_path, monkeypatch):
        """Multiple patterns can be separated by colons."""
        monkeypatch.setenv("TOKEN_SAVIOR_EXCLUDE_PATTERNS", "**/aaa/**:**/bbb/**")

        (tmp_path / "main.py").write_text("x = 1\n")
        for d in ("aaa", "bbb"):
            (tmp_path / d).mkdir()
            (tmp_path / d / "mod.py").write_text("y = 2\n")

        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        idx = indexer.index()

        indexed_paths = list(idx.files.keys())
        assert any("main.py" in p for p in indexed_paths)
        assert not any("aaa" in p for p in indexed_paths)
        assert not any("bbb" in p for p in indexed_paths)

    def test_empty_env_var_ignored(self, tmp_path, monkeypatch):
        """An empty TOKEN_SAVIOR_EXCLUDE_PATTERNS should not break indexing."""
        monkeypatch.setenv("TOKEN_SAVIOR_EXCLUDE_PATTERNS", "")

        (tmp_path / "ok.py").write_text("z = 3\n")
        indexer = ProjectIndexer(str(tmp_path), include_patterns=["**/*.py"])
        idx = indexer.index()

        assert idx.total_files == 1


class TestIntegration:
    def test_index_token_savior_source(self):
        """Index the actual token-savior src directory as an integration test."""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src_dir = os.path.join(project_root, "src")

        if not os.path.isdir(src_dir):
            pytest.skip("token-savior src directory not found")

        indexer = ProjectIndexer(
            src_dir,
            include_patterns=["**/*.py"],
        )
        idx = indexer.index()

        # Should find at least several Python files
        assert idx.total_files >= 5
        assert idx.total_functions >= 10
        assert idx.total_classes >= 3

        # Should have known symbols
        assert "annotate" in idx.symbol_table or "annotate_python" in idx.symbol_table

        # Import graph should be non-empty
        assert len(idx.import_graph) > 0

        # Build time should be reasonable (< 5 seconds)
        assert idx.index_build_time_seconds < 5.0


class TestBuildFileCoverage:
    def test_discovers_gradle_and_maven_files(self, tmp_path):
        root = tmp_path / "build-project"
        root.mkdir()

        (root / "build.gradle.kts").write_text(
            "plugins {\n    id(\"java\")\n}\n",
            encoding="utf-8",
        )
        (root / "settings.gradle").write_text(
            "rootProject.name = 'demo'\ninclude(':app')\n",
            encoding="utf-8",
        )
        (root / "gradle.properties").write_text(
            "org.gradle.jvmargs=-Xmx1g\n",
            encoding="utf-8",
        )
        (root / "pom.xml").write_text(
            "<project><dependencies><dependency/></dependencies></project>",
            encoding="utf-8",
        )

        idx = ProjectIndexer(str(root)).index()

        assert "build.gradle.kts" in idx.files
        assert "settings.gradle" in idx.files
        assert "gradle.properties" in idx.files
        assert "pom.xml" in idx.files
        assert [section.title for section in idx.files["build.gradle.kts"].sections] == [
            "plugins",
            "id java",
        ]
        assert [section.title for section in idx.files["pom.xml"].sections] == [
            "project",
            "dependencies",
            "dependency",
        ]
