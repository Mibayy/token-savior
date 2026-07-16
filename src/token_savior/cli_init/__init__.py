"""
`ts init` -- merge Token Savior hook configuration into an AI agent's
settings file.

Inspired by `rtk init -g --agent X`.  Stdlib only.

Exit codes:
    0  success (or dry-run)
    1  unsupported agent / unknown error
    2  settings file unwritable / locked
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Iterable

from .agent_paths import SUPPORTED_AGENTS, detection_path, hook_config_paths, settings_path
from .merger import added_entries, format_diff, merge_hook_config


# Resolve the directory whose ``hooks/`` subdir holds the bundled configs.
# Two layouts to support:
#   1. PyPI install -- hooks shipped inside the wheel at
#      ``site-packages/token_savior/hooks/`` via the
#      ``[tool.hatch.build.targets.wheel.force-include]`` rule in pyproject.
#   2. Source checkout (editable install) -- hooks live at the repo root
#      ``/<repo>/hooks/``; ``Path(__file__).parents[3]`` resolves to the
#      repo root (cli_init/__init__.py -> token_savior/ -> src/ -> repo).
# We pick layout 1 when ``token_savior/hooks/`` exists next to the package,
# otherwise fall back to layout 2.
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if (_PACKAGE_ROOT / "hooks").is_dir():
    _REPO_ROOT = _PACKAGE_ROOT
else:
    _REPO_ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _detect_agent(home: Path) -> str | None:
    """Return the first agent whose install marker already exists."""
    for agent in SUPPORTED_AGENTS:
        try:
            if detection_path(agent, home).exists():
                return agent
        except ValueError:
            continue
    return None


def _read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"cannot read {path}: {e}") from e
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"settings file is not valid JSON ({path}): {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"settings file root must be a JSON object: {path}")
    return data


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _backup_path(target: Path, *, now: _dt.datetime | None = None) -> Path:
    now = now or _utcnow()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return target.with_name(f"{target.name}.bak-{stamp}")


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        raise PermissionError(f"cannot write {path}: {e}") from e


def _hook_python() -> str:
    """Interpreter for the installed hook commands: the one running `ts init`
    (it can import token_savior by construction), preferring its unversioned
    `python3` alias so the command survives a venv rebuilt on a newer Python.
    """
    exe = Path(sys.executable)
    alias = exe.with_name("python3")
    try:
        if alias != exe and alias.samefile(exe):
            return str(alias)
    except OSError:
        pass
    return str(exe)


def _hook_invocation(module: str, ts_root: Path) -> str:
    """How the hook command names its Python entrypoint.

    Wheel installs ship the hooks inside the package (ts_root IS the package
    dir), so `-m token_savior.hooks.<module>` — no interpreter-versioned
    site-packages path to go stale. Source checkouts keep hooks/ at the repo
    root, outside the package, so those run by script path.
    """
    if ts_root.resolve() == _PACKAGE_ROOT:
        return f"-m token_savior.hooks.{module}"
    return str(ts_root / "hooks" / f"{module}.py")


def _json_escaped(value: str) -> str:
    return json.dumps(value)[1:-1]


def _load_hook_bundles(agent: str, ts_root: Path) -> list[dict]:
    """Load shipped hook JSON configs and substitute the {{TS_PYTHON}},
    {{TS_HOOK:<module>}} and legacy {{TS_HOOKS_DIR}} placeholders with the
    actual install paths of this Token Savior copy."""
    hooks_dir = _json_escaped(str((ts_root / "hooks").resolve()))
    python = _json_escaped(_hook_python())
    bundles: list[dict] = []
    missing: list[Path] = []
    for p in hook_config_paths(agent, ts_root):
        if not p.exists():
            missing.append(p)
            continue
        try:
            raw = p.read_text(encoding="utf-8")
            raw = raw.replace("{{TS_HOOKS_DIR}}", hooks_dir)
            raw = raw.replace("{{TS_PYTHON}}", python)
            raw = re.sub(
                r"\{\{TS_HOOK:(\w+)\}\}",
                lambda m: _json_escaped(_hook_invocation(m.group(1), ts_root)),
                raw,
            )
            bundles.append(json.loads(raw))
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(f"cannot load bundled hook config {p}: {e}") from e
    if not bundles:
        raise RuntimeError(
            f"no bundled hook configs found for agent {agent!r} "
            f"(looked for: {', '.join(str(m) for m in missing)})"
        )
    return bundles


def _apply_bundles(existing: dict, bundles: Iterable[dict]) -> dict:
    merged = existing
    for b in bundles:
        merged = merge_hook_config(merged, b)
    return merged


# --------------------------------------------------------------------------- #
# Public entrypoint                                                           #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ts init",
        description="Merge Token Savior hook config into your AI agent settings.",
    )
    p.add_argument(
        "--agent",
        choices=list(SUPPORTED_AGENTS),
        help="Target AI agent (auto-detected if omitted).",
    )
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--global", "-g", dest="scope_global", action="store_true",
                       help="Write to the global (~/) settings file (default).")
    scope.add_argument("--local", "-l", dest="scope_local", action="store_true",
                       help="Write to the project-local settings file.")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip the confirmation prompt.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the diff but do not write anything.")
    # Test hooks (hidden):
    p.add_argument("--home", default=None, help=argparse.SUPPRESS)
    p.add_argument("--cwd", default=None, help=argparse.SUPPRESS)
    p.add_argument("--ts-root", default=None, help=argparse.SUPPRESS)
    return p


def run(argv: list[str] | None = None, *,
        stdin=None, stdout=None, stderr=None) -> int:
    """Execute `ts init`.  Returns an exit code."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    stdin = stdin or sys.stdin

    parser = build_parser()
    args = parser.parse_args(argv)

    home = Path(args.home) if args.home else Path.home()
    cwd = Path(args.cwd) if args.cwd else Path.cwd()
    ts_root = Path(args.ts_root) if args.ts_root else _REPO_ROOT

    scope = "local" if args.scope_local else "global"

    agent = args.agent or _detect_agent(home)
    if agent is None:
        print(
            "error: could not auto-detect an agent (no known settings.json found). "
            f"Pass --agent {{{','.join(SUPPORTED_AGENTS)}}}.",
            file=stderr,
        )
        return 1
    if agent not in SUPPORTED_AGENTS:
        print(f"error: unsupported agent {agent!r}", file=stderr)
        return 1

    try:
        target = settings_path(agent, scope, home=home, cwd=cwd)
        bundles = _load_hook_bundles(agent, ts_root)
        before = _read_settings(target)
        after = _apply_bundles(before, bundles)
    except RuntimeError as e:
        print(f"error: {e}", file=stderr)
        return 1

    added = added_entries(before, after)
    if not added:
        print(f"Token Savior hooks already installed for {agent} ({target}).", file=stdout)
        return 0

    # Show the diff.
    print(f"Target: {target}", file=stdout)
    print(f"Agent:  {agent}  (scope: {scope})", file=stdout)
    print(f"Will add {len(added)} hook entr{'y' if len(added) == 1 else 'ies'}:", file=stdout)
    for event, (matcher, cmd) in added:
        print(f"  + {event}: matcher={matcher!r} command={cmd}", file=stdout)
    print("", file=stdout)
    print(format_diff(before, after), file=stdout)

    if args.dry_run:
        print("(dry-run: no changes written)", file=stdout)
        return 0

    if not args.yes:
        try:
            answer = input("Apply? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.", file=stdout)
            return 0

    # Backup if the file exists.
    if target.exists():
        bak = _backup_path(target)
        try:
            bak.write_bytes(target.read_bytes())
        except OSError as e:
            print(f"error: cannot create backup {bak}: {e}", file=stderr)
            return 2
        print(f"Backup: {bak}", file=stdout)

    try:
        _write_settings(target, after)
    except PermissionError as e:
        print(f"error: {e}", file=stderr)
        return 2

    print(f"Wrote {target}.", file=stdout)
    print("", file=stdout)
    print("Next steps to activate Token Savior:", file=stdout)
    print("  1. Add these env vars to your shell rc (~/.bashrc, ~/.zshrc):", file=stdout)
    print("       export TS_BASH_COMPACT=1     # compact git/pytest/cargo/gh/... outputs", file=stdout)
    print("       export TS_BASH_REWRITE=1     # rewrite commands to denser variants", file=stdout)
    print("  2. (Optional) For the Pareto-optimal MCP manifest:", file=stdout)
    print("       export TOKEN_SAVIOR_PROFILE=optimized", file=stdout)
    print(f"  3. Restart {agent} so the new hooks take effect.", file=stdout)
    return 0
