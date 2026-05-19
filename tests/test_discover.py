"""Tests for ts_discover: transcript scanner + pattern detectors + MCP handler."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


from token_savior.discover import discover
from token_savior.discover.patterns import (
    BatchFindSymbolPattern,
    EditWithoutContextPattern,
    MemorySearchWithoutIndexPattern,
    NativeShellOnCodePattern,
    ReadGrepReadPattern,
)
from token_savior.discover.transcript_scanner import (
    Event,
    iter_events,
)


# ---------------------------------------------------------------------------
# Helpers — synthetic JSONL fixtures
# ---------------------------------------------------------------------------


def _assistant_event(ts: str, tool_name: str, tool_input: dict) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": "test-session-1",
        "cwd": "/root/test-project",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": tool_name,
                    "input": tool_input,
                }
            ],
        },
    }


def _write_session(
    tmp_path: Path,
    project_dir: str,
    session_name: str,
    events: list[dict],
) -> Path:
    pdir = tmp_path / project_dir
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{session_name}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return path


def _ev(seconds: float, tool: str, args: dict, session: str = "S1") -> Event:
    base = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
    return Event(
        ts=base + timedelta(seconds=seconds),
        tool_name=tool,
        args=args,
        session_id=session,
        project="-root-test",
    )


# ---------------------------------------------------------------------------
# Scanner tests
# ---------------------------------------------------------------------------


class TestTranscriptScanner:
    def test_iter_events_skips_missing_root(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        assert list(iter_events(missing)) == []

    def test_iter_events_parses_tool_uses(self, tmp_path):
        _write_session(
            tmp_path,
            "-root-test",
            "sess1",
            [
                _assistant_event(
                    "2026-05-19T12:00:00Z", "Read", {"file_path": "/root/foo.py"}
                ),
                _assistant_event(
                    "2026-05-19T12:00:05Z",
                    "Bash",
                    {"command": "grep -rn foo /root/foo.py"},
                ),
            ],
        )
        evs = list(iter_events(tmp_path))
        assert len(evs) == 2
        assert evs[0].tool_name == "Read"
        assert evs[0].args["file_path"] == "/root/foo.py"
        assert evs[1].tool_name == "Bash"
        assert evs[1].args["command"].startswith("grep")
        assert evs[0].session_id == "sess1"
        assert evs[0].project == "-root-test"

    def test_iter_events_filters_by_project(self, tmp_path):
        _write_session(
            tmp_path,
            "-root-a",
            "s",
            [_assistant_event("2026-05-19T12:00:00Z", "Read", {"file_path": "/a.py"})],
        )
        _write_session(
            tmp_path,
            "-root-b",
            "s",
            [_assistant_event("2026-05-19T12:00:00Z", "Read", {"file_path": "/b.py"})],
        )
        evs = list(iter_events(tmp_path, project="root-a"))
        assert len(evs) == 1
        assert evs[0].args["file_path"] == "/a.py"

    def test_iter_events_filters_by_since(self, tmp_path):
        _write_session(
            tmp_path,
            "-root-test",
            "s",
            [
                _assistant_event(
                    "2026-01-01T00:00:00Z", "Read", {"file_path": "/old.py"}
                ),
                _assistant_event(
                    "2026-05-19T12:00:00Z", "Read", {"file_path": "/new.py"}
                ),
            ],
        )
        # mtime is recent (file just written) so per-file filter passes; per-event filter
        # should drop the old one.
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        evs = list(iter_events(tmp_path, since=since))
        assert len(evs) == 1
        assert evs[0].args["file_path"] == "/new.py"

    def test_iter_events_skips_non_assistant(self, tmp_path):
        _write_session(
            tmp_path,
            "-root-test",
            "s",
            [
                {"type": "user", "timestamp": "2026-05-19T12:00:00Z"},
                {"type": "queue-operation"},
                _assistant_event(
                    "2026-05-19T12:00:00Z", "Read", {"file_path": "/a.py"}
                ),
            ],
        )
        evs = list(iter_events(tmp_path))
        assert len(evs) == 1

    def test_iter_events_handles_malformed_lines(self, tmp_path):
        pdir = tmp_path / "-root-test"
        pdir.mkdir()
        path = pdir / "bad.jsonl"
        path.write_text(
            "not json\n"
            + json.dumps(_assistant_event("2026-05-19T12:00:00Z", "Read", {"file_path": "/a.py"}))
            + "\n",
            encoding="utf-8",
        )
        evs = list(iter_events(tmp_path))
        assert len(evs) == 1

    def test_pruned_args_only_keep_keys(self, tmp_path):
        _write_session(
            tmp_path,
            "-root-test",
            "s",
            [
                _assistant_event(
                    "2026-05-19T12:00:00Z",
                    "Read",
                    {"file_path": "/a.py", "secret_prompt": "PRIVATE"},
                ),
            ],
        )
        evs = list(iter_events(tmp_path))
        assert evs[0].args == {"file_path": "/a.py"}
        assert "secret_prompt" not in evs[0].args


# ---------------------------------------------------------------------------
# Pattern detector tests
# ---------------------------------------------------------------------------


class TestReadGrepReadPattern:
    def test_detects_chain(self):
        evs = [
            _ev(0, "Read", {"file_path": "/root/p/a.py"}),
            _ev(10, "Grep", {"pattern": "foo"}),
            _ev(30, "Read", {"file_path": "/root/p/b.py"}),
        ]
        out = list(ReadGrepReadPattern().detect(evs))
        assert len(out) == 1
        assert out[0].pattern == "read_grep_read_chain"
        assert "get_full_context" in out[0].replacement

    def test_skips_when_outside_window(self):
        evs = [
            _ev(0, "Read", {"file_path": "/root/p/a.py"}),
            _ev(10, "Grep", {"pattern": "foo"}),
            _ev(300, "Read", {"file_path": "/root/p/b.py"}),
        ]
        assert list(ReadGrepReadPattern().detect(evs)) == []

    def test_skips_different_projects(self):
        evs = [
            _ev(0, "Read", {"file_path": "/root/a/x.py"}),
            _ev(10, "Grep", {"pattern": "foo"}),
            _ev(30, "Read", {"file_path": "/root/b/x.py"}),
        ]
        assert list(ReadGrepReadPattern().detect(evs)) == []


class TestBatchFindSymbolPattern:
    def test_detects_3_in_a_row(self):
        evs = [
            _ev(0, "mcp__token-savior__find_symbol", {"name": "f1"}),
            _ev(5, "mcp__token-savior__find_symbol", {"name": "f2"}),
            _ev(10, "mcp__token-savior__find_symbol", {"name": "f3"}),
        ]
        out = list(BatchFindSymbolPattern().detect(evs))
        assert len(out) == 1
        assert "batch" in out[0].replacement.lower()

    def test_skips_already_batched(self):
        evs = [
            _ev(0, "find_symbol", {"names": ["a", "b"]}),
            _ev(5, "find_symbol", {"names": ["c"]}),
            _ev(10, "find_symbol", {"names": ["d"]}),
        ]
        assert list(BatchFindSymbolPattern().detect(evs)) == []

    def test_skips_only_two(self):
        evs = [
            _ev(0, "find_symbol", {"name": "f1"}),
            _ev(5, "find_symbol", {"name": "f2"}),
        ]
        assert list(BatchFindSymbolPattern().detect(evs)) == []


class TestEditWithoutContextPattern:
    def test_detects_gfs_then_edit(self):
        evs = [
            _ev(0, "mcp__token-savior__get_function_source", {"name": "foo"}),
            _ev(10, "Edit", {"file_path": "/root/p/a.py"}),
        ]
        out = list(EditWithoutContextPattern().detect(evs))
        assert len(out) == 1
        assert "get_edit_context" in out[0].replacement

    def test_skipped_when_full_context_first(self):
        evs = [
            _ev(0, "mcp__token-savior__get_full_context", {"name": "foo"}),
            _ev(5, "mcp__token-savior__get_function_source", {"name": "foo"}),
            _ev(10, "Edit", {"file_path": "/root/p/a.py"}),
        ]
        assert list(EditWithoutContextPattern().detect(evs)) == []


class TestMemorySearchWithoutIndexPattern:
    def test_detects_naked_search(self):
        evs = [
            _ev(0, "memory_search", {"query": "foo"}),
        ]
        out = list(MemorySearchWithoutIndexPattern().detect(evs))
        assert len(out) == 1

    def test_skipped_when_indexed_first(self):
        evs = [
            _ev(0, "memory_index", {"query": "foo"}),
            _ev(5, "memory_search", {"query": "foo"}),
        ]
        assert list(MemorySearchWithoutIndexPattern().detect(evs)) == []


class TestNativeShellOnCodePattern:
    def test_detects_grep_on_py(self):
        evs = [_ev(0, "Bash", {"command": "grep -rn foo /root/a.py"})]
        out = list(NativeShellOnCodePattern().detect(evs))
        assert len(out) == 1
        assert "search_codebase" in out[0].replacement

    def test_detects_cat_on_ts(self):
        evs = [_ev(0, "Bash", {"command": "cat /root/a.ts | head"})]
        out = list(NativeShellOnCodePattern().detect(evs))
        assert len(out) == 1

    def test_skips_non_code_file(self):
        evs = [_ev(0, "Bash", {"command": "cat /root/foo.txt"})]
        assert list(NativeShellOnCodePattern().detect(evs)) == []

    def test_skips_other_verbs(self):
        evs = [_ev(0, "Bash", {"command": "ls /root/a.py"})]
        assert list(NativeShellOnCodePattern().detect(evs)) == []

    def test_strips_cd_prefix(self):
        evs = [_ev(0, "Bash", {"command": "cd /root && grep -rn foo /root/a.py"})]
        out = list(NativeShellOnCodePattern().detect(evs))
        assert len(out) == 1


# ---------------------------------------------------------------------------
# End-to-end + MCP handler
# ---------------------------------------------------------------------------


class TestDiscoverEndToEnd:
    def test_e2e_aggregates_across_sessions(self, tmp_path):
        _write_session(
            tmp_path,
            "-root-x",
            "s1",
            [
                _assistant_event("2026-05-19T12:00:00Z", "Read", {"file_path": "/root/p/a.py"}),
                _assistant_event("2026-05-19T12:00:05Z", "Grep", {"pattern": "foo"}),
                _assistant_event("2026-05-19T12:00:10Z", "Read", {"file_path": "/root/p/b.py"}),
                _assistant_event(
                    "2026-05-19T12:01:00Z",
                    "Bash",
                    {"command": "grep -rn foo /root/p/a.py"},
                ),
            ],
        )
        out = discover(since_days=365, root=tmp_path)
        names = {f.pattern for f in out}
        assert "read_grep_read_chain" in names
        assert "native_shell_on_code" in names

    def test_e2e_filters_by_since(self, tmp_path):
        _write_session(
            tmp_path,
            "-root-x",
            "s1",
            [
                _assistant_event("2026-01-01T00:00:00Z", "Read", {"file_path": "/root/p/a.py"}),
                _assistant_event("2026-01-01T00:00:05Z", "Grep", {"pattern": "foo"}),
                _assistant_event("2026-01-01T00:00:10Z", "Read", {"file_path": "/root/p/b.py"}),
            ],
        )
        # Setting since_days=1 against a far-past fixture: per-event filter keeps
        # nothing once now()-1d is applied, so we expect no findings.
        out = discover(since_days=1, root=tmp_path)
        assert out == []

    def test_handler_dispatches_through_meta(self, tmp_path, monkeypatch):
        # Build a fixture and patch transcript_root to point at it.
        _write_session(
            tmp_path,
            "-root-x",
            "s1",
            [
                _assistant_event("2026-05-19T12:00:00Z", "Read", {"file_path": "/root/p/a.py"}),
                _assistant_event("2026-05-19T12:00:05Z", "Grep", {"pattern": "foo"}),
                _assistant_event("2026-05-19T12:00:10Z", "Read", {"file_path": "/root/p/b.py"}),
            ],
        )
        from token_savior.discover import transcript_scanner as ts_mod
        from token_savior import discover as discover_pkg

        monkeypatch.setattr(ts_mod, "transcript_root", lambda: tmp_path)
        monkeypatch.setattr(discover_pkg, "transcript_root", lambda: tmp_path)

        # Smoke-test via the dispatch dict.
        from token_savior.server_handlers import META_HANDLERS

        assert "ts_discover" in META_HANDLERS
        result = META_HANDLERS["ts_discover"]({"since_days": 365, "format": "table"})
        assert len(result) == 1
        text = result[0].text
        assert "ts_discover" in text or "read_grep_read_chain" in text

    def test_handler_json_output(self, tmp_path, monkeypatch):
        _write_session(
            tmp_path,
            "-root-x",
            "s1",
            [
                _assistant_event("2026-05-19T12:00:00Z", "Read", {"file_path": "/root/p/a.py"}),
                _assistant_event("2026-05-19T12:00:05Z", "Grep", {"pattern": "foo"}),
                _assistant_event("2026-05-19T12:00:10Z", "Read", {"file_path": "/root/p/b.py"}),
            ],
        )
        from token_savior.discover import transcript_scanner as ts_mod
        from token_savior import discover as discover_pkg

        monkeypatch.setattr(ts_mod, "transcript_root", lambda: tmp_path)
        monkeypatch.setattr(discover_pkg, "transcript_root", lambda: tmp_path)

        from token_savior.server_handlers import META_HANDLERS

        result = META_HANDLERS["ts_discover"](
            {"since_days": 365, "format": "json"}
        )
        payload = json.loads(result[0].text)
        assert "findings" in payload
        assert payload["since_days"] == 365


class TestSchemaRegistered:
    def test_tool_schema_registered(self):
        from token_savior.tool_schemas import TOOL_SCHEMAS

        assert "ts_discover" in TOOL_SCHEMAS
        schema = TOOL_SCHEMAS["ts_discover"]
        assert "since_days" in schema["inputSchema"]["properties"]
        assert "format" in schema["inputSchema"]["properties"]
