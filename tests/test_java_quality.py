"""Tests for Java allocation/performance hotspot detection."""

from __future__ import annotations

from pathlib import Path

from token_savior.java_quality import (
    find_allocation_hotspots,
    find_performance_hotspots,
)
from token_savior.project_indexer import ProjectIndexer


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestJavaAllocationHotspots:
    def test_ranks_java_methods_by_allocation_score(self, tmp_path):
        root = tmp_path / "alloc-project"
        _write_file(
            root / "src/main/java/com/acme/runtime/AllocationExamples.java",
            """\
            package com.acme.runtime;

            import java.util.ArrayList;
            import java.util.List;
            import java.util.Optional;

            public final class AllocationExamples {
                public String heavy(int value) {
                    List<String> values = new ArrayList<>();
                    values.add(String.format("v-%d", value));
                    return Optional.of(Integer.valueOf(value)).map(Object::toString).orElse("");
                }

                public int light(int value) {
                    return value + 1;
                }
            }
            """,
        )

        index = ProjectIndexer(str(root)).index()
        result = find_allocation_hotspots(index)

        assert "Java Allocation Hotspots" in result
        assert "com.acme.runtime.AllocationExamples.heavy(int)" in result
        assert "collection/buffer construction" in result
        assert "string formatting" in result
        assert "optional wrapper" in result
        assert "boxing helper" in result
        assert "com.acme.runtime.AllocationExamples.light(int)" not in result


class TestJavaPerformanceHotspots:
    def test_ranks_java_methods_by_performance_score(self, tmp_path):
        root = tmp_path / "perf-project"
        _write_file(
            root / "src/main/java/com/acme/runtime/PerformanceExamples.java",
            """\
            package com.acme.runtime;

            import java.util.concurrent.CompletableFuture;
            import java.util.concurrent.locks.ReentrantLock;

            public final class PerformanceExamples {
                private volatile long sequence;
                private volatile long cursor;
                private final ReentrantLock lock = new ReentrantLock();

                public long waitForWork(CompletableFuture<Long> future) throws Exception {
                    lock.lock();
                    try {
                        sequence++;
                        cursor = future.get();
                        return cursor;
                    } finally {
                        lock.unlock();
                    }
                }

                public long fastPath(long value) {
                    return value + sequence + cursor;
                }
            }
            """,
        )

        index = ProjectIndexer(str(root)).index()
        result = find_performance_hotspots(index)

        assert "Java Performance Hotspots" in result
        assert "com.acme.runtime.PerformanceExamples.waitForWork(" in result
        assert "explicit lock" in result
        assert "blocking wait" in result
        assert "shared mutable fields without cache-line padding" in result
        assert "com.acme.runtime.PerformanceExamples.fastPath(long)" in result

    def test_does_not_treat_map_or_json_get_as_blocking_wait(self, tmp_path):
        root = tmp_path / "perf-false-positive-project"
        _write_file(
            root / "src/main/java/com/acme/runtime/LookupExamples.java",
            """\
            package com.acme.runtime;

            import java.util.Map;

            final class JsonNode {
                JsonNode get(String key) {
                    return this;
                }
            }

            public final class LookupExamples {
                public String read(Map<String, String> values, JsonNode node) {
                    String left = values.get("left");
                    JsonNode right = node.get("right");
                    return left == null ? "" : left + right;
                }
            }
            """,
        )

        index = ProjectIndexer(str(root)).index()
        result = find_performance_hotspots(index)

        assert "blocking wait" not in result

    def test_returns_empty_message_when_no_java_perf_issues_found(self, tmp_path):
        root = tmp_path / "clean-project"
        _write_file(
            root / "src/main/java/com/acme/runtime/CleanExamples.java",
            """\
            package com.acme.runtime;

            public final class CleanExamples {
                public long fastPath(long value) {
                    return value + 1;
                }
            }
            """,
        )

        index = ProjectIndexer(str(root)).index()

        assert find_performance_hotspots(index) == "No Java performance hotspots found."
