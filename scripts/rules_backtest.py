"""Backtest the rules catalog against historical Bash tool captures.

Answers, without deploying anything: how many past commands the DENY rules
would have blocked (eyeball the list for false positives vs true catches), and
how often preflight-before-push would have prompted. A clean catalog = zero
false positives among the deny hits.

Run:  python3 scripts/rules_backtest.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_savior.memory import rules  # noqa: E402


def extract_commands() -> list[str]:
    """Pull Bash commands from the tool_captures history."""
    from token_savior import memory_db
    conn = memory_db.get_db()
    rows = conn.execute(
        "SELECT args_summary FROM tool_captures "
        "WHERE tool_name='Bash' AND args_summary IS NOT NULL"
    ).fetchall()
    conn.close()
    cmds: list[str] = []
    for (arg,) in rows:
        try:
            d = json.loads(arg)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("command"):
            cmds.append(d["command"])
    return cmds


def backtest(commands: list[str], catalog: list[dict] | None = None) -> dict:
    """Replay commands against the catalog. Assumes preconditions satisfied so
    only the pure-deny rules fire; counts push occurrences separately."""
    if catalog is None:
        catalog = rules.load_rules()
    push_rules = [r for r in catalog if r.get("id") == "preflight-before-push"]
    deny_hits: list[dict] = []
    push_count = 0
    for cmd in commands:
        d = rules.evaluate("Bash", {"command": cmd}, "backtest",
                           rules=catalog, precondition_check=lambda s, n: True)
        if d["decision"] == "deny":
            deny_hits.append({"command": cmd, "rule_id": d["rule_id"]})
        if push_rules and rules.match("Bash", {"command": cmd}, push_rules):
            push_count += 1
    return {"analyzed": len(commands), "deny_hits": deny_hits, "push_count": push_count}


def main() -> int:
    cmds = extract_commands()
    res = backtest(cmds)
    print(f"Analysé          : {res['analyzed']} commandes Bash historiques")
    print(f"git push trouvés : {res['push_count']} (auraient demandé preflight)")
    print(f"Deny-rule hits   : {len(res['deny_hits'])}")
    for h in res["deny_hits"]:
        print(f"  [{h['rule_id']}] {h['command'][:110]}")
    if not res["deny_hits"]:
        print("→ 0 blocage sur l'historique : aucune fausse alerte des règles deny.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
