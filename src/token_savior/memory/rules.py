"""Trigger→action rules for deterministic enforcement (unité rules).

A small, hand-maintained catalog (JSON) decides whether a pending tool call is
allowed, denied, or warned. The PreToolUse hook turns a `deny` into a real
permissionDecision:deny. Decision logic here is pure and testable; the hook
does the I/O (emit JSON, log the ledger event) and always fails open.
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any, Callable

# Louis-editable catalog, next to the hooks.
DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2].parent / "hooks" / "ledger-rules.json"


def load_rules(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load the rules catalog. Missing/invalid file → empty list (fail open)."""
    p = Path(path) if path else DEFAULT_RULES_PATH
    try:
        data = json.loads(p.read_text())
    except Exception:
        return []
    return data if isinstance(data, list) else []


def match(tool_name: str, tool_input: dict[str, Any] | None,
          rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rules whose trigger matches this tool call."""
    ti = tool_input or {}
    out: list[dict[str, Any]] = []
    for r in rules:
        trig = r.get("trigger", {})
        if trig.get("tool") and trig["tool"] != tool_name:
            continue
        cmd_re = trig.get("command_regex")
        if cmd_re and not re.search(cmd_re, ti.get("command", "") or ""):
            continue
        glob = trig.get("file_glob")
        if glob and not fnmatch.fnmatch(ti.get("file_path", "") or "", glob):
            continue
        out.append(r)
    return out


def precondition_met(session_id: str | None, name: str | None) -> bool:
    """True if a `precondition` event named `name` was logged this session."""
    if not session_id or not name:
        return False
    from token_savior.memory import ledger
    for ev in ledger.ledger_query(event_type="precondition",
                                  session_id=session_id, limit=100):
        if (ev.get("meta") or {}).get("name") == name:
            return True
    return False


def _allow() -> dict[str, Any]:
    return {"decision": "allow", "reason": None, "rule_id": None, "severity": None}


def _decision(decision: str, rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision": decision,
        "reason": rule.get("action", {}).get("message"),
        "rule_id": rule.get("id"),
        "severity": rule.get("severity"),
    }


def evaluate(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    session_id: str | None,
    *,
    rules: list[dict[str, Any]] | None = None,
    precondition_check: Callable[[str | None, str | None], bool] | None = None,
) -> dict[str, Any]:
    """Decide allow/deny for a pending tool call. Pure — no I/O.

    `deny` actions take precedence (most restrictive), then unmet
    `require_precondition`, then `warn` (which allows with a reason).
    """
    if rules is None:
        rules = load_rules()
    matched = match(tool_name, tool_input, rules)
    if not matched:
        return _allow()
    check = precondition_check or precondition_met

    for r in matched:
        if r.get("action", {}).get("type") == "deny":
            return _decision("deny", r)
    for r in matched:
        act = r.get("action", {})
        if act.get("type") == "require_precondition":
            if session_id and check(session_id, act.get("precondition")):
                continue
            return _decision("deny", r)
    for r in matched:
        if r.get("action", {}).get("type") == "warn":
            return _decision("allow", r)
    return _allow()
