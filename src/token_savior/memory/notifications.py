"""Telegram notification sink for critical observations.

Lifted from memory_db.py during the memory/ subpackage split.
Side-effect-only: reads env vars, posts to api.telegram.org, swallows errors.
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from typing import Any


def notify_telegram(obs: dict[str, Any]) -> None:
    """Send a Telegram notification for a critical observation. Silent on failure."""
    from token_savior import memory_db  # lazy: avoid circular import at module load

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    obs_type = obs.get("type", "")
    try:
        mode = memory_db.get_current_mode()
        if obs_type not in mode.get("notify_telegram_types", []):
            return
    except Exception:
        pass

    emoji = {
        "guardrail": "🚫",
        "error_pattern": "🔴",
        "warning": "⚠️",
        "bugfix": "🐛",
        "decision": "🏛",
        "convention": "📐",
        "note": "📝",
    }.get(obs_type, "📌")

    symbol_part = f"\n🔗 `{obs['symbol']}`" if obs.get("symbol") else ""
    content = obs.get("content") or ""
    suffix = "..." if len(content) > 200 else ""
    text = (
        f"{emoji} *Token Savior Memory*\n"
        f"[{obs_type}] {obs.get('title','')}"
        f"{symbol_part}\n\n"
        f"{content[:200]}{suffix}"
    )

    try:
        data = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
