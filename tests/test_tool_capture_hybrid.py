"""Tests for the hybrid sandbox+compact mode in tool_capture_hook (F2-hybrid).

The hook prints a single JSON object to stdout. We drive ``main()`` directly,
feed a fake PostToolUse event on stdin, and stub the sandbox so no SQLite
file is touched.

Decision tree under test:
  * TS_BASH_COMPACT=0 (or unset)       -> no compaction, sandbox iff above THRESHOLD
  * compact match + tiny  result       -> compact only, NO sandbox
  * compact match + small result       -> compact only, NO sandbox (legacy)
  * compact match + large result       -> compact preview + ts://capture/N ref
  * no compactor match (Bash)          -> sandbox-only path (unchanged)
"""
from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make the hook script importable as a module
HOOK_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOK_DIR))

import tool_capture_hook  # noqa: E402


@pytest.fixture
def stub_sandbox(monkeypatch):
    """Capture all capture_put calls without touching SQLite."""
    calls: list[dict] = []

    def fake_put(*, tool_name, output, args_summary=None,
                 session_id=None, project_root=None, meta=None):
        calls.append({
            "tool_name": tool_name,
            "output": output,
            "args_summary": args_summary,
            "session_id": session_id,
            "project_root": project_root,
            "meta": meta or {},
        })
        cid = len(calls)
        return {
            "id": cid,
            "uri": f"ts://capture/{cid}",
            "preview": output[:200],
            "bytes": len(output),
            "lines": output.count("\n") + 1,
        }

    fake_module = SimpleNamespace(capture_put=fake_put)
    # Force-import the submodule so `from token_savior.memory import tool_capture`
    # resolves to our stub. Patch both the package attr AND sys.modules so
    # cached resolutions see the same stub.
    import token_savior.memory as mem
    import token_savior.memory.tool_capture  # noqa: F401  (ensure registered)
    monkeypatch.setattr(mem, "tool_capture", fake_module, raising=False)
    monkeypatch.setitem(sys.modules, "token_savior.memory.tool_capture", fake_module)
    return calls


def _run_hook(event: dict, monkeypatch, env: dict | None = None) -> dict:
    """Drive the hook end-to-end, returning the parsed JSON it prints."""
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    # Reload to re-evaluate module-level THRESHOLD constants under the env
    importlib.reload(tool_capture_hook)
    tool_capture_hook.main()
    out = buf.getvalue().strip()
    assert out, "hook produced no stdout"
    return json.loads(out)


def _bash_event(command: str, stdout: str, stderr: str = "") -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": stderr},
        "session_id": "sess-1",
        "cwd": "/tmp/proj",
    }


# ---------------------------------------------------------------------------
# 1. Small compact result -> compact only, no sandbox
# ---------------------------------------------------------------------------


def test_small_compact_emits_inline_no_sandbox(stub_sandbox, monkeypatch):
    """A small compact rendering should be inlined; sandbox not touched."""
    # Produce a recognized command (git status) with smallish output so the
    # compactor matches but the compact result stays well under inline_threshold.
    stdout = "On branch main\n" + ("\tmodified:   src/foo.py\n" * 20)
    event = _bash_event("git status", stdout)
    out = _run_hook(event, monkeypatch, env={"TS_BASH_COMPACT": "1"})

    note = out["hookSpecificOutput"]["additionalContext"]
    assert "[token-savior:compact]" in note
    assert "ts://capture/" not in note
    assert stub_sandbox == []  # no sandbox call


# ---------------------------------------------------------------------------
# 2. Large compact result -> compact preview + ts://capture/N
# ---------------------------------------------------------------------------


def test_large_compact_sandboxes_full_output(stub_sandbox, monkeypatch):
    """If compact result > inline_threshold, sandbox the full ORIGINAL output."""
    # Use pytest with many distinct failures — the compactor lists each one,
    # so the compact rendering itself stays large (>4K). Original output is
    # ~the same order, but every byte is preserved in the sandbox.
    failures = "\n".join(
        f"FAILED tests/test_mod_{i}.py::test_case_{i} - AssertionError: expected {i}"
        for i in range(300)
    )
    stdout = failures + "\n=== 300 failed in 12.3s ==="
    event = _bash_event("pytest -q", stdout)
    out = _run_hook(
        event, monkeypatch,
        env={
            "TS_BASH_COMPACT": "1",
            "TS_COMPACT_INLINE_THRESHOLD": "1024",  # force hybrid trigger
            "TS_COMPACT_TINY_THRESHOLD": "256",
        },
    )

    note = out["hookSpecificOutput"]["additionalContext"]
    assert "[token-savior:compact]" in note
    assert "ts://capture/1" in note
    assert "use capture_get to retrieve" in note
    assert len(stub_sandbox) == 1
    # The sandboxed payload is the full original (stdout + stderr concat).
    assert "test_case_299" in stub_sandbox[0]["output"]
    assert "test_case_0" in stub_sandbox[0]["output"]
    assert stub_sandbox[0]["meta"].get("mode") == "hybrid"


