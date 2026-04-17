"""A3: opt-in LLM auto-extraction from PostToolUse.

Activates only when ``TS_AUTO_EXTRACT=1`` is set in the environment.
When inactive every public entry point is a cheap boolean check — no
imports of http libs, no threads, no network traffic. The surrounding
hook pipeline is unaffected in the common case.

Flow when enabled:

    PostToolUse hook (shell)
      → ``process_tool_use(tool_name, tool_input, tool_output)``
        → daemon thread (non-blocking)
          → build compact prompt
          → POST Anthropic messages API (stdlib ``urllib``)
          → parse JSON array, validate fields
          → ``observation_save`` per valid item (tag: ``auto-extract``)

Env:
    TS_AUTO_EXTRACT=1                       — master switch (required)
    TS_API_KEY=...                          — Anthropic API key (required)
    TS_MODEL=claude-haiku-4-5-20251001      — override default model

Dedup is handled downstream by ``content_hash`` (P2); extracting the
same obs twice collapses to a single row.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any

ENV_ENABLED = "TS_AUTO_EXTRACT"
ENV_API_KEY = "TS_API_KEY"
ENV_MODEL = "TS_MODEL"

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_OBS_PER_CALL = 3
MAX_OUTPUT_CHARS = 2000
VALID_TYPES = {
    "bugfix", "convention", "warning", "guardrail", "infra", "command",
}

SYSTEM_PROMPT = (
    "Extract 0-3 observations from this tool use.\n"
    "Return JSON array only, no prose.\n"
    "Each item: {type, title, content, why, symbol?, file_path?}\n"
    "Types: bugfix|convention|warning|guardrail|infra|command\n"
    "If nothing notable: return []"
)


def is_enabled() -> bool:
    """Master switch — True only when ``TS_AUTO_EXTRACT=1`` exactly."""
    return os.environ.get(ENV_ENABLED, "").strip() == "1"


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _build_user_prompt(
    tool_name: str, tool_input: Any, tool_output: str
) -> str:
    try:
        input_str = json.dumps(tool_input, default=str, ensure_ascii=False)
    except Exception:
        input_str = str(tool_input)
    if len(input_str) > MAX_OUTPUT_CHARS:
        input_str = input_str[:MAX_OUTPUT_CHARS] + "…"
    return (
        f"tool_name: {tool_name}\n"
        f"tool_input: {input_str}\n"
        f"tool_output: {_truncate(tool_output)}"
    )


def _call_api(system: str, user: str, api_key: str, model: str) -> str | None:
    """POST Anthropic ``/v1/messages`` via stdlib urllib; return raw text or None."""
    import urllib.request

    body = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        print(f"[token-savior:auto-extract] API call failed: {exc}",
              file=sys.stderr)
        return None
    try:
        for block in payload.get("content") or []:
            if block.get("type") == "text":
                return block.get("text") or ""
    except Exception:
        pass
    return None


def _parse_items(raw: str) -> list[dict]:
    """Parse the LLM's JSON array reply; return validated items (max 3)."""
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data[:MAX_OBS_PER_CALL]:
        if not isinstance(item, dict):
            continue
        obs_type = str(item.get("type", "")).lower().strip()
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "")).strip()
        if obs_type not in VALID_TYPES:
            continue
        if not title or not content:
            continue
        norm: dict[str, Any] = {
            "type": obs_type,
            "title": title[:200],
            "content": content[:2000],
        }
        for key, cap in (("why", 500), ("symbol", 200), ("file_path", 500)):
            raw_val = item.get(key)
            if raw_val:
                s = str(raw_val).strip()
                if s:
                    norm[key] = s[:cap]
        out.append(norm)
    return out


def extract_observations(
    tool_name: str, tool_input: Any, tool_output: str
) -> list[dict]:
    """Call the LLM and return a validated list of observation dicts.

    Returns ``[]`` when the API key is missing, the call fails, or the
    response is malformed. Never raises.
    """
    api_key = os.environ.get(ENV_API_KEY, "").strip()
    if not api_key:
        return []
    model = os.environ.get(ENV_MODEL, "").strip() or DEFAULT_MODEL
    user = _build_user_prompt(tool_name, tool_input, tool_output)
    raw = _call_api(SYSTEM_PROMPT, user, api_key, model)
    if raw is None:
        return []
    return _parse_items(raw)


def _save_extracted(items: list[dict], project_root: str) -> int:
    """Save validated obs via observation_save; return count persisted."""
    if not items or not project_root:
        return 0
    try:
        from token_savior import memory_db
    except Exception:
        return 0
    saved = 0
    for item in items:
        try:
            oid = memory_db.observation_save(
                session_id=None,
                project_root=project_root,
                type=item["type"],
                title=item["title"],
                content=item["content"],
                why=item.get("why"),
                symbol=item.get("symbol"),
                file_path=item.get("file_path"),
                tags=["auto-extract"],
                importance=2,
            )
            if oid:
                saved += 1
        except Exception as exc:
            print(f"[token-savior:auto-extract] save error: {exc}",
                  file=sys.stderr)
    return saved


def _resolve_project_root() -> str:
    """Fallback project resolution: pick the most-observed project_root."""
    try:
        from token_savior import memory_db
        db = memory_db.get_db()
        row = db.execute(
            "SELECT project_root FROM observations "
            "GROUP BY project_root ORDER BY COUNT(*) DESC LIMIT 1",
        ).fetchone()
        db.close()
        if row:
            return row[0] if isinstance(row, tuple) else row["project_root"]
    except Exception:
        pass
    return ""


def _process_sync(
    tool_name: str, tool_input: Any, tool_output: str, project_root: str,
) -> int:
    items = extract_observations(tool_name, tool_input, tool_output)
    if not items:
        return 0
    return _save_extracted(items, project_root)


def process_tool_use(
    tool_name: str,
    tool_input: Any,
    tool_output: str = "",
    project_root: str | None = None,
) -> bool:
    """Non-blocking dispatch: start a daemon thread, return immediately.

    Returns True when a worker was spawned, False when auto-extract is
    disabled (zero overhead path) or no project context resolvable.
    """
    if not is_enabled():
        return False
    pr = project_root or _resolve_project_root()
    if not pr:
        return False
    threading.Thread(
        target=_process_sync,
        args=(tool_name, tool_input, tool_output, pr),
        daemon=True,
    ).start()
    return True
