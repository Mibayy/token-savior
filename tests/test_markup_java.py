"""Tests for the tree-sitter-based Java annotator."""

from token_savior.java_annotator import annotate_java


SOURCE_COMPLEX = """\
package com.acme.pricing;

import com.acme.shared.MathUtil;
import static java.util.Objects.requireNonNull;

public final class PriceEngine implements QuotePublisher {
    private final MathUtil util;

    public PriceEngine(MathUtil util) {
        this.util = requireNonNull(util);
    }

    public int apply(int input) {
        helper();
        return MathUtil.scale(input);
    }

    private void helper() {
    }

    public static class Builder {
        public PriceEngine build(MathUtil util) {
            return new PriceEngine(util);
        }
    }
}
"""


class TestJavaImports:
    def test_package_and_imports_are_extracted(self):
        meta = annotate_java(SOURCE_COMPLEX, "PriceEngine.java")

        assert meta.module_name == "com.acme.pricing"
        assert len(meta.imports) == 2

        explicit_import = meta.imports[0]
        assert explicit_import.module == "com.acme.shared.MathUtil"
        assert explicit_import.names == ["MathUtil"]
        assert explicit_import.is_from_import is False

        static_import = meta.imports[1]
        assert static_import.module == "java.util.Objects"
        assert static_import.names == ["requireNonNull"]
        assert static_import.is_from_import is True