# ---------------------------------------------------------------------------
# 3. Tiny compact result -> compact only, no sandbox (even if forced low thr)
# ---------------------------------------------------------------------------


def test_tiny_compact_skips_sandbox(stub_sandbox, monkeypatch):
    """Below tiny_threshold the hybrid path must never sandbox."""
    # A very short git status — compact output a few dozen bytes at most.
    stdout = "On branch main\nnothing to commit, working tree clean\n"
    event = _bash_event("git status", stdout)
    out = _run_hook(
        event, monkeypatch,
        env={
            "TS_BASH_COMPACT": "1",
            # Aggressive thresholds: even if inline=0, tiny=large keeps it inline.
            "TS_COMPACT_INLINE_THRESHOLD": "0",
            "TS_COMPACT_TINY_THRESHOLD": "100000",
        },
    )

    note = out["hookSpecificOutput"]["additionalContext"]
    assert "[token-savior:compact]" in note
    assert "ts://capture/" not in note
    assert stub_sandbox == []


# ---------------------------------------------------------------------------
# 4. No compactor match -> sandbox path unchanged
# ---------------------------------------------------------------------------


def test_no_compactor_match_uses_sandbox_path(stub_sandbox, monkeypatch):
    """An unknown command above THRESHOLD should still get sandbox-only."""
    stdout = "x" * 6000  # > default THRESHOLD (4096)
    event = _bash_event("some-unknown-tool --weird", stdout)
    out = _run_hook(
        event, monkeypatch,
        env={"TS_BASH_COMPACT": "1"},
    )

    note = out["hookSpecificOutput"]["additionalContext"]
    # Legacy sandbox path emits "[token-savior:capture]" (note the prefix).
    assert "[token-savior:capture]" in note
    assert "ts://capture/1" in note
    assert "[token-savior:compact]" not in note
    assert len(stub_sandbox) == 1
    assert stub_sandbox[0]["meta"].get("hook") == "PostToolUse"
    assert stub_sandbox[0]["meta"].get("mode") != "hybrid"


# ---------------------------------------------------------------------------
# 5. TS_BASH_COMPACT=0 -> compaction disabled, legacy behavior intact
# ---------------------------------------------------------------------------


def test_compact_disabled_preserves_legacy_behavior(stub_sandbox, monkeypatch):
    """When TS_BASH_COMPACT is off, even a recognized command falls through
    to the sandbox path (if large) or pass-through (if small)."""
    # Recognized command (git status) but compaction OFF.
    files = "\n".join(f"\tmodified:   src/file_{i:04d}.py" for i in range(300))
    stdout = "On branch main\n" + files + "\n"
    event = _bash_event("git status", stdout)
    # Don't set TS_BASH_COMPACT at all.
    monkeypatch.delenv("TS_BASH_COMPACT", raising=False)
    out = _run_hook(event, monkeypatch, env={})

    note = out["hookSpecificOutput"]["additionalContext"]
    # No compact prefix -- legacy sandbox-only path.
    assert "[token-savior:compact]" not in note
    assert "[token-savior:capture]" in note
    assert "ts://capture/1" in note
    assert len(stub_sandbox) == 1
    assert stub_sandbox[0]["meta"].get("mode") != "hybrid"


# ---------------------------------------------------------------------------
# 6. Bonus: CompactResult carries original_text (dataclass contract)
# ---------------------------------------------------------------------------


def test_compact_result_carries_original_text():
    """The hybrid path relies on result.original_text being populated."""
    from token_savior.compactors import compact

    stdout = "On branch main\n" + ("\tmodified:   src/foo.py\n" * 10)
    r = compact("git status", stdout)
    assert r is not None
    assert r.original_text == stdout  # stderr empty
    assert r.original_bytes == len(stdout.encode("utf-8"))
