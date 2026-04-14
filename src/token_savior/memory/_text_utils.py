"""Text-similarity primitives shared by memory dedup / search / health.

Lifted from memory_db.py during the memory/ subpackage split.
Pure functions and constants only — no DB access.
"""

from __future__ import annotations

import re

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "is",
    "are", "be", "this", "that", "it", "at", "by", "with", "as", "from", "how",
    "why", "what", "when", "where", "can", "le", "la", "les", "un", "une", "des",
    "de", "du", "et", "ou", "est", "sont", "pour", "dans", "sur", "avec", "pas",
    "qui", "que", "quoi", "comment", "pourquoi", "je", "tu", "il", "elle", "on",
    "nous", "vous", "ils", "se", "sa", "son", "ses", "ce", "ces", "tout", "tous",
    "plus", "moins", "faire", "fait", "peux", "peut", "veux", "mais", "donc",
})

_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_-]{2,}")


def _jaccard(a: str, b: str) -> float:
    sa = set((a or "").lower().split())
    sb = set((b or "").lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
