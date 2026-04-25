"""Tests for the tool_capture module (sandbox of verbose tool outputs)."""
from __future__ import annotations

import pytest

from token_savior import db_core
from token_savior.memory import tool_capture


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch, tmp_path):
    """Each test gets its own SQLite file so captures don't leak between tests."""
    db_path = tmp_path / "test_memory.sqlite"
    monkeypatch.setattr(db_core, "MEMORY_DB_PATH", db_path)
    # Reset the cached migrations set so run_migrations actually runs.
    db_core._migrated_paths.clear()
    db_core.run_migrations(db_path)
    yield db_path


def test_put_returns_handle_and_preview():
    output = "small payload"
    res = tool_capture.capture_put(tool_name="Bash", output=output)
    assert res["id"] is not None
    assert res["uri"].startswith("ts://capture/")
    assert res["bytes"] == len(output)
    assert res["preview"] == output  # below preview cap, returned as-is


def test_put_truncates_above_8mib():
    big = "x" * (9 * 1024 * 1024)
    res = tool_capture.capture_put(tool_name="Bash", output=big)
    assert res["bytes"] <= 8 * 1024 * 1024 + 50  # cap + marker
    got = tool_capture.capture_get(res["id"], range_spec="all")
    assert "truncated" in got["content"].lower()


def test_preview_handles_long_output():
    lines = "\n".join(f"line-{i}" for i in range(500))
    res = tool_capture.capture_put(tool_name="Bash", output=lines)
    assert "lines omitted" in res["preview"]
    assert len(res["preview"]) < 1500  # preview is bounded


def test_search_finds_specific_term():
    tool_capture.capture_put(
        tool_name="Bash",
        output="a normal log line\nERROR: something broke\nrecovered\n",
        session_id="s1",
    )
    tool_capture.capture_put(
        tool_name="WebFetch",
        output="just html, no errors here",
        session_id="s1",
    )
    hits = tool_capture.capture_search("ERROR", session_id="s1")
    assert len(hits) == 1
    assert hits[0]["tool_name"] == "Bash"
    assert "ERROR" in hits[0]["snippet"] or "error" in hits[0]["snippet"].lower()


def test_search_filter_by_tool_name():
    tool_capture.capture_put(tool_name="Bash", output="payload alpha", session_id="s2")
    tool_capture.capture_put(tool_name="WebFetch", output="payload alpha", session_id="s2")
    hits = tool_capture.capture_search("alpha", session_id="s2", tool_name="WebFetch")
    assert len(hits) == 1
    assert hits[0]["tool_name"] == "WebFetch"


def test_search_returns_empty_for_blank_query():
    tool_capture.capture_put(tool_name="Bash", output="anything")
    assert tool_capture.capture_search("") == []


def test_get_range_specifications():
    lines = "\n".join(f"line-{i}" for i in range(1, 11))  # line-1 .. line-10
    res = tool_capture.capture_put(tool_name="Bash", output=lines)

    head = tool_capture.capture_get(res["id"], range_spec="head")
    assert "line-1" in head["content"] and "line-10" in head["content"]

    tail = tool_capture.capture_get(res["id"], range_spec="tail")
    assert "line-10" in tail["content"]

    full = tool_capture.capture_get(res["id"], range_spec="all")
    assert full["content"] == lines

    sliced = tool_capture.capture_get(res["id"], range_spec="line:3-5")
    assert sliced["content"] == "line-3\nline-4\nline-5"

    preview = tool_capture.capture_get(res["id"], range_spec="preview")
    assert preview["content"]  # not None, not empty


def test_get_max_bytes_caps_response():
    res = tool_capture.capture_put(tool_name="Bash", output="x" * 1000)
    got = tool_capture.capture_get(res["id"], range_spec="all", max_bytes=100)
    assert len(got["content"]) <= 110  # 100 + capped marker


def test_get_returns_none_for_missing_id():
    assert tool_capture.capture_get(99999) is None


def test_aggregate_stats():
    text = "first\nsecond\nthird\n"
    res = tool_capture.capture_put(tool_name="Bash", output=text)
    agg = tool_capture.capture_aggregate(res["id"], transform="stats")
    assert agg["lines"] == 3
    assert agg["words"] == 3
    assert agg["first_line"] == "first"
    assert agg["last_line"] == "third"


def test_aggregate_extract_regex():
    text = "see https://a.example and https://b.example also https://a.example again"
    res = tool_capture.capture_put(tool_name="WebFetch", output=text)
    agg = tool_capture.capture_aggregate(res["id"], transform="extract:https?://\\S+")
    assert agg["distinct_matches"] == 2  # dedup
    assert "https://a.example" in agg["matches"]


def test_aggregate_count_regex():
    text = "ERROR\nINFO\nERROR\nWARN\nERROR"
    res = tool_capture.capture_put(tool_name="Bash", output=text)
    agg = tool_capture.capture_aggregate(res["id"], transform="count:ERROR")
    assert agg["count"] == 3


def test_aggregate_unique_lines():
    text = "a\nb\na\nc\nb\nd"
    res = tool_capture.capture_put(tool_name="Bash", output=text)
    agg = tool_capture.capture_aggregate(res["id"], transform="unique_lines")
    assert agg["unique_lines"] == 4


def test_aggregate_bad_regex_returns_error():
    res = tool_capture.capture_put(tool_name="Bash", output="x")
    agg = tool_capture.capture_aggregate(res["id"], transform="extract:[unclosed")
    assert "error" in agg


def test_aggregate_unknown_transform():
    res = tool_capture.capture_put(tool_name="Bash", output="x")
    agg = tool_capture.capture_aggregate(res["id"], transform="banana")
    assert "error" in agg


def test_list_orders_newest_first():
    a = tool_capture.capture_put(tool_name="Bash", output="first", session_id="ord")
    b = tool_capture.capture_put(tool_name="Bash", output="second", session_id="ord")
    rows = tool_capture.capture_list(session_id="ord")
    assert len(rows) == 2
    assert rows[0]["id"] == b["id"]
    assert rows[1]["id"] == a["id"]


def test_list_filter_by_tool_name():
    tool_capture.capture_put(tool_name="Bash", output="x", session_id="lf")
    tool_capture.capture_put(tool_name="WebFetch", output="y", session_id="lf")
    rows = tool_capture.capture_list(session_id="lf", tool_name="WebFetch")
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "WebFetch"


def test_purge_by_session():
    tool_capture.capture_put(tool_name="Bash", output="a", session_id="p1")
    tool_capture.capture_put(tool_name="Bash", output="b", session_id="p2")
    n = tool_capture.capture_purge(session_id="p1")
    assert n == 1
    assert len(tool_capture.capture_list(session_id="p1")) == 0
    assert len(tool_capture.capture_list(session_id="p2")) == 1


def test_purge_requires_filter():
    """Without any filter, purge must be a noop to prevent accidental wipes."""
    tool_capture.capture_put(tool_name="Bash", output="keep me")
    n = tool_capture.capture_purge()
    assert n == 0
    assert tool_capture.capture_list()  # still there


def test_meta_persists_as_json():
    res = tool_capture.capture_put(
        tool_name="Bash", output="x", meta={"exit_code": 1, "duration_ms": 42}
    )
    got = tool_capture.capture_get(res["id"])
    assert "exit_code" in (got["meta_json"] or "")
