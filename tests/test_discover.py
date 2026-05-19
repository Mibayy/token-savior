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

    def test_schema_exposes_adoption_formats(self):
        from token_savior.tool_schemas import TOOL_SCHEMAS

        fmt = TOOL_SCHEMAS["ts_discover"]["inputSchema"]["properties"]["format"]
        assert set(fmt["enum"]) == {"table", "json", "adoption", "adoption_json"}

    def test_schema_project_doc_reflects_all_default(self):
        from token_savior.tool_schemas import TOOL_SCHEMAS

        desc = TOOL_SCHEMAS["ts_discover"]["inputSchema"]["properties"]["project"][
            "description"
        ].lower()
        # No more "active project default" wording; advertise "all" semantics.
        assert "all" in desc
        assert "active project" not in desc


# ---------------------------------------------------------------------------
# F4 cross-project + adoption mode
# ---------------------------------------------------------------------------


def _session_event(
    ts: str,
    tool_name: str,
    tool_input: dict,
    session_id: str = "test-session-1",
    cwd: str = "/root/test-project",
) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": session_id,
        "cwd": cwd,
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


class TestCrossProjectScan:
    def test_scans_all_project_dirs_when_project_is_none(self, tmp_path):
        # Two distinct project dirs, each producing the same chain pattern.
        _write_session(
            tmp_path,
            "-root-alpha",
            "sA",
            [
                _session_event("2026-05-19T12:00:00Z", "Read", {"file_path": "/root/p/a.py"}),
                _session_event("2026-05-19T12:00:05Z", "Grep", {"pattern": "foo"}),
                _session_event("2026-05-19T12:00:10Z", "Read", {"file_path": "/root/p/b.py"}),
            ],
        )
        _write_session(
            tmp_path,
            "-root-beta",
            "sB",
            [
                _session_event("2026-05-19T13:00:00Z", "Read", {"file_path": "/root/p/c.py"}),
                _session_event("2026-05-19T13:00:05Z", "Grep", {"pattern": "bar"}),
                _session_event("2026-05-19T13:00:10Z", "Read", {"file_path": "/root/p/d.py"}),
            ],
        )

        out = discover(since_days=365, project=None, root=tmp_path)
        patterns = {f.pattern: f for f in out}
        assert "read_grep_read_chain" in patterns
        rgrr = patterns["read_grep_read_chain"]
        # One chain per project, aggregated.
        assert rgrr.count == 2
        # top_projects must include BOTH project dirs.
        assert set(rgrr.top_projects.keys()) == {"-root-alpha", "-root-beta"}
        assert rgrr.top_projects["-root-alpha"] == 1
        assert rgrr.top_projects["-root-beta"] == 1

    def test_top_projects_counts_hits_per_project(self, tmp_path):
        # Project alpha gets 2 chains, beta gets 1.
        _write_session(
            tmp_path,
            "-root-alpha",
            "sA1",
            [
                _session_event("2026-05-19T12:00:00Z", "Read", {"file_path": "/root/p/a.py"}),
                _session_event("2026-05-19T12:00:05Z", "Grep", {"pattern": "foo"}),
                _session_event("2026-05-19T12:00:10Z", "Read", {"file_path": "/root/p/b.py"}),
            ],
        )
        _write_session(
            tmp_path,
            "-root-alpha",
            "sA2",
            [
                _session_event("2026-05-19T14:00:00Z", "Read", {"file_path": "/root/p/x.py"}),
                _session_event("2026-05-19T14:00:05Z", "Grep", {"pattern": "x"}),
                _session_event("2026-05-19T14:00:10Z", "Read", {"file_path": "/root/p/y.py"}),
            ],
        )
        _write_session(
            tmp_path,
            "-root-beta",
            "sB1",
            [
                _session_event("2026-05-19T15:00:00Z", "Read", {"file_path": "/root/p/m.py"}),
                _session_event("2026-05-19T15:00:05Z", "Grep", {"pattern": "m"}),
                _session_event("2026-05-19T15:00:10Z", "Read", {"file_path": "/root/p/n.py"}),
            ],
        )

        out = discover(since_days=365, project=None, root=tmp_path)
        rgrr = next(f for f in out if f.pattern == "read_grep_read_chain")
        assert rgrr.count == 3
        assert rgrr.top_projects["-root-alpha"] == 2
        assert rgrr.top_projects["-root-beta"] == 1

    def test_substring_filter_still_works(self, tmp_path):
        _write_session(
            tmp_path,
            "-root-alpha",
            "sA",
            [
                _session_event("2026-05-19T12:00:00Z", "Read", {"file_path": "/root/p/a.py"}),
                _session_event("2026-05-19T12:00:05Z", "Grep", {"pattern": "foo"}),
                _session_event("2026-05-19T12:00:10Z", "Read", {"file_path": "/root/p/b.py"}),
            ],
        )
        _write_session(
            tmp_path,
            "-root-beta",
            "sB",
            [
                _session_event("2026-05-19T13:00:00Z", "Read", {"file_path": "/root/p/c.py"}),
                _session_event("2026-05-19T13:00:05Z", "Grep", {"pattern": "bar"}),
                _session_event("2026-05-19T13:00:10Z", "Read", {"file_path": "/root/p/d.py"}),
            ],
        )

        out = discover(since_days=365, project="alpha", root=tmp_path)
        rgrr = next(f for f in out if f.pattern == "read_grep_read_chain")
        assert rgrr.count == 1
        assert "-root-alpha" in rgrr.top_projects
        assert "-root-beta" not in rgrr.top_projects