class TestJavaTypeAndMethodExtraction:
    def test_classes_and_methods_have_qualified_names(self):
        meta = annotate_java(SOURCE_COMPLEX, "PriceEngine.java")

        class_names = {cls.name for cls in meta.classes}
        assert {"PriceEngine", "Builder"} <= class_names

        engine = next(cls for cls in meta.classes if cls.name == "PriceEngine")
        assert engine.qualified_name == "com.acme.pricing.PriceEngine"
        assert "QuotePublisher" in engine.base_classes
        assert {method.name for method in engine.methods} == {
            "PriceEngine",
            "apply",
            "helper",
        }

        builder = next(cls for cls in meta.classes if cls.name == "Builder")
        assert builder.qualified_name == "com.acme.pricing.PriceEngine.Builder"
        assert [method.name for method in builder.methods] == ["build"]

        apply = next(func for func in meta.functions if func.name == "apply")
        assert apply.qualified_name == "com.acme.pricing.PriceEngine.apply(int)"
        assert apply.parameters == ["input"]
        assert engine.visibility == "public"
        assert apply.visibility == "public"
        assert apply.return_type == "int"

        helper = next(func for func in meta.functions if func.name == "helper")
        assert helper.visibility == "private"
        assert helper.return_type == "void"

    def test_dependency_graph_tracks_local_and_imported_references(self):
        meta = annotate_java(SOURCE_COMPLEX, "PriceEngine.java")

        apply_deps = set(meta.dependency_graph["com.acme.pricing.PriceEngine.apply(int)"])
        assert "com.acme.pricing.PriceEngine.helper()" in apply_deps
        assert "com.acme.shared.MathUtil" in apply_deps

        constructor_deps = set(
            meta.dependency_graph["com.acme.pricing.PriceEngine.PriceEngine(MathUtil)"]
        )
        assert "java.util.Objects" in constructor_deps

    def test_dependency_graph_uses_ast_for_receiver_calls_method_refs_and_lambdas(self):
        src = """\
package com.acme.runtime;

import java.util.function.Supplier;

interface Handler {
    void onTrade(String trade);
}

final class Worker {
    void run() {
    }
}

final class FooNode {
    FooNode() {
    }
}

public final class Engine {
    private final Handler handler;

    Engine(Handler handler) {
        this.handler = handler;
    }

    public void register(String trade) {
        handler.onTrade(trade);
        Runnable runnable = Worker::run;
        Supplier<FooNode> supplier = () -> new FooNode();
    }
}
"""
        meta = annotate_java(src, "Engine.java")

        register_deps = set(meta.dependency_graph["com.acme.runtime.Engine.register(String)"])
        assert "com.acme.runtime.Handler.onTrade(String)" in register_deps
        assert "com.acme.runtime.Worker.run()" in register_deps
        assert "com.acme.runtime.FooNode" in register_deps

    def test_dependency_graph_binds_runtime_launch_sites(self):
        src = """\
package com.acme.runtime;

import java.util.concurrent.Executor;

final class WorkerThread extends Thread {
    @Override
    public void run() {
    }
}

final class WorkerTask implements Runnable {
    @Override
    public void run() {
    }
}

public final class Launcher {
    public void startThread(WorkerThread worker) {
        worker.start();
    }

    public void executeTask(Executor executor, WorkerTask task) {
        executor.execute(task);
    }
}
"""
        meta = annotate_java(src, "Launcher.java")

        start_deps = set(meta.dependency_graph["com.acme.runtime.Launcher.startThread(WorkerThread)"])
        execute_deps = set(
            meta.dependency_graph["com.acme.runtime.Launcher.executeTask(Executor,WorkerTask)"]
        )
        assert "com.acme.runtime.WorkerThread.run()" in start_deps
        assert "com.acme.runtime.WorkerTask.run()" in execute_deps

    def test_record_and_overloaded_methods_are_detected(self):
        src = """\
package com.acme.model;

public record Price(int bid, int ask) {
    public Price {
    }

    public int spread() {
        return ask - bid;
    }

    public int format(int scale) {
        return scale;
    }

    public int format(int scale, int precision) {
        return scale + precision;
    }
}
"""
        meta = annotate_java(src, "Price.java")

        record_cls = next(cls for cls in meta.classes if cls.name == "Price")
        assert record_cls.qualified_name == "com.acme.model.Price"
        assert {func.name for func in meta.functions} >= {"Price", "spread", "format"}
        assert len([func for func in meta.functions if func.name == "format"]) == 2
        assert {
            func.qualified_name for func in meta.functions if func.name == "format"
        } == {
            "com.acme.model.Price.format(int)",
            "com.acme.model.Price.format(int,int)",
        }

    def test_annotation_enum_and_javadocs_are_extracted(self):
        src = """\
package com.acme.meta;

/**
 * Marks pricing handlers.
 */
public @interface Handler {
    String value();
}

/**
 * Execution state.
 */
public enum Status {
    NEW,
    RUNNING;

    /**
     * Whether work started.
     */
    public boolean isActive() {
        return this == RUNNING;
    }
}
"""
        meta = annotate_java(src, "Meta.java")

        handler = next(cls for cls in meta.classes if cls.name == "Handler")
        assert handler.qualified_name == "com.acme.meta.Handler"
        assert handler.docstring == "Marks pricing handlers."
        assert [method.name for method in handler.methods] == ["value"]

        value = next(func for func in meta.functions if func.name == "value")
        assert value.qualified_name == "com.acme.meta.Handler.value()"

        status = next(cls for cls in meta.classes if cls.name == "Status")
        assert status.docstring == "Execution state."
        assert [method.name for method in status.methods] == ["isActive"]

        is_active = next(func for func in meta.functions if func.name == "isActive")
        assert is_active.qualified_name == "com.acme.meta.Status.isActive()"
        assert is_active.docstring == "Whether work started."

    def test_module_info_and_package_info_are_surfaced_as_sections(self):
        module_src = """\
open module com.acme.app {
    requires transitive java.sql;
    exports com.acme.api;
    uses com.acme.spi.Plugin;
    provides com.acme.spi.Plugin with com.acme.impl.DefaultPlugin;
}
"""
        module_meta = annotate_java(module_src, "module-info.java")
        assert module_meta.module_name == "com.acme.app"
        assert [section.title for section in module_meta.sections] == [
            "open module com.acme.app",
            "requires transitive java.sql",
            "exports com.acme.api",
            "uses com.acme.spi.Plugin",
            "provides com.acme.spi.Plugin with com.acme.impl.DefaultPlugin",
        ]

        package_src = """\
/**
 * Public API package.
 */
@Deprecated
package com.acme.api;
"""
        package_meta = annotate_java(package_src, "package-info.java")
        assert package_meta.module_name == "com.acme.api"
        titles = [section.title for section in package_meta.sections]
        assert "Javadoc Public API package." in titles
        assert "@Deprecated" in titles
        assert "package com.acme.api" in titles

    def test_interface_varargs_and_static_wildcard_imports_are_supported(self):
        src = """\
package com.acme.api;

import static com.acme.shared.MathUtil.*;

public interface Pricer {
    @Deprecated
    int quote(String symbol, int... levels);
}
"""
        meta = annotate_java(src, "Pricer.java")

        assert meta.module_name == "com.acme.api"
        assert len(meta.imports) == 1
        assert meta.imports[0].module == "com.acme.shared.MathUtil"
        assert meta.imports[0].names == ["*"]
        assert meta.imports[0].is_from_import is True

        pricer = next(cls for cls in meta.classes if cls.name == "Pricer")
        assert pricer.qualified_name == "com.acme.api.Pricer"
        assert [method.name for method in pricer.methods] == ["quote"]

        quote = next(func for func in meta.functions if func.name == "quote")
        assert quote.qualified_name == "com.acme.api.Pricer.quote(String,int...)"
        assert quote.parameters == ["symbol", "levels"]
        assert quote.decorators == ["Deprecated"]

    def test_sealed_types_generic_signatures_and_permits_are_supported(self):
        src = """\
package com.acme.shape;

public sealed interface Shape permits Circle, Square {
    @Deprecated
    default <T extends Number> T scale(
        java.util.List<String> labels,
        int[] dims,
        String... names
    ) {
        return null;
    }
}

final class Circle implements Shape {
}

non-sealed class Square implements Shape {
}
"""
        meta = annotate_java(src, "Shape.java")

        shape = next(cls for cls in meta.classes if cls.name == "Shape")
        assert shape.qualified_name == "com.acme.shape.Shape"
        assert set(shape.base_classes) == {"Circle", "Square"}
        assert [method.name for method in shape.methods] == ["scale"]

        scale = next(func for func in meta.functions if func.name == "scale")
        assert (
            scale.qualified_name
            == "com.acme.shape.Shape.scale(java.util.List<String>,int[],String...)"
        )
        assert scale.parameters == ["labels", "dims", "names"]
        assert scale.decorators == ["Deprecated"]

        circle = next(cls for cls in meta.classes if cls.name == "Circle")
        square = next(cls for cls in meta.classes if cls.name == "Square")
        assert circle.base_classes == ["Shape"]
        assert square.base_classes == ["Shape"]

        shape_deps = set(meta.dependency_graph["com.acme.shape.Shape"])
        assert "com.acme.shape.Circle" in shape_deps
        assert "com.acme.shape.Square" in shape_deps

    def test_package_info_supports_multiple_annotations(self):
        src = """\
/**
 * Public API package.
 */
@com.acme.meta.PublicApi
@Deprecated
package com.acme.api;
"""
        meta = annotate_java(src, "package-info.java")

        assert meta.module_name == "com.acme.api"
        titles = [section.title for section in meta.sections]
        assert "Javadoc Public API package." in titles
        assert "@PublicApi" in titles
        assert "@Deprecated" in titles
        assert "package com.acme.api" in titles

    def test_abstract_methods_inner_classes_and_nested_records_are_supported(self):
        src = """\
package com.acme.nested;

public abstract class Outer {
    public abstract int compute(String symbol);

    public class Inner {
        public record Result(int value) {
            public Result {
            }
        }

        public Result build(int value) {
            return new Result(value);
        }
    }
}
"""
        meta = annotate_java(src, "Outer.java")

        outer = next(cls for cls in meta.classes if cls.name == "Outer")
        inner = next(cls for cls in meta.classes if cls.qualified_name == "com.acme.nested.Outer.Inner")
        result = next(
            cls for cls in meta.classes if cls.qualified_name == "com.acme.nested.Outer.Inner.Result"
        )

        assert outer.qualified_name == "com.acme.nested.Outer"
        assert {method.name for method in outer.methods} == {"compute"}

        compute = next(func for func in meta.functions if func.name == "compute")
        assert compute.qualified_name == "com.acme.nested.Outer.compute(String)"
        assert compute.parameters == ["symbol"]

        assert inner.qualified_name == "com.acme.nested.Outer.Inner"
        assert [method.name for method in inner.methods] == ["build"]

        build = next(func for func in meta.functions if func.name == "build")
        assert build.qualified_name == "com.acme.nested.Outer.Inner.build(int)"

        assert result.qualified_name == "com.acme.nested.Outer.Inner.Result"
        assert [method.name for method in result.methods] == ["Result"]

        build_deps = set(meta.dependency_graph["com.acme.nested.Outer.Inner.build(int)"])
        assert "com.acme.nested.Outer.Inner.Result" in build_deps

    def test_module_info_supports_opens_requires_static_and_multiple_exports(self):
        src = """\
module com.acme.app {
    requires static java.sql;
    opens com.acme.internal to com.acme.framework, com.acme.tests;
    exports com.acme.api;
    exports com.acme.spi to com.partner.client;
}
"""
        meta = annotate_java(src, "module-info.java")

        assert meta.module_name == "com.acme.app"
        assert [section.title for section in meta.sections] == [
            "module com.acme.app",
            "requires static java.sql",
            "opens com.acme.internal to com.acme.framework, com.acme.tests",
            "exports com.acme.api",
            "exports com.acme.spi to com.partner.client",
        ]

    def test_wildcard_generic_bounds_are_preserved_in_method_signatures(self):
        src = """\
package com.acme.generics;

public final class Bounds {
    public void apply(
        java.util.List<? extends Number> sources,
        java.util.Map<String, ? super Integer> sink
    ) {
    }
}
"""
        meta = annotate_java(src, "Bounds.java")

        apply = next(func for func in meta.functions if func.name == "apply")
        assert (
            apply.qualified_name
            == "com.acme.generics.Bounds.apply(java.util.List<? extends Number>,java.util.Map<String, ? super Integer>)"
        )
        assert apply.parameters == ["sources", "sink"]

    def test_annotation_elements_with_defaults_are_extracted(self):
        src = """\
package com.acme.meta;

public @interface Config {
    String value() default "prod";
    int[] levels() default {1, 2, 3};
}
"""
        meta = annotate_java(src, "Config.java")

        config = next(cls for cls in meta.classes if cls.name == "Config")
        assert config.qualified_name == "com.acme.meta.Config"
        assert [method.name for method in config.methods] == ["value", "levels"]
        assert {
            func.qualified_name for func in meta.functions if func.parent_class == "Config"
        } == {
            "com.acme.meta.Config.value()",
            "com.acme.meta.Config.levels()",
        }

    def test_local_and_anonymous_classes_get_scoped_symbols_without_simple_aliases(self):
        src = """\
package com.acme.local;

public final class Worker {
    public int execute(int value) {
        class LocalFormatter {
            int format(int input) {
                return input + 1;
            }
        }

        Runnable runnable = new Runnable() {
            @Override
            public void run() {
            }
        };

        runnable.run();
        return new LocalFormatter().format(value);
    }
}
"""
        meta = annotate_java(src, "Worker.java")

        worker = next(cls for cls in meta.classes if cls.name == "Worker")
        assert worker.qualified_name == "com.acme.local.Worker"
        assert [method.name for method in worker.methods] == ["execute"]

        execute = next(func for func in meta.functions if func.name == "execute")
        assert execute.qualified_name == "com.acme.local.Worker.execute(int)"
        assert execute.parameters == ["value"]
        local_formatter = next(
            cls
            for cls in meta.classes
            if cls.qualified_name
            == "com.acme.local.Worker.execute(int)::<local>.LocalFormatter"
        )
        assert local_formatter.name == "LocalFormatter"
        assert [method.name for method in local_formatter.methods] == ["format"]

        local_format = next(
            func
            for func in meta.functions
            if func.qualified_name
            == "com.acme.local.Worker.execute(int)::<local>.LocalFormatter.format(int)"
        )
        assert local_format.parameters == ["input"]

        execute_deps = set(meta.dependency_graph["com.acme.local.Worker.execute(int)"])
        assert "com.acme.local.Worker.execute(int)::<local>.LocalFormatter" in execute_deps
