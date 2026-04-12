"""Tests for the dead_code module."""

from __future__ import annotations

from token_savior.models import (
    ClassInfo,
    FunctionInfo,
    ImportInfo,
    LineRange,
    ProjectIndex,
    StructuralMetadata,
)
from token_savior.dead_code import find_dead_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_func(
    name: str,
    qualified_name: str | None = None,
    line_start: int = 1,
    line_end: int = 5,
    decorators: list[str] | None = None,
    parameters: list[str] | None = None,
    is_method: bool = False,
    parent_class: str | None = None,
) -> FunctionInfo:
    return FunctionInfo(
        name=name,
        qualified_name=qualified_name or name,
        line_range=LineRange(line_start, line_end),
        parameters=parameters or [],
        decorators=decorators or [],
        docstring=None,
        is_method=is_method,
        parent_class=parent_class,
    )


def _make_class(
    name: str,
    line_start: int = 1,
    line_end: int = 10,
    decorators: list[str] | None = None,
    methods: list[FunctionInfo] | None = None,
    base_classes: list[str] | None = None,
    qualified_name: str | None = None,
) -> ClassInfo:
    return ClassInfo(
        name=name,
        line_range=LineRange(line_start, line_end),
        base_classes=base_classes or [],
        methods=methods or [],
        decorators=decorators or [],
        docstring=None,
        qualified_name=qualified_name,
    )


def _make_meta(
    source_name: str,
    functions: list[FunctionInfo] | None = None,
    classes: list[ClassInfo] | None = None,
    lines: list[str] | None = None,
) -> StructuralMetadata:
    content_lines = lines or []
    return StructuralMetadata(
        source_name=source_name,
        total_lines=len(content_lines) or 50,
        total_chars=sum(len(line) for line in content_lines) or 500,
        lines=content_lines,
        line_char_offsets=[],
        functions=functions or [],
        classes=classes or [],
    )


