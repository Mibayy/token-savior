"""Unit tests for the `ts init` subcommand (F3)."""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from token_savior.cli_init import (
    _apply_bundles,
    _backup_path,
    _detect_agent,
    _load_hook_bundles,
    _read_settings,
    run,
)
from token_savior.cli_init.agent_paths import (
    SUPPORTED_AGENTS,
    hook_config_paths,
    settings_path,
)
from token_savior.cli_init.merger import (
    added_entries,
    merge_hook_arrays,
    merge_hook_config,
)


# Resolve repo root from the test file location -> .../ts-f3cli/
REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Pure-function merge tests                                                   #
# --------------------------------------------------------------------------- #
def _sample_claude_bundle() -> dict:
    return {
        "_comment": "test bundle",
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Bash|Read",
                    "hooks": [
                        {"type": "command", "command": "/bin/true", "timeout": 1000}
                    ],
                }
            ]
        },
    }


def test_merge_into_empty_settings_creates_hooks_key() -> None:
    out = merge_hook_config({}, _sample_claude_bundle())
    assert "hooks" in out
    assert "PostToolUse" in out["hooks"]
    assert len(out["hooks"]["PostToolUse"]) == 1


def test_merge_preserves_existing_unrelated_keys() -> None:
    existing = {
        "theme": "dark",
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Other",
                    "hooks": [{"type": "command", "command": "/bin/other"}],
                }
            ],
            "OtherEvent": [{"foo": "bar"}],
        },
    }
    out = merge_hook_config(existing, _sample_claude_bundle())
    # Unrelated keys preserved.
    assert out["theme"] == "dark"
    assert out["hooks"]["OtherEvent"] == [{"foo": "bar"}]
    # PostToolUse array grew: existing + new.
    assert len(out["hooks"]["PostToolUse"]) == 2
    matchers = {e["matcher"] for e in out["hooks"]["PostToolUse"]}
    assert matchers == {"Other", "Bash|Read"}


def test_merge_is_idempotent_no_duplicates() -> None:
    bundle = _sample_claude_bundle()
    once = merge_hook_config({}, bundle)
    twice = merge_hook_config(once, bundle)
    assert once == twice
    assert len(twice["hooks"]["PostToolUse"]) == 1


def test_merge_hook_arrays_dedup_by_fingerprint() -> None:
    a = [
        {"matcher": "X", "hooks": [{"type": "command", "command": "/c1"}]},
    ]
    b = [
        {"matcher": "X", "hooks": [{"type": "command", "command": "/c1"}]},  # dup
        {"matcher": "Y", "hooks": [{"type": "command", "command": "/c2"}]},  # new
    ]
    out = merge_hook_arrays(a, b)
    assert len(out) == 2
    assert {e["matcher"] for e in out} == {"X", "Y"}


def test_merge_accepts_codex_inline_command_shape() -> None:
    codex = {
        "hooks": {
            "tool_complete": [
                {
                    "matcher": "shell|read_file",
                    "command": "/usr/bin/python3 /tmp/x.py",
                    "timeout_ms": 5000,
                }
            ]
        }
    }
    out = merge_hook_config({}, codex)
    assert out["hooks"]["tool_complete"][0]["command"].startswith("/usr/bin/python3")
    # idempotent
    out2 = merge_hook_config(out, codex)
    assert out == out2


def test_added_entries_lists_only_new() -> None:
    before = {"hooks": {"PostToolUse": [
        {"matcher": "Old", "hooks": [{"type": "command", "command": "/old"}]},
    ]}}
    after = merge_hook_config(before, _sample_claude_bundle())
    added = added_entries(before, after)
    assert len(added) == 1
    event, (matcher, cmd) = added[0]
    assert event == "PostToolUse"
    assert matcher == "Bash|Read"
    assert cmd == "/bin/true"


# --------------------------------------------------------------------------- #
# agent_paths                                                                 #
# --------------------------------------------------------------------------- #
def test_settings_path_global_per_agent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    expected = {
        "claude": home / ".claude" / "settings.json",
        "cursor": home / ".cursor" / "settings.json",
        "gemini": home / ".gemini" / "settings.json",
        "codex": home / ".codex" / "settings.json",
    }
    for agent, path in expected.items():
        assert settings_path(agent, "global", home=home) == path


def test_settings_path_unsupported_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        settings_path("emacs", "global", home=tmp_path)


def test_hook_config_paths_claude_has_both() -> None:
    paths = hook_config_paths("claude", REPO_ROOT)
    names = {p.name for p in paths}
    assert "tool-capture-hooks-config.json" in names
    assert "bash-rewriter-config.json" in names


def test_load_hook_bundles_claude_real_repo() -> None:
    bundles = _load_hook_bundles("claude", REPO_ROOT)
    # tool-capture + bash-rewriter
    assert len(bundles) == 2
    # All have an inner "hooks" dict.
    for b in bundles:
        assert isinstance(b.get("hooks"), dict)


def test_apply_bundles_combines_post_and_pre() -> None:
    bundles = _load_hook_bundles("claude", REPO_ROOT)
    merged = _apply_bundles({}, bundles)
    assert "PostToolUse" in merged["hooks"]
    assert "PreToolUse" in merged["hooks"]


