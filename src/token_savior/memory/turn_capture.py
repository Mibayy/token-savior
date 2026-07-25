"""Turn-level conversational capture (Stop hook boundary).

The PostToolUse pipeline only sees tool calls, so a turn that carries a
preference, a decision or a validated piece of content leaves no trace when
it happens to use no tool. This module reads the last turn of the transcript
and promotes it to observations.

Two cost guards, in order:

1. ``should_capture`` — a pure local regex gate. No subprocess, no network.
   Turns that carry no intent signal cost nothing at all.
2. ``DEFAULT_MODEL`` — the extraction runs on the cheap model, not on the
   session model.

Flow:

    Stop hook (shell)
      → ``capture_turn(transcript_path, project_root)``
        → read last user/assistant pair
        → local gate (bail out here in the common case)
        → ``claude -p --model haiku`` (stdlib subprocess)
        → parse JSON array, validate fields
        → ``observation_save`` per valid item (tag: ``turn-capture``)

Dedup is handled downstream by ``content_hash``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any

DEFAULT_MODEL = "haiku"
MAX_OBS_PER_TURN = 3
MIN_PROMPT_CHARS = 15
CALL_TIMEOUT_SEC = 60

VALID_TYPES = {
    "preference", "decision", "content", "convention", "guardrail", "warning",
}

REQUIRED_FIELDS = ("type", "title", "content")

#: Intent signals. A turn matching none of these is dropped before any spend.
SIGNAL_PATTERNS = [
    # French — preference / standing instruction
    r"\bà l'avenir\b", r"\bdésormais\b", r"\btoujours\b", r"\bjamais\b",
    r"\bje préfère\b", r"\bje veux que\b", r"\bne mets pas\b",
    r"\bretiens\b", r"\bnote que\b", r"\brappelle-toi\b",
    # French — decision / correction
    r"\bon part sur\b", r"\bon garde\b", r"\bon fait\b", r"\bplutôt que\b",
    r"\battention à\b", r"\bfais gaffe\b", r"\ben fait c'est\b",
    r"\bc'est pas ça\b", r"\bvalidé\b", r"\bok pour\b",
    # English — preference / decision
    r"\bfrom now on\b", r"\balways\b", r"\bnever\b", r"\bprefer\b",
    r"\bremember that\b", r"\blet's go with\b", r"\bwe decided\b",
    r"\bactually,? it's\b",
]

_SIGNAL_RE = re.compile("|".join(SIGNAL_PATTERNS), re.IGNORECASE)

SYSTEM_PROMPT = (
    "Extract 0-3 durable observations from this conversation turn.\n"
    "Keep only what stays true after the turn: a stated preference, a "
    "settled decision, a correction of vocabulary or method, a piece of "
    "content validated for publication.\n"
    "Drop anything one-off, and drop anything about running a command.\n"
    "Return a JSON array only, no prose.\n"
    "Each item: {type, title, content, why}\n"
    "Types: preference|decision|content|convention|guardrail|warning\n"
    "Write each item in the language of the turn.\n"
    "If nothing durable: return []"
)


# Blocs injectes par le harnais : rappels memoire, notes systeme. Un tour qui
# ne fait que les citer ne contient aucune information nouvelle.
_INJECTED_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE)


def should_capture(prompt_text: str) -> bool:
    """Local gate — True when the turn carries a durable intent signal.

    Les blocs injectes sont retires AVANT de chercher un signal. Sans ca, un
    souvenir rappele en debut de session est re-extrait au tour suivant comme
    s'il venait de la conversation, et se reecrit en boucle. C'est la cause
    racine mesuree de l'audit Mem0 : 97,8% de bruit sur 32 jours de
    production, dont 52,7% de re-extraction du contexte injecte, et un seul
    faux souvenir ayant engendre 808 doublons.
    github.com/mem0ai/mem0/issues/4573
    """
    if not prompt_text:
        return False
    said = _INJECTED_RE.sub(" ", prompt_text).strip()
    if len(said) < MIN_PROMPT_CHARS:
        return False
    return _SIGNAL_RE.search(said) is not None


def _text_of(content: Any) -> str:
    """Flatten a message content field to plain text, ignoring tool blocks."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return ""


def read_last_turn(transcript_path: str) -> dict | None:
    """Return ``{'user': ..., 'assistant': ...}`` for the last exchange."""
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None

    user = ""
    assistant = ""
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        message = entry.get("message") or {}
        role = message.get("role") or entry.get("type")
        text = _text_of(message.get("content"))
        if not text:
            continue
        if role == "user":
            user = text
            assistant = ""
        elif role == "assistant":
            assistant = text

    if not user and not assistant:
        return None
    return {"user": user, "assistant": assistant}


def _extract_array(raw: str) -> str:
    """Return the JSON array inside a fenced block or prose, else ``raw``."""
    if not isinstance(raw, str):
        return ""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end > start:
        return raw[start:end + 1]
    return raw


def _parse_items(raw: str) -> list[dict]:
    """Parse the model output into a validated list of observation dicts."""
    try:
        data = json.loads(_extract_array(raw))
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    items = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if any(not entry.get(f) for f in REQUIRED_FIELDS):
            continue
        if entry.get("type") not in VALID_TYPES:
            continue
        items.append(entry)
        if len(items) == MAX_OBS_PER_TURN:
            break
    return items


def _call_claude(prompt: str, model: str) -> str | None:
    """Run ``claude -p`` on the cheap model; return raw text or None."""
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model],
            capture_output=True,
            text=True,
            timeout=CALL_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[token-savior:turn-capture] call failed: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _build_prompt(turn: dict) -> str:
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"user: {turn['user']}\n\n"
        f"assistant: {turn['assistant'][:2000]}"
    )


def capture_turn(transcript_path: str, project_root: str) -> list[dict]:
    """Read the last turn and persist any durable observation it carries."""
    turn = read_last_turn(transcript_path)
    if turn is None or not should_capture(turn["user"]):
        return []

    raw = _call_claude(_build_prompt(turn), DEFAULT_MODEL)
    if not raw:
        return []

    from token_savior import memory_db

    saved = []
    for item in _parse_items(raw):
        obs_id = memory_db.observation_save(
            session_id=None,
            project_root=project_root,
            type=item["type"],
            title=item["title"],
            content=item["content"],
            why=item.get("why"),
            # Provenance : distinguer ce que l'utilisateur a dit lui-meme d'un
            # fait extrait d'un contenu tiers ingere par l'agent. C'est le
            # manque numero un du domaine, et la racine commune du bug de
            # boucle Mem0 et de l'attaque MemGhost, qui plante un faux souvenir
            # persistant via un simple email piege (arXiv 2607.05189, 71% de
            # reussite sur Claude Code). Sans ce marqueur, un souvenir empoisonne
            # est indiscernable d'une preference exprimee de vive voix.
            tags=["turn-capture", "provenance:utilisateur"],
        )
        if obs_id is not None:
            saved.append(item)
    return saved
