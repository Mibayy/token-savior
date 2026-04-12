"""Integration tests for Java indexing and query resolution."""

from __future__ import annotations

import textwrap

from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import create_project_query_functions


def _write_file(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def _build_java_project(root) -> None:
    _write_file(
        root / "src/main/java/com/acme/shared/MathUtil.java",
        """\
        package com.acme.shared;

        public final class MathUtil {
            public static int scale(int value) {
                return value * 2;
            }
        }
        """,
    )
    _write_file(
        root / "src/main/java/com/acme/local/Worker.java",
        """\
        package com.acme.local;

        public final class Worker {
            public int execute(int value) {
                class LocalFormatter {
                    int format(int input) {
                        return input + 1;
                    }
                }

                return new LocalFormatter().format(value);
            }
        }
        """,
    )
    _write_file(
        root / "src/main/java/com/acme/pricing/QuotePublisher.java",
        """\
        package com.acme.pricing;

        public interface QuotePublisher {
            void publish();
        }
        """,
    )
    _write_file(
        root / "src/main/java/com/acme/pricing/PriceEngine.java",
        """\
        package com.acme.pricing;

        import com.acme.shared.MathUtil;

        public final class PriceEngine implements QuotePublisher {
            public int apply(int input) {
                helper();
                return MathUtil.scale(input);
            }

            private void helper() {
            }

            @Override
            public void publish() {
                helper();
            }
        }
        """,
    )
    _write_file(
        root / "src/test/java/com/acme/pricing/PriceEngineTest.java",
        """\
        package com.acme.pricing;

        public final class PriceEngineTest {
            public void testApply() {
                PriceEngine engine = new PriceEngine();
                engine.apply(42);
            }
        }
        """,
    )


class TestJavaProjectIndexer:
    def test_indexes_java_symbols_and_imports(self, tmp_path):
        root = tmp_path / "java-project"
        root.mkdir()
        _build_java_project(root)

        idx = ProjectIndexer(str(root)).index()

        assert "src/main/java/com/acme/pricing/PriceEngine.java" in idx.files
        assert "PriceEngine" in idx.symbol_table
        assert "com.acme.pricing.PriceEngine" in idx.symbol_table
        assert "com.acme.pricing.PriceEngine.apply(int)" in idx.symbol_table
        assert "com.acme.pricing.PriceEngine.apply" in idx.symbol_table
        assert "PriceEngine.apply" in idx.symbol_table

        imports = idx.import_graph["src/main/java/com/acme/pricing/PriceEngine.java"]
        assert "src/main/java/com/acme/shared/MathUtil.java" in imports

    def test_builds_java_dependency_graph_and_queries(self, tmp_path):
        root = tmp_path / "java-project"
        root.mkdir()
        _build_java_project(root)

        idx = ProjectIndexer(str(root)).index()
        funcs = create_project_query_functions(idx)

        deps = idx.global_dependency_graph["com.acme.pricing.PriceEngine.apply(int)"]
        assert "com.acme.pricing.PriceEngine.helper()" in deps
        assert "com.acme.shared.MathUtil" in deps

        class_deps = idx.global_dependency_graph["com.acme.pricing.PriceEngine"]
        assert "com.acme.pricing.QuotePublisher" in class_deps

        math_dependents = idx.reverse_dependency_graph["com.acme.shared.MathUtil"]
        assert "com.acme.pricing.PriceEngine.apply(int)" in math_dependents

        result = funcs["find_symbol"]("com.acme.pricing.PriceEngine")
        assert result["file"] == "src/main/java/com/acme/pricing/PriceEngine.java"
        assert result["type"] == "class"

        bare_class_result = funcs["find_symbol"]("PriceEngine")
        assert bare_class_result["file"] == "src/main/java/com/acme/pricing/PriceEngine.java"
        assert bare_class_result["type"] == "class"
        assert bare_class_result["name"] == "com.acme.pricing.PriceEngine"

        method_result = funcs["find_symbol"]("PriceEngine.apply")
        assert method_result["file"] == "src/main/java/com/acme/pricing/PriceEngine.java"
        assert method_result["type"] == "method"
        assert method_result["name"] == "com.acme.pricing.PriceEngine.apply(int)"

        class_source = funcs["get_class_source"]("com.acme.pricing.PriceEngine")
        assert "class PriceEngine" in class_source

        method_source = funcs["get_function_source"]("com.acme.pricing.PriceEngine.apply")
        assert "MathUtil.scale" in method_source

        dependency_result = funcs["get_dependencies"]("PriceEngine.apply")
        assert any(
            dep.get("name") == "com.acme.pricing.PriceEngine.helper()"
            for dep in dependency_result
        )

        class_dependency_result = funcs["get_dependencies"]("PriceEngine")
        assert any(
            dep.get("name") == "com.acme.pricing.QuotePublisher"
            for dep in class_dependency_result
        )
        assert any(
            dep.get("name") == "com.acme.pricing.PriceEngine.helper()"
            for dep in class_dependency_result
        )
        assert any(
            dep.get("name") == "com.acme.shared.MathUtil"
            for dep in class_dependency_result
        )

    def test_indexes_scoped_local_java_classes_without_simple_aliases(self, tmp_path):
        root = tmp_path / "java-project"
        root.mkdir()
        _build_java_project(root)

        idx = ProjectIndexer(str(root)).index()
        funcs = create_project_query_functions(idx)

        local_class = "com.acme.local.Worker.execute(int)::<local>.LocalFormatter"
        local_method = f"{local_class}.format(int)"

        assert local_class in idx.symbol_table
        assert local_method in idx.symbol_table
        assert "LocalFormatter" not in idx.symbol_table
        assert "LocalFormatter.format" not in idx.symbol_table

        class_result = funcs["find_symbol"](local_class)
        assert class_result["file"] == "src/main/java/com/acme/local/Worker.java"
        assert class_result["type"] == "class"

        class_source = funcs["get_class_source"](local_class)
        assert "class LocalFormatter" in class_source

        deps = idx.global_dependency_graph["com.acme.local.Worker.execute(int)"]
        assert local_class in deps

    def test_resolves_cross_file_java_method_references_into_global_graph(self, tmp_path):
        root = tmp_path / "java-project"
        root.mkdir()
        _write_file(
            root / "src/main/java/com/acme/runtime/Factories.java",
            """\
            package com.acme.runtime;

            public final class Factories {
                public static Object createFeed() {
                    return new Object();
                }
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/runtime/TradingGraphs.java",
            """\
            package com.acme.runtime;

            public final class TradingGraphs {
                public void register() {
                    GraphDefinition.register(Factories::createFeed);
                }
            }
            """,
        )

        idx = ProjectIndexer(str(root)).index()
        funcs = create_project_query_functions(idx)

        register_symbol = "com.acme.runtime.TradingGraphs.register()"
        factory_symbol = "com.acme.runtime.Factories.createFeed()"

        deps = idx.global_dependency_graph[register_symbol]
        assert factory_symbol in deps

        dependents = idx.reverse_dependency_graph[factory_symbol]
        assert register_symbol in dependents

        dependency_result = funcs["get_dependencies"]("TradingGraphs.register")
        assert any(dep.get("name") == factory_symbol for dep in dependency_result)

        dependent_result = funcs["get_dependents"]("Factories.createFeed")
        assert any(dep.get("name") == register_symbol for dep in dependent_result)