# --------------------------------------------------------------------------- #
# Helpers (read / backup / detect)                                            #
# --------------------------------------------------------------------------- #
def test_read_settings_missing_returns_empty(tmp_path: Path) -> None:
    assert _read_settings(tmp_path / "nope.json") == {}


def test_read_settings_invalid_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text("{not json")
    with pytest.raises(RuntimeError):
        _read_settings(p)


def test_backup_path_format(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    now = _dt.datetime(2026, 5, 19, 13, 45, 7, tzinfo=_dt.timezone.utc)
    bak = _backup_path(target, now=now)
    assert bak.name == "settings.json.bak-20260519-134507"


def test_detect_agent_finds_first_existing(tmp_path: Path) -> None:
    home = tmp_path
    # Create only ~/.cursor/settings.json
    (home / ".cursor").mkdir()
    (home / ".cursor" / "settings.json").write_text("{}")
    assert _detect_agent(home) == "cursor"


def test_detect_agent_none_when_nothing(tmp_path: Path) -> None:
    assert _detect_agent(tmp_path) is None


# --------------------------------------------------------------------------- #
# End-to-end `run()` tests -- never touch the real ~/.                        #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fake_home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _argv(agent: str, home: Path, *extra: str) -> list[str]:
    return [
        "--agent", agent,
        "--home", str(home),
        "--ts-root", str(REPO_ROOT),
        *extra,
    ]


def test_run_dry_run_does_not_write(fake_home: Path, capsys) -> None:
    rc = run(_argv("claude", fake_home, "--dry-run"))
    assert rc == 0
    target = fake_home / ".claude" / "settings.json"
    assert not target.exists()
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "PostToolUse" in out


def test_run_yes_writes_file_and_creates_no_backup_for_new(fake_home: Path, capsys) -> None:
    rc = run(_argv("claude", fake_home, "--yes"))
    assert rc == 0
    target = fake_home / ".claude" / "settings.json"
    assert target.exists()
    data = json.loads(target.read_text())
    assert "PostToolUse" in data["hooks"]
    assert "PreToolUse" in data["hooks"]
    # No backup expected (file didn't exist before).
    backups = list(target.parent.glob("settings.json.bak-*"))
    assert backups == []


def test_run_yes_backs_up_existing(fake_home: Path) -> None:
    target = fake_home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"theme": "dark"}))
    rc = run(_argv("claude", fake_home, "--yes"))
    assert rc == 0
    backups = list(target.parent.glob("settings.json.bak-*"))
    assert len(backups) == 1
    # Backup contains the *original* content.
    assert json.loads(backups[0].read_text()) == {"theme": "dark"}
    # Final settings preserve theme and have hooks merged in.
    final = json.loads(target.read_text())
    assert final["theme"] == "dark"
    assert "PostToolUse" in final["hooks"]


def test_run_idempotent_second_call_noop(fake_home: Path, capsys) -> None:
    assert run(_argv("claude", fake_home, "--yes")) == 0
    capsys.readouterr()  # drop first output
    rc = run(_argv("claude", fake_home, "--yes"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "already installed" in out
    # Still only one PostToolUse entry per matcher.
    target = fake_home / ".claude" / "settings.json"
    data = json.loads(target.read_text())
    matchers = [e.get("matcher") for e in data["hooks"]["PostToolUse"]]
    assert len(matchers) == len(set(matchers))


def test_run_unsupported_agent_returns_1(fake_home: Path, capsys) -> None:
    # argparse rejects unknown choices with exit code 2 -- we catch the
    # SystemExit from argparse to assert the contract.
    with pytest.raises(SystemExit) as exc:
        run(["--agent", "emacs", "--home", str(fake_home)])
    assert exc.value.code == 2  # argparse choice rejection
    err = capsys.readouterr().err
    assert "emacs" in err or "invalid choice" in err


def test_run_autodetect_no_agent_returns_1(fake_home: Path, capsys) -> None:
    rc = run(["--home", str(fake_home), "--ts-root", str(REPO_ROOT), "--dry-run"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "auto-detect" in err.lower() or "agent" in err.lower()


def test_run_unwritable_target_returns_2(fake_home: Path, monkeypatch, capsys) -> None:
    # Force _write_settings to raise PermissionError.
    from token_savior import cli_init as mod

    def boom(path, data):
        raise PermissionError(f"cannot write {path}: locked")

    monkeypatch.setattr(mod, "_write_settings", boom)
    rc = run(_argv("claude", fake_home, "--yes"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "cannot write" in err.lower()


def test_run_preserves_existing_hook_entry(fake_home: Path) -> None:
    target = fake_home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "hooks": {
            "PostToolUse": [
                {"matcher": "MyMatcher",
                 "hooks": [{"type": "command", "command": "/my/cmd"}]}
            ]
        }
    }))
    assert run(_argv("claude", fake_home, "--yes")) == 0
    data = json.loads(target.read_text())
    entries = data["hooks"]["PostToolUse"]
    matchers = {e["matcher"] for e in entries}
    assert "MyMatcher" in matchers
    assert len(entries) >= 2  # original + TS-added


def test_run_works_for_every_supported_agent(fake_home: Path) -> None:
    for agent in SUPPORTED_AGENTS:
        rc = run([
            "--agent", agent,
            "--home", str(fake_home / agent),
            "--ts-root", str(REPO_ROOT),
            "--dry-run",
        ])
        assert rc == 0, f"dry-run failed for {agent}"
