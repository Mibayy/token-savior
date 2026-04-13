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

    def test_propagates_java_interface_dispatch_to_implementations(self, tmp_path):
        root = tmp_path / "java-project"
        root.mkdir()
        _write_file(
            root / "src/main/java/com/acme/feed/TradeHandler.java",
            """\
            package com.acme.feed;

            public interface TradeHandler {
                void onTrade(String trade);
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/feed/LoggingTradeHandler.java",
            """\
            package com.acme.feed;

            public final class LoggingTradeHandler implements TradeHandler {
                @Override
                public void onTrade(String trade) {
                }
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/feed/Dispatcher.java",
            """\
            package com.acme.feed;

            public final class Dispatcher {
                private final TradeHandler handler;

                public Dispatcher(TradeHandler handler) {
                    this.handler = handler;
                }

                public void dispatch(String trade) {
                    handler.onTrade(trade);
                }
            }
            """,
        )

        idx = ProjectIndexer(str(root)).index()
        funcs = create_project_query_functions(idx)

        dispatch_symbol = "com.acme.feed.Dispatcher.dispatch(String)"
        iface_symbol = "com.acme.feed.TradeHandler.onTrade(String)"
        impl_symbol = "com.acme.feed.LoggingTradeHandler.onTrade(String)"

        deps = idx.global_dependency_graph[dispatch_symbol]
        assert iface_symbol in deps
        assert impl_symbol in deps

        dependents = idx.reverse_dependency_graph[impl_symbol]
        assert dispatch_symbol in dependents

        dependent_result = funcs["get_dependents"]("LoggingTradeHandler.onTrade")
        assert any(dep.get("name") == dispatch_symbol for dep in dependent_result)

    def test_resolves_java_lambda_constructor_edges_into_global_graph(self, tmp_path):
        root = tmp_path / "java-project"
        root.mkdir()
        _write_file(
            root / "src/main/java/com/acme/runtime/CryptoAssetAggregationNode.java",
            """\
            package com.acme.runtime;

            public final class CryptoAssetAggregationNode {
                public CryptoAssetAggregationNode() {
                }
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/runtime/Factories.java",
            """\
            package com.acme.runtime;

            import java.util.function.Supplier;

            public final class Factories {
                public Supplier<CryptoAssetAggregationNode> nodeFactory() {
                    return () -> new CryptoAssetAggregationNode();
                }
            }
            """,
        )

        idx = ProjectIndexer(str(root)).index()

        factory_symbol = "com.acme.runtime.Factories.nodeFactory()"
        node_symbol = "com.acme.runtime.CryptoAssetAggregationNode"
        deps = idx.global_dependency_graph[factory_symbol]
        assert node_symbol in deps

    def test_get_call_chain_reaches_node_through_factory_edges(self, tmp_path):
        root = tmp_path / "java-project"
        root.mkdir()
        _write_file(
            root / "src/main/java/com/acme/app/TradeResearchApiApplication.java",
            """\
            package com.acme.app;

            public final class TradeResearchApiApplication {
                public static void main(String[] args) {
                    new CryptoCycleGraphs().register();
                }
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/app/CryptoCycleGraphs.java",
            """\
            package com.acme.app;

            public final class CryptoCycleGraphs {
                public void register() {
                    GraphDefinition.register(Factories::cryptoAssetAggregationFactory);
                }
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/app/Factories.java",
            """\
            package com.acme.app;

            import java.util.function.Supplier;

            public final class Factories {
                public static Supplier<CryptoAssetAggregationNode> cryptoAssetAggregationFactory() {
                    return () -> new CryptoAssetAggregationNode();
                }
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/app/CryptoAssetAggregationNode.java",
            """\
            package com.acme.app;

            public final class CryptoAssetAggregationNode {
                public CryptoAssetAggregationNode() {
                }
            }
            """,
        )

        idx = ProjectIndexer(str(root)).index()
        funcs = create_project_query_functions(idx)

        result = funcs["get_call_chain"](
            "TradeResearchApiApplication",
            "CryptoAssetAggregationNode",
        )

        assert "chain" in result
        names = [step["name"] for step in result["chain"]]
        assert names[0] == "com.acme.app.TradeResearchApiApplication"
        assert "com.acme.app.CryptoCycleGraphs.register()" in names
        assert "com.acme.app.Factories.cryptoAssetAggregationFactory()" in names
        assert names[-1] == "com.acme.app.CryptoAssetAggregationNode"

    def test_adds_spring_framework_entry_edges(self, tmp_path):
        root = tmp_path / "spring-project"
        root.mkdir()
        _write_file(
            root / "src/main/java/com/acme/web/UserController.java",
            """\
            package com.acme.web;

            import org.springframework.web.bind.annotation.GetMapping;
            import org.springframework.web.bind.annotation.RestController;

            @RestController
            public final class UserController {
                @GetMapping("/users")
                public String getUser() {
                    return "ok";
                }
            }
            """,
        )

        idx = ProjectIndexer(str(root)).index()

        controller_symbol = "com.acme.web.UserController"
        method_symbol = "com.acme.web.UserController.getUser()"
        framework_dependents = idx.reverse_dependency_graph[method_symbol]
        assert any(dep.startswith("__framework__.spring.class:controller:") for dep in framework_dependents)
        assert any(dep.startswith("__framework__.spring.method:route:") for dep in framework_dependents)
        assert any(
            dep.startswith("__framework__.spring.class:controller:")
            for dep in idx.reverse_dependency_graph[controller_symbol]
        )

    def test_adds_runtime_dispatch_edges_for_indirect_runnable_run(self, tmp_path):
        root = tmp_path / "java-project"
        root.mkdir()
        _write_file(
            root / "src/main/java/com/acme/runtime/BaseShutdownThread.java",
            """\
            package com.acme.runtime;

            public abstract class BaseShutdownThread implements Runnable {
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/runtime/RuntimeShutdownThread.java",
            """\
            package com.acme.runtime;

            public final class RuntimeShutdownThread extends BaseShutdownThread {
                @Override
                public void run() {
                }
            }
            """,
        )

        idx = ProjectIndexer(str(root)).index()

        run_symbol = "com.acme.runtime.RuntimeShutdownThread.run()"
        dependents = idx.reverse_dependency_graph[run_symbol]
        assert "__runtime__.java.dispatch:Runnable.run" in dependents

    def test_get_call_chain_reaches_node_through_spring_boot_wiring(self, tmp_path):
        root = tmp_path / "spring-project"
        root.mkdir()
        _write_file(
            root / "src/main/java/com/acme/app/TradeResearchApiApplication.java",
            """\
            package com.acme.app;

            import org.springframework.boot.SpringApplication;
            import org.springframework.boot.autoconfigure.SpringBootApplication;

            @SpringBootApplication
            public final class TradeResearchApiApplication {
                public static void main(String[] args) {
                    SpringApplication.run(TradeResearchApiApplication.class, args);
                }
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/app/LiveIngressCoordinator.java",
            """\
            package com.acme.app;

            import org.springframework.stereotype.Service;

            @Service
            public final class LiveIngressCoordinator {
                private final CryptoCycleGraphs graphs;

                public LiveIngressCoordinator(CryptoCycleGraphs graphs) {
                    this.graphs = graphs;
                }

                public void start() {
                    graphs.register();
                }
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/app/CryptoCycleGraphs.java",
            """\
            package com.acme.app;

            import org.springframework.stereotype.Component;

            @Component
            public final class CryptoCycleGraphs {
                public void register() {
                    GraphDefinition.register(Factories::cryptoAssetAggregationFactory);
                }
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/app/Factories.java",
            """\
            package com.acme.app;

            import java.util.function.Supplier;

            public final class Factories {
                public static Supplier<CryptoAssetAggregationNode> cryptoAssetAggregationFactory() {
                    return () -> new CryptoAssetAggregationNode();
                }
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/app/CryptoAssetAggregationNode.java",
            """\
            package com.acme.app;

            public final class CryptoAssetAggregationNode {
            }
            """,
        )

        idx = ProjectIndexer(str(root)).index()
        funcs = create_project_query_functions(idx)

        result = funcs["get_call_chain"](
            "TradeResearchApiApplication",
            "CryptoAssetAggregationNode",
        )

        assert "chain" in result
        names = [step["name"] for step in result["chain"]]
        assert "com.acme.app.TradeResearchApiApplication.main(String[])" in names
        assert "com.acme.app.LiveIngressCoordinator.start()" in names
        assert "com.acme.app.CryptoCycleGraphs.register()" in names
        assert "com.acme.app.Factories.cryptoAssetAggregationFactory()" in names
        assert names[-1] == "com.acme.app.CryptoAssetAggregationNode"

    def test_reports_duplicate_java_classes(self, tmp_path):
        root = tmp_path / "java-project"
        root.mkdir()
        _write_file(
            root / "src/main/java/com/acme/view/HotViewKeys.java",
            """\
            package com.acme.view;

            public final class HotViewKeys {
            }
            """,
        )
        _write_file(
            root / "src/generated/java/com/acme/view/HotViewKeys.java",
            """\
            package com.acme.view;

            public final class HotViewKeys {
            }
            """,
        )

        idx = ProjectIndexer(str(root)).index()
        funcs = create_project_query_functions(idx)

        duplicates = funcs["get_duplicate_classes"]("HotViewKeys")
        assert duplicates == [
            {
                "name": "HotViewKeys",
                "qualified_name": "com.acme.view.HotViewKeys",
                "count": 2,
                "files": [
                    "src/generated/java/com/acme/view/HotViewKeys.java",
                    "src/main/java/com/acme/view/HotViewKeys.java",
                ],
            }
        ]

    def test_reports_simple_name_duplicate_java_classes(self, tmp_path):
        root = tmp_path / "java-project"
        root.mkdir()
        _write_file(
            root / "src/main/java/com/acme/view/HotViewKeys.java",
            """\
            package com.acme.view;

            public final class HotViewKeys {
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/legacy/HotViewKeys.java",
            """\
            package com.acme.legacy;

            public final class HotViewKeys {
            }
            """,
        )

        idx = ProjectIndexer(str(root)).index()
        funcs = create_project_query_functions(idx)

        duplicates = funcs["get_duplicate_classes"]("HotViewKeys", simple_name_mode=True)
        assert duplicates == [
            {
                "name": "HotViewKeys",
                "qualified_names": [
                    "com.acme.legacy.HotViewKeys",
                    "com.acme.view.HotViewKeys",
                ],
                "count": 2,
                "files": [
                    "src/main/java/com/acme/legacy/HotViewKeys.java",
                    "src/main/java/com/acme/view/HotViewKeys.java",
                ],
            }
        ]

    def test_simple_name_duplicates_filter_constant_like_names(self, tmp_path):
        root = tmp_path / "java-project"
        root.mkdir()
        _write_file(
            root / "src/main/java/com/acme/view/DM.java",
            """\
            package com.acme.view;

            public final class DM {
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/legacy/DM.java",
            """\
            package com.acme.legacy;

            public final class DM {
            }
            """,
        )

        idx = ProjectIndexer(str(root)).index()
        funcs = create_project_query_functions(idx)

        duplicates = funcs["get_duplicate_classes"]("DM", simple_name_mode=True)
        assert duplicates == []

    def test_adds_direct_runtime_edges_from_thread_and_executor_launch_sites(self, tmp_path):
        root = tmp_path / "java-project"
        root.mkdir()
        _write_file(
            root / "src/main/java/com/acme/runtime/WorkerThread.java",
            """\
            package com.acme.runtime;

            public final class WorkerThread extends Thread {
                @Override
                public void run() {
                }
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/runtime/WorkerTask.java",
            """\
            package com.acme.runtime;

            public final class WorkerTask implements Runnable {
                @Override
                public void run() {
                }
            }
            """,
        )
        _write_file(
            root / "src/main/java/com/acme/runtime/Launcher.java",
            """\
            package com.acme.runtime;

            import java.util.concurrent.Executor;

            public final class Launcher {
                public void startThread(WorkerThread worker) {
                    worker.start();
                }

                public void executeTask(Executor executor, WorkerTask task) {
                    executor.execute(task);
                }
            }
            """,
        )

        idx = ProjectIndexer(str(root)).index()

        start_symbol = "com.acme.runtime.Launcher.startThread(WorkerThread)"
        execute_symbol = "com.acme.runtime.Launcher.executeTask(Executor,WorkerTask)"
        thread_run_symbol = "com.acme.runtime.WorkerThread.run()"
        task_run_symbol = "com.acme.runtime.WorkerTask.run()"

        assert thread_run_symbol in idx.global_dependency_graph[start_symbol]
        assert task_run_symbol in idx.global_dependency_graph[execute_symbol]
        assert start_symbol in idx.reverse_dependency_graph[thread_run_symbol]
        assert execute_symbol in idx.reverse_dependency_graph[task_run_symbol]