class TestComputeAdoption:
    def test_ratios_on_synthetic_session(self):
        from token_savior.discover.patterns import compute_adoption

        evs = [
            _ev(0, "Read", {"file_path": "/a.py"}, session="s1"),
            _ev(1, "Grep", {"pattern": "x"}, session="s1"),
            _ev(2, "mcp__token-savior__find_symbol", {"name": "foo"}, session="s1"),
            _ev(3, "mcp__token-savior__get_function_source", {"name": "foo"}, session="s1"),
        ]
        report = compute_adoption({"s1": evs})
        assert report.total_ts == 2
        assert report.total_native == 2
        assert report.total_relevant == 4
        assert abs(report.ts_ratio - 0.5) < 1e-9
        assert abs(report.native_ratio - 0.5) < 1e-9
        assert len(report.sessions) == 1
        assert report.sessions[0].session_id == "s1"

    def test_other_calls_excluded_from_ratio(self):
        from token_savior.discover.patterns import compute_adoption

        evs = [
            _ev(0, "Read", {"file_path": "/a.py"}, session="s1"),
            _ev(1, "TodoWrite", {"todos": []}, session="s1"),
            _ev(2, "mcp__some-other-server__do_thing", {}, session="s1"),
        ]
        report = compute_adoption({"s1": evs})
        # Only Read counts toward TS vs native; the other two are 'other'.
        assert report.total_ts == 0
        assert report.total_native == 1
        assert report.total_other == 2
        assert report.ts_ratio == 0.0

    def test_trend_split_first_vs_second_half(self):
        from token_savior.discover.patterns import compute_adoption

        # First-half: 2 native, 0 TS. Second-half: 0 native, 2 TS.
        # Median of [0,1,2,3] is index 2 (timestamp 2), so events with ts<2
        # land in the first half and ts>=2 in the second half.
        evs = [
            _ev(0, "Read", {"file_path": "/a.py"}, session="s1"),
            _ev(1, "Grep", {"pattern": "x"}, session="s1"),
            _ev(2, "mcp__token-savior__find_symbol", {"name": "f"}, session="s1"),
            _ev(3, "mcp__token-savior__get_function_source", {"name": "f"}, session="s1"),
        ]
        report = compute_adoption({"s1": evs})
        # First half should be native-heavy, second half TS-heavy → positive trend.
        assert report.trend_delta > 0.5

    def test_worst_sessions_ordered_by_ratio(self):
        from token_savior.discover.patterns import compute_adoption

        # s1: 100% native (worst). s2: 50/50. s3: 100% TS.
        sessions = {
            "s1": [
                _ev(0, "Read", {"file_path": "/a.py"}, session="s1"),
                _ev(1, "Grep", {"pattern": "x"}, session="s1"),
            ],
            "s2": [
                _ev(0, "Read", {"file_path": "/b.py"}, session="s2"),
                _ev(1, "mcp__token-savior__find_symbol", {"name": "g"}, session="s2"),
            ],
            "s3": [
                _ev(0, "mcp__token-savior__find_symbol", {"name": "h"}, session="s3"),
                _ev(1, "mcp__token-savior__get_function_source", {"name": "h"}, session="s3"),
            ],
        }
        report = compute_adoption(sessions)
        worst = report.worst_sessions(2)
        assert worst[0].session_id == "s1"
        assert worst[1].session_id == "s2"


class TestAdoptionHandler:
    def _setup_fixture(self, tmp_path, monkeypatch):
        # Mixed session: 1 TS call + 2 native calls.
        _write_session(
            tmp_path,
            "-root-x",
            "s1",
            [
                _session_event("2026-05-19T12:00:00Z", "Read", {"file_path": "/root/p/a.py"}),
                _session_event("2026-05-19T12:00:05Z", "Grep", {"pattern": "foo"}),
                _session_event(
                    "2026-05-19T12:00:10Z",
                    "mcp__token-savior__find_symbol",
                    {"name": "foo"},
                ),
            ],
        )
        from token_savior.discover import transcript_scanner as ts_mod
        from token_savior import discover as discover_pkg

        monkeypatch.setattr(ts_mod, "transcript_root", lambda: tmp_path)
        monkeypatch.setattr(discover_pkg, "transcript_root", lambda: tmp_path)

    def test_adoption_table(self, tmp_path, monkeypatch):
        self._setup_fixture(tmp_path, monkeypatch)
        from token_savior.server_handlers import META_HANDLERS

        result = META_HANDLERS["ts_discover"](
            {"since_days": 365, "format": "adoption"}
        )
        assert len(result) == 1
        text = result[0].text
        assert "Token Savior adoption" in text or "adoption" in text.lower()
        assert "TS" in text
        assert "native" in text.lower()

    def test_adoption_json(self, tmp_path, monkeypatch):
        self._setup_fixture(tmp_path, monkeypatch)
        from token_savior.server_handlers import META_HANDLERS

        result = META_HANDLERS["ts_discover"](
            {"since_days": 365, "format": "adoption_json"}
        )
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "adoption" in payload
        adoption = payload["adoption"]
        assert adoption["total_ts"] == 1
        assert adoption["total_native"] == 2
        # 1 TS / 3 relevant = 0.3333
        assert abs(adoption["ts_ratio"] - 1 / 3) < 1e-3

    def test_backward_compat_empty_arguments(self, tmp_path, monkeypatch):
        # Existing callers passing {} (no format, no project) must still work.
        from token_savior.discover import transcript_scanner as ts_mod
        from token_savior import discover as discover_pkg

        monkeypatch.setattr(ts_mod, "transcript_root", lambda: tmp_path)
        monkeypatch.setattr(discover_pkg, "transcript_root", lambda: tmp_path)

        from token_savior.server_handlers import META_HANDLERS

        result = META_HANDLERS["ts_discover"]({})
        assert len(result) == 1
        # Empty transcript root → "no findings" string from _fmt_table.
        assert isinstance(result[0].text, str)