def _make_index(
    files: dict[str, StructuralMetadata],
    reverse_dependency_graph: dict[str, set[str]] | None = None,
    duplicate_classes: dict[str, list[str]] | None = None,
) -> ProjectIndex:
    return ProjectIndex(
        root_path="/project",
        files=files,
        reverse_dependency_graph=reverse_dependency_graph or {},
        duplicate_classes=duplicate_classes or {},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBasicDeadCode:
    def test_function_with_no_dependents_is_dead(self):
        func = _make_func("unused_helper", line_start=15)
        meta = _make_meta("src/utils.py", functions=[func])
        index = _make_index({"src/utils.py": meta})
        result = find_dead_code(index)
        assert "unused_helper" in result
        assert "1 unreferenced symbol" in result

    def test_function_with_dependents_is_not_dead(self):
        func = _make_func("used_helper", line_start=10)
        meta = _make_meta("src/utils.py", functions=[func])
        index = _make_index(
            {"src/utils.py": meta},
            reverse_dependency_graph={"used_helper": {"caller"}},
        )
        result = find_dead_code(index)
        assert "used_helper" not in result
        assert "0 unreferenced symbols" in result

    def test_cross_project_callers_keep_symbol_live(self):
        func = _make_func(
            "decode",
            qualified_name="com.acme.feed.TradeDecoder.decode(byte[])",
            line_start=10,
            is_method=True,
            parent_class="TradeDecoder",
        )
        cls = _make_class(
            "TradeDecoder",
            methods=[func],
            qualified_name="com.acme.feed.TradeDecoder",
        )
        index = _make_index(
            {"src/main/java/com/acme/feed/TradeDecoder.java": _make_meta(
                "src/main/java/com/acme/feed/TradeDecoder.java",
                functions=[func],
                classes=[cls],
            )}
        )
        sibling = ProjectIndex(
            root_path="/sibling",
            files={},
            global_dependency_graph={
                "com.acme.app.DecoderUser.run()": {"com.acme.feed.TradeDecoder.decode(byte[])"}
            },
        )
        result = find_dead_code(index, sibling_indices={"sibling": sibling})
        assert "TradeDecoder" not in result
        assert "decode(" not in result

    def test_cross_project_imports_keep_class_api_live(self):
        func = _make_func(
            "buffer",
            qualified_name="com.acme.lvc.LvcStore.buffer()",
            line_start=10,
            is_method=True,
            parent_class="LvcStore",
        )
        cls = _make_class(
            "LvcStore",
            methods=[func],
            qualified_name="com.acme.lvc.LvcStore",
        )
        index = _make_index(
            {"src/main/java/com/acme/lvc/LvcStore.java": _make_meta(
                "src/main/java/com/acme/lvc/LvcStore.java",
                functions=[func],
                classes=[cls],
            )}
        )
        sibling = ProjectIndex(
            root_path="/sibling",
            files={
                "src/main/java/com/acme/app/UseStore.java": StructuralMetadata(
                    source_name="src/main/java/com/acme/app/UseStore.java",
                    total_lines=1,
                    total_chars=0,
                    lines=[""],
                    line_char_offsets=[],
                    imports=[],
                )
            },
        )
        sibling.files["src/main/java/com/acme/app/UseStore.java"].imports = [
            ImportInfo(
                module="com.acme.lvc.LvcStore",
                names=["LvcStore"],
                alias=None,
                line_number=1,
                is_from_import=False,
            )
        ]

        result = find_dead_code(index, sibling_indices={"sibling": sibling})
        assert "LvcStore" not in result
        assert "buffer(" not in result

    def test_empty_index_returns_zero(self):
        index = _make_index({})
        result = find_dead_code(index)
        assert "0 unreferenced symbols" in result


class TestEntryPoints:
    def test_main_function_not_flagged(self):
        func = _make_func("main", line_start=5)
        meta = _make_meta("src/app.py", functions=[func])
        index = _make_index({"src/app.py": meta})
        result = find_dead_code(index)
        assert "main" not in result
        assert "0 unreferenced symbols" in result

    def test_dunder_init_not_flagged(self):
        func = _make_func(
            "__init__",
            qualified_name="MyClass.__init__",
            line_start=5,
            is_method=True,
            parent_class="MyClass",
        )
        meta = _make_meta("src/myclass.py", functions=[func])
        index = _make_index({"src/myclass.py": meta})
        result = find_dead_code(index)
        assert "__init__" not in result

    def test_dunder_main_not_flagged(self):
        func = _make_func("__main__", line_start=5)
        meta = _make_meta("src/app.py", functions=[func])
        index = _make_index({"src/app.py": meta})
        result = find_dead_code(index)
        assert "__main__" not in result

    def test_route_decorator_not_flagged(self):
        func = _make_func("my_view", line_start=20, decorators=["app.route('/home')"])
        meta = _make_meta("src/views.py", functions=[func])
        index = _make_index({"src/views.py": meta})
        result = find_dead_code(index)
        assert "my_view" not in result

    def test_click_command_decorator_not_flagged(self):
        func = _make_func("cli_cmd", line_start=20, decorators=["click.command()"])
        meta = _make_meta("src/cli.py", functions=[func])
        index = _make_index({"src/cli.py": meta})
        result = find_dead_code(index)
        assert "cli_cmd" not in result

    def test_task_decorator_not_flagged(self):
        func = _make_func("run_task", line_start=20, decorators=["celery.task"])
        meta = _make_meta("src/tasks.py", functions=[func])
        index = _make_index({"src/tasks.py": meta})
        result = find_dead_code(index)
        assert "run_task" not in result

    def test_test_prefix_function_not_flagged(self):
        func = _make_func("test_something", line_start=10)
        meta = _make_meta("src/logic.py", functions=[func])
        index = _make_index({"src/logic.py": meta})
        result = find_dead_code(index)
        assert "test_something" not in result

    def test_fixture_decorator_not_flagged(self):
        func = _make_func("my_fixture", line_start=5, decorators=["pytest.fixture"])
        meta = _make_meta("src/conftest.py", functions=[func])
        index = _make_index({"src/conftest.py": meta})
        result = find_dead_code(index)
        assert "my_fixture" not in result

    def test_setup_decorator_not_flagged(self):
        func = _make_func("setup_method", line_start=5, decorators=["setup"])
        meta = _make_meta("src/test_x.py", functions=[func])
        index = _make_index({"src/test_x.py": meta})
        result = find_dead_code(index)
        assert "setup_method" not in result

    def test_dataclass_decorator_class_not_flagged(self):
        cls = _make_class("MyData", line_start=1, line_end=10, decorators=["dataclass"])
        meta = _make_meta("src/models.py", classes=[cls])
        index = _make_index({"src/models.py": meta})
        result = find_dead_code(index)
        assert "MyData" not in result

    def test_model_decorator_class_not_flagged(self):
        cls = _make_class("UserModel", line_start=1, line_end=10, decorators=["pydantic.model"])
        meta = _make_meta("src/models.py", classes=[cls])
        index = _make_index({"src/models.py": meta})
        result = find_dead_code(index)
        assert "UserModel" not in result


class TestTestFiles:
    def test_function_in_test_file_prefix_not_flagged(self):
        func = _make_func("helper_in_test_file", line_start=5)
        meta = _make_meta("tests/test_utils.py", functions=[func])
        index = _make_index({"tests/test_utils.py": meta})
        result = find_dead_code(index)
        assert "helper_in_test_file" not in result

    def test_function_in_suffix_test_file_not_flagged(self):
        func = _make_func("helper_in_test_file", line_start=5)
        meta = _make_meta("tests/utils_test.py", functions=[func])
        index = _make_index({"tests/utils_test.py": meta})
        result = find_dead_code(index)
        assert "helper_in_test_file" not in result

    def test_function_in_java_test_file_not_flagged(self):
        func = _make_func(
            "testApply",
            qualified_name="com.acme.pricing.PriceEngineTest.testApply()",
            line_start=5,
            is_method=True,
            parent_class="PriceEngineTest",
        )
        meta = _make_meta("src/test/java/com/acme/pricing/PriceEngineTest.java", functions=[func])
        index = _make_index({"src/test/java/com/acme/pricing/PriceEngineTest.java": meta})
        result = find_dead_code(index)
        assert "testApply" not in result


class TestInitPy:
    def test_symbol_in_init_py_not_flagged(self):
        func = _make_func("exported_util", line_start=5)
        meta = _make_meta("mypackage/__init__.py", functions=[func])
        index = _make_index({"mypackage/__init__.py": meta})
        result = find_dead_code(index)
        assert "exported_util" not in result


class TestClassDeadCode:
    def test_class_with_no_dependents_is_dead(self):
        cls = _make_class("OldProcessor", line_start=42, line_end=60)
        meta = _make_meta("src/legacy.py", classes=[cls])
        index = _make_index({"src/legacy.py": meta})
        result = find_dead_code(index)
        assert "OldProcessor" in result

    def test_class_with_dependents_not_dead(self):
        cls = _make_class("UsedProcessor", line_start=42, line_end=60)
        meta = _make_meta("src/legacy.py", classes=[cls])
        index = _make_index(
            {"src/legacy.py": meta},
            reverse_dependency_graph={"UsedProcessor": {"main"}},
        )
        result = find_dead_code(index)
        assert "UsedProcessor" not in result

    def test_java_class_uses_qualified_name_for_dependents(self):
        cls = ClassInfo(
            name="PriceEngine",
            qualified_name="com.acme.pricing.PriceEngine",
            line_range=LineRange(10, 40),
            base_classes=[],
            methods=[],
            decorators=[],
            docstring=None,
        )
        meta = _make_meta("src/main/java/com/acme/pricing/PriceEngine.java", classes=[cls])
        index = _make_index(
            {"src/main/java/com/acme/pricing/PriceEngine.java": meta},
            reverse_dependency_graph={"com.acme.pricing.PriceEngine": {"caller"}},
        )
        result = find_dead_code(index)
        assert "PriceEngine" not in result

    def test_java_constructor_not_reported_as_dead_code(self):
        constructor = _make_func(
            "PriceEngine",
            qualified_name="com.acme.pricing.PriceEngine.PriceEngine()",
            line_start=12,
            line_end=14,
            is_method=True,
            parent_class="PriceEngine",
        )
        meta = _make_meta("src/main/java/com/acme/pricing/PriceEngine.java", functions=[constructor])
        index = _make_index({"src/main/java/com/acme/pricing/PriceEngine.java": meta})
        result = find_dead_code(index)
        assert "PriceEngine()" not in result


class TestFrameworkAndDispatchHeuristics:
    def test_interface_methods_are_not_reported_as_dead(self):
        iface_method = _make_func(
            "decode",
            qualified_name="com.acme.feed.TradeDecoder.decode(byte[])",
            line_start=2,
            line_end=2,
            is_method=True,
            parent_class="TradeDecoder",
        )
        iface = _make_class(
            "TradeDecoder",
            line_start=1,
            line_end=3,
            methods=[iface_method],
            qualified_name="com.acme.feed.TradeDecoder",
        )
        meta = _make_meta(
            "src/main/java/com/acme/feed/TradeDecoder.java",
            functions=[iface_method],
            classes=[iface],
            lines=[
                "public interface TradeDecoder {",
                "  void decode(byte[] buf);",
                "}",
            ],
        )
        index = _make_index({"src/main/java/com/acme/feed/TradeDecoder.java": meta})
        result = find_dead_code(index)
        assert "decode()" not in result

    def test_signature_propagates_liveness_from_interface_method(self):
        iface_method = _make_func(
            "decode",
            qualified_name="com.acme.feed.TradeDecoder.decode(byte[])",
            line_start=2,
            line_end=2,
            is_method=True,
            parent_class="TradeDecoder",
        )
        impl_method = _make_func(
            "decode",
            qualified_name="com.acme.feed.FastTradeDecoder.decode(byte[])",
            line_start=6,
            line_end=8,
            is_method=True,
            parent_class="FastTradeDecoder",
        )
        iface = _make_class(
            "TradeDecoder",
            line_start=1,
            line_end=3,
            methods=[iface_method],
            qualified_name="com.acme.feed.TradeDecoder",
        )
        impl = _make_class(
            "FastTradeDecoder",
            line_start=5,
            line_end=9,
            methods=[impl_method],
            base_classes=["TradeDecoder"],
            qualified_name="com.acme.feed.FastTradeDecoder",
        )
        meta = _make_meta(
            "src/main/java/com/acme/feed/Decoders.java",
            functions=[iface_method, impl_method],
            classes=[iface, impl],
            lines=[
                "public interface TradeDecoder {",
                "  void decode(byte[] buf);",
                "}",
                "public final class FastTradeDecoder implements TradeDecoder {",
                "}",
            ],
        )
        index = _make_index(
            {"src/main/java/com/acme/feed/Decoders.java": meta},
            reverse_dependency_graph={"com.acme.feed.TradeDecoder.decode(byte[])": {"caller"}},
        )
        result = find_dead_code(index)
        assert "FastTradeDecoder.decode" not in result
    def test_spring_configuration_properties_class_and_accessors_not_flagged(self):
        getter = _make_func(
            "apiKey",
            qualified_name="com.acme.config.AppProperties.apiKey()",
            line_start=6,
            line_end=7,
            is_method=True,
            parent_class="AppProperties",
        )
        setter = _make_func(
            "setApiKey",
            qualified_name="com.acme.config.AppProperties.setApiKey(java.lang.String)",
            line_start=9,
            line_end=10,
            is_method=True,
            parent_class="AppProperties",
        )
        cls = _make_class(
            "AppProperties",
            line_start=1,
            line_end=12,
            decorators=["ConfigurationProperties"],
            methods=[getter, setter],
            qualified_name="com.acme.config.AppProperties",
        )
        meta = _make_meta(
            "src/main/java/com/acme/config/AppProperties.java",
            functions=[getter, setter],
            classes=[cls],
            lines=[
                "@ConfigurationProperties",
                "public final class AppProperties {",
                "}",
            ],
        )
        index = _make_index({"src/main/java/com/acme/config/AppProperties.java": meta})
        result = find_dead_code(index)
        assert "AppProperties" not in result
        assert "apiKey()" not in result
        assert "setApiKey()" not in result

    def test_spring_bean_and_route_methods_not_flagged(self):
        bean = _make_func(
            "redisHotViewReader",
            qualified_name="com.acme.config.AppConfiguration.redisHotViewReader()",
            line_start=6,
            line_end=8,
            decorators=["Bean"],
            is_method=True,
            parent_class="AppConfiguration",
        )
        route = _make_func(
            "status",
            qualified_name="com.acme.web.StatusController.status()",
            line_start=16,
            line_end=18,
            decorators=["GetMapping"],
            is_method=True,
            parent_class="StatusController",
        )
        config_cls = _make_class(
            "AppConfiguration",
            line_start=1,
            line_end=10,
            decorators=["Configuration"],
            methods=[bean],
            qualified_name="com.acme.config.AppConfiguration",
        )
        controller_cls = _make_class(
            "StatusController",
            line_start=12,
            line_end=20,
            decorators=["RestController"],
            methods=[route],
            qualified_name="com.acme.web.StatusController",
        )
        meta = _make_meta(
            "src/main/java/com/acme/App.java",
            functions=[bean, route],
            classes=[config_cls, controller_cls],
            lines=[
                "@Configuration",
                "class AppConfiguration {",
                "}",
                "@RestController",
                "class StatusController {",
                "}",
            ],
        )
        index = _make_index({"src/main/java/com/acme/App.java": meta})
        result = find_dead_code(index)
        assert "redisHotViewReader()" not in result
        assert "status()" not in result

    def test_jmh_files_are_excluded(self):
        benchmark = _make_func(
            "measureThroughput",
            qualified_name="com.acme.bench.QuoteIngressBenchmark.measureThroughput()",
            line_start=6,
            line_end=8,
            decorators=["Benchmark"],
            is_method=True,
            parent_class="QuoteIngressBenchmark",
        )
        state_cls = _make_class(
            "PipelineState",
            line_start=10,
            line_end=15,
            decorators=["State"],
            qualified_name="com.acme.bench.PipelineState",
        )
        benchmark_cls = _make_class(
            "QuoteIngressBenchmark",
            line_start=1,
            line_end=9,
            methods=[benchmark],
            qualified_name="com.acme.bench.QuoteIngressBenchmark",
        )
        meta = _make_meta(
            "src/jmh/java/com/acme/bench/QuoteIngressBenchmark.java",
            functions=[benchmark],
            classes=[benchmark_cls, state_cls],
        )
        index = _make_index({"src/jmh/java/com/acme/bench/QuoteIngressBenchmark.java": meta})
        result = find_dead_code(index)
        assert "QuoteIngressBenchmark" not in result
        assert "measureThroughput()" not in result
        assert "PipelineState" not in result

    def test_java_method_reference_target_not_flagged(self):
        factory = _make_func(
            "cryptoAssetAggregationFactory",
            qualified_name="com.acme.runtime.Factories.cryptoAssetAggregationFactory()",
            line_start=2,
            line_end=4,
            is_method=True,
            parent_class="Factories",
        )
        cls = _make_class(
            "Factories",
            line_start=1,
            line_end=6,
            methods=[factory],
            qualified_name="com.acme.runtime.Factories",
        )
        meta = _make_meta(
            "src/main/java/com/acme/runtime/Factories.java",
            functions=[factory],
            classes=[cls],
            lines=[
                "class Factories {",
                "  GraphDefinition.register(Factories::cryptoAssetAggregationFactory);",
                "}",
            ],
        )
        index = _make_index({"src/main/java/com/acme/runtime/Factories.java": meta})
        result = find_dead_code(index)
        assert "cryptoAssetAggregationFactory()" not in result

    def test_cross_file_method_reference_target_not_flagged(self):
        factory = _make_func(
            "cryptoAssetAggregationFactory",
            qualified_name="com.acme.runtime.Factories.cryptoAssetAggregationFactory()",
            line_start=2,
            line_end=4,
            is_method=True,
            parent_class="Factories",
        )
        factory_cls = _make_class(
            "Factories",
            line_start=1,
            line_end=6,
            methods=[factory],
            qualified_name="com.acme.runtime.Factories",
        )
        factory_meta = _make_meta(
            "src/main/java/com/acme/runtime/Factories.java",
            functions=[factory],
            classes=[factory_cls],
            lines=[
                "class Factories {",
                "  static Object cryptoAssetAggregationFactory() { return null; }",
                "}",
            ],
        )
        graph_meta = _make_meta(
            "src/main/java/com/acme/runtime/TradingGraphs.java",
            lines=[
                "class TradingGraphs {",
                "  void register() { GraphDefinition.register(Factories::cryptoAssetAggregationFactory); }",
                "}",
            ],
        )
        index = _make_index(
            {
                "src/main/java/com/acme/runtime/Factories.java": factory_meta,
                "src/main/java/com/acme/runtime/TradingGraphs.java": graph_meta,
            }
        )
        result = find_dead_code(index)
        assert "cryptoAssetAggregationFactory()" not in result

    def test_java_override_and_runnable_run_not_flagged(self):
        override = _make_func(
            "onOpen",
            qualified_name="com.acme.ws.BinanceConsumer.onOpen()",
            line_start=2,
            line_end=4,
            decorators=["Override"],
            is_method=True,
            parent_class="BinanceConsumer",
        )
        run = _make_func(
            "run",
            qualified_name="com.acme.runtime.RuntimeShutdownThread.run()",
            line_start=7,
            line_end=9,
            is_method=True,
            parent_class="RuntimeShutdownThread",
        )
        consumer_cls = _make_class(
            "BinanceConsumer",
            line_start=1,
            line_end=5,
            methods=[override],
            base_classes=["WebSocketFrameConsumer"],
            qualified_name="com.acme.ws.BinanceConsumer",
        )
        runnable_cls = _make_class(
            "RuntimeShutdownThread",
            line_start=6,
            line_end=10,
            methods=[run],
            base_classes=["Runnable"],
            qualified_name="com.acme.runtime.RuntimeShutdownThread",
        )
        meta = _make_meta(
            "src/main/java/com/acme/runtime/RuntimeHooks.java",
            functions=[override, run],
            classes=[consumer_cls, runnable_cls],
        )
        index = _make_index({"src/main/java/com/acme/runtime/RuntimeHooks.java": meta})
        result = find_dead_code(index)
        assert "onOpen()" not in result
        assert "run()" not in result

    def test_callback_like_methods_without_override_are_not_flagged(self):
        callback = _make_func(
            "accept",
            qualified_name="com.acme.feed.AlpacaTradeConsumer.accept()",
            line_start=3,
            line_end=5,
            is_method=True,
            parent_class="AlpacaTradeConsumer",
        )
        cls = _make_class(
            "AlpacaTradeConsumer",
            line_start=1,
            line_end=6,
            methods=[callback],
            base_classes=["TradeConsumer"],
            qualified_name="com.acme.feed.AlpacaTradeConsumer",
        )
        meta = _make_meta(
            "src/main/java/com/acme/feed/AlpacaTradeConsumer.java",
            functions=[callback],
            classes=[cls],
        )
        index = _make_index({"src/main/java/com/acme/feed/AlpacaTradeConsumer.java": meta})
        result = find_dead_code(index)
        assert "accept()" not in result

    def test_duplicate_java_classes_and_methods_are_not_reported_as_dead(self):
        method_a = _make_func(
            "render",
            qualified_name="com.acme.view.HotViewKeys.render()",
            line_start=2,
            line_end=4,
            is_method=True,
            parent_class="HotViewKeys",
        )
        method_b = _make_func(
            "render",
            qualified_name="com.acme.view.HotViewKeys.render()",
            line_start=2,
            line_end=4,
            is_method=True,
            parent_class="HotViewKeys",
        )
        class_a = _make_class(
            "HotViewKeys",
            line_start=1,
            line_end=5,
            methods=[method_a],
            qualified_name="com.acme.view.HotViewKeys",
        )
        class_b = _make_class(
            "HotViewKeys",
            line_start=1,
            line_end=5,
            methods=[method_b],
            qualified_name="com.acme.view.HotViewKeys",
        )
        index = _make_index(
            {
                "src/main/java/com/acme/view/HotViewKeys.java": _make_meta(
                    "src/main/java/com/acme/view/HotViewKeys.java",
                    functions=[method_a],
                    classes=[class_a],
                ),
                "src/generated/java/com/acme/view/HotViewKeys.java": _make_meta(
                    "src/generated/java/com/acme/view/HotViewKeys.java",
                    functions=[method_b],
                    classes=[class_b],
                ),
            },
            duplicate_classes={
                "com.acme.view.HotViewKeys": [
                    "src/generated/java/com/acme/view/HotViewKeys.java",
                    "src/main/java/com/acme/view/HotViewKeys.java",
                ]
            },
        )
        result = find_dead_code(index)
        assert "HotViewKeys" not in result
        assert "render()" not in result

    def test_value_class_accessors_and_mutable_setters_not_flagged(self):
        accessor = _make_func(
            "symbol",
            qualified_name="com.acme.events.QuoteEvent.symbol()",
            line_start=2,
            line_end=3,
            is_method=True,
            parent_class="QuoteEvent",
        )
        setter = _make_func(
            "set",
            qualified_name="com.acme.events.MutableQuoteEvent.set()",
            line_start=7,
            line_end=9,
            is_method=True,
            parent_class="MutableQuoteEvent",
        )
        value_cls = _make_class(
            "QuoteEvent",
            line_start=1,
            line_end=4,
            methods=[accessor],
            qualified_name="com.acme.events.QuoteEvent",
        )
        mutable_cls = _make_class(
            "MutableQuoteEvent",
            line_start=6,
            line_end=10,
            methods=[setter],
            qualified_name="com.acme.events.MutableQuoteEvent",
        )
        meta = _make_meta(
            "src/main/java/com/acme/events/QuoteTypes.java",
            functions=[accessor, setter],
            classes=[value_cls, mutable_cls],
            lines=[
                "public final class QuoteEvent {",
                "  public long symbol() {",
                "    return symbol;",
                "  }",
                "}",
                "public final class MutableQuoteEvent {",
                "  public void set(long symbol) {",
                "    this.symbol = symbol;",
                "  }",
                "}",
            ],
        )
        index = _make_index({"src/main/java/com/acme/events/QuoteTypes.java": meta})
        result = find_dead_code(index)
        assert "symbol()" not in result
        assert "set()" not in result

    def test_typescript_ui_files_are_excluded(self):
        func = _make_func("DiagnosticsOverview", line_start=3)
        cls = _make_class("DiagnosticsQuery", line_start=1, line_end=2)
        meta = _make_meta("ui/src/pages/Diagnostics.tsx", functions=[func], classes=[cls])
        index = _make_index({"ui/src/pages/Diagnostics.tsx": meta})
        result = find_dead_code(index)
        assert "DiagnosticsOverview" not in result
        assert "DiagnosticsQuery" not in result

    def test_java_main_class_not_flagged(self):
        main_method = _make_func(
            "main",
            qualified_name="com.acme.runtime.TradeResearchRuntimeMain.main(java.lang.String[])",
            line_start=2,
            line_end=4,
            parameters=["String[] args"],
            is_method=True,
            parent_class="TradeResearchRuntimeMain",
        )
        cls = _make_class(
            "TradeResearchRuntimeMain",
            line_start=1,
            line_end=5,
            methods=[main_method],
            qualified_name="com.acme.runtime.TradeResearchRuntimeMain",
        )
        meta = _make_meta(
            "src/main/java/com/acme/runtime/TradeResearchRuntimeMain.java",
            functions=[main_method],
            classes=[cls],
            lines=[
                "public final class TradeResearchRuntimeMain {",
                "  public static void main(String[] args) {",
                "  }",
                "}",
            ],
        )
        index = _make_index({"src/main/java/com/acme/runtime/TradeResearchRuntimeMain.java": meta})
        result = find_dead_code(index)
        assert "TradeResearchRuntimeMain" not in result


class TestMaxResults:
    def test_max_results_limits_output(self):
        funcs = [_make_func(f"dead_{i}", line_start=i * 10) for i in range(1, 11)]
        meta = _make_meta("src/lots.py", functions=funcs)
        index = _make_index({"src/lots.py": meta})
        result = find_dead_code(index, max_results=3)
        # Should still say total found
        assert "10 unreferenced symbols found" in result
        # But only show 3 entries in the body
        dead_lines = [line for line in result.splitlines() if "function dead_" in line]
        assert len(dead_lines) == 3

    def test_max_results_default_is_50(self):
        funcs = [_make_func(f"dead_{i}", line_start=i * 10) for i in range(1, 60)]
        meta = _make_meta("src/lots.py", functions=funcs)
        index = _make_index({"src/lots.py": meta})
        result = find_dead_code(index)
        dead_lines = [line for line in result.splitlines() if "function dead_" in line]
        assert len(dead_lines) == 50


class TestOutputFormat:
    def test_output_format(self):
        func = _make_func("unused_helper", line_start=15)
        meta = _make_meta("src/utils.py", functions=[func])
        index = _make_index({"src/utils.py": meta})
        result = find_dead_code(index)
        assert "Dead Code Analysis" in result
        assert "src/utils.py:" in result
        assert "line 15:" in result
        assert "function unused_helper" in result

    def test_output_sorted_by_file_then_line(self):
        func_a = _make_func("dead_a", line_start=30)
        func_b = _make_func("dead_b", line_start=5)
        func_z = _make_func("dead_z", line_start=1)
        meta_src = _make_meta("src/z_mod.py", functions=[func_a, func_b])
        meta_aaa = _make_meta("aaa/module.py", functions=[func_z])
        index = _make_index({"src/z_mod.py": meta_src, "aaa/module.py": meta_aaa})
        result = find_dead_code(index)
        pos_aaa = result.index("aaa/module.py")
        pos_src = result.index("src/z_mod.py")
        assert pos_aaa < pos_src, "Files should be sorted alphabetically"
        pos_b = result.index("line 5:")
        pos_a = result.index("line 30:")
        assert pos_b < pos_a, "Within a file, symbols sorted by line number"

    def test_class_output_format(self):
        cls = _make_class("OldProcessor", line_start=42, line_end=60)
        meta = _make_meta("src/legacy.py", classes=[cls])
        index = _make_index({"src/legacy.py": meta})
        result = find_dead_code(index)
        assert "class OldProcessor" in result
        assert "line 42:" in result
