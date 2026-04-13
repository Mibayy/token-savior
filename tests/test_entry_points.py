"""Tests for entry point detection."""

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

    def test_api_substring_alone_does_not_create_route_reason(self):
        from pathlib import Path
        from token_savior.project_indexer import ProjectIndexer
        from token_savior.query_api import create_project_query_functions

        tmp = Path(self.tmp)
        (tmp / "api" / "contracts.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp / "api" / "contracts.py").write_text(
            "class Contracts:\n    def onInit(self):\n        pass\n"
        )
        idx = ProjectIndexer(self.tmp)
        project_index = idx.index()
        funcs = create_project_query_functions(project_index)

        result = funcs["get_entry_points"]()
        entry = next((r for r in result if r["name"].endswith(".onInit")), None)
        assert entry is not None
        assert "routes/api path" not in entry["reasons"]

    def test_java_application_and_benchmark_points_are_scored(self, tmp_path):
        idx = _make_indexer(
            tmp_path,
            {
                "src/main/java/com/acme/app/SampleGraphApplication.java": """\
package com.acme.app;

import org.openjdk.jmh.annotations.Benchmark;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public final class SampleGraphApplication {
    public static void main(String[] args) {
    }

    @Benchmark
    public void runBenchmark() {
    }
}
""",
            },
        )
        from token_savior.query_api import create_project_query_functions

        funcs = create_project_query_functions(idx.index())
        result = funcs["get_entry_points"]()

        main_entry = next(
            entry
            for entry in result
            if entry["name"] == "com.acme.app.SampleGraphApplication.main(String[])"
        )
        benchmark_entry = next(
            entry
            for entry in result
            if entry["name"] == "com.acme.app.SampleGraphApplication.runBenchmark()"
        )

        assert any("spring boot application" in reason for reason in main_entry["reasons"])
        assert any("entry class (SampleGraphApplication)" in reason for reason in main_entry["reasons"])
        assert benchmark_entry["score"] > 0.3
        assert any("benchmark lifecycle" in reason for reason in benchmark_entry["reasons"])
