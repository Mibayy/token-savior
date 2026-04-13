"""Differential Context Protocol — Rabin-fingerprint content chunker.

Splits tool outputs into content-defined chunks so that two outputs sharing
~N% of content share ~N% of chunk fingerprints. Stable chunks (seen in prior
sessions) are pinned at the front of the reordered output, maximizing the
length of the prefix that Anthropic's prompt cache can reuse.

Reference: Rabin-Karp rolling hash (LBFS, rsync, zsync, content-defined
chunking). Chunk size ≈ ``BOUNDARY_MOD`` bytes on average, bounded by
``MIN_CHUNK`` / ``MAX_CHUNK``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

WINDOW_SIZE = 64     # Rolling-hash window length
BOUNDARY_MOD = 256   # hash mod BOUNDARY_MOD == 0 → boundary (~256B average)
MIN_CHUNK = 128      # minimum chunk length
MAX_CHUNK = 1024     # forced cut above this length


@dataclass
class ContentChunk:
    content: str
    fingerprint: str          # SHA-256 (12 hex) of the chunk bytes
    is_stable: bool = False   # Seen in a prior session
    cache_hit_count: int = 0  # Times this fingerprint was seen before
    _rank: float = field(default=0.0, repr=False)


def _pow_prime(window: int, mod: int, prime: int = 31) -> int:
    """Cached PRIME**window mod MOD — pre-compute once."""
    return pow(prime, window, mod)


def rabin_fingerprint(data: str, window: int = WINDOW_SIZE) -> list[int]:
    """Return boundary byte offsets for *data*.

    Algorithm: maintain a rolling polynomial hash over a sliding byte window.
    Whenever ``h mod BOUNDARY_MOD == 0`` and the current run has reached
    ``MIN_CHUNK``, emit a boundary. Any run reaching ``MAX_CHUNK`` is cut
    deterministically to bound worst-case chunk size.
    """
    bytes_data = data.encode("utf-8")
    n = len(bytes_data)
    if n == 0:
        return [0, 0]
    boundaries: list[int] = [0]
    if n <= window:
        boundaries.append(n)
        return boundaries

    PRIME = 31
    MOD = 2**32
    prime_pow = _pow_prime(window, MOD, PRIME)

    h = 0
    for i in range(window):
        h = (h * PRIME + bytes_data[i]) % MOD

    for i in range(window, n):
        h = (h * PRIME + bytes_data[i] - bytes_data[i - window] * prime_pow) % MOD
        if h % BOUNDARY_MOD == 0:
            pos = i - window + 1
            if pos - boundaries[-1] >= MIN_CHUNK:
                boundaries.append(pos)
        elif i - boundaries[-1] >= MAX_CHUNK:
            boundaries.append(i)

    if boundaries[-1] != n:
        boundaries.append(n)
    return boundaries


def chunk_content(content: str) -> list[ContentChunk]:
    """Split *content* into content-defined chunks."""
    if not content:
        return []
    boundaries = rabin_fingerprint(content)
    chunks: list[ContentChunk] = []
    # We need char offsets, but boundaries are byte offsets. Re-encode once
    # and decode slices using errors='ignore' to be robust to multi-byte
    # boundaries (rare; Rabin boundaries are byte-aligned).
    raw = content.encode("utf-8")
    for i in range(len(boundaries) - 1):
        start, end = boundaries[i], boundaries[i + 1]
        if end <= start:
            continue
        text = raw[start:end].decode("utf-8", errors="ignore")
        if not text:
            continue
        fp = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        chunks.append(ContentChunk(content=text, fingerprint=fp))
    return chunks
