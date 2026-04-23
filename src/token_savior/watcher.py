"""File watcher with graceful fallback to mtime polling.

Replaces the per-query mtime stat loop (~2.1 ms/call measured on a 243-
file project, AUDIT.md Phase 1) with a background thread that streams
change events via ``watchfiles`` (inotify / FSEvents / ReadDirectoryChangesW).

Rename-atomique editors (vim, IntelliJ) surface as ``added`` + ``deleted``
pairs; the caller handles both so such workflows reindex correctly.

Controlled by ``TOKEN_SAVIOR_WATCHER``:

- ``auto`` (default) : try the watcher; on any start-time or runtime
  failure, log one loud line to stderr and the slot manager falls back
  to the legacy mtime-stat path.
- ``on``               : require the watcher. Failure is surfaced to the
                          caller, no fallback.
- ``off``              : never start the watcher; mtime path only.

Linux inotify is bounded by ``fs.inotify.max_user_watches`` (default
8 192 on stock Ubuntu). When the project has more files than the limit
allows we log a pre-flight warning so the user can raise the sysctl
before the thread starts failing silently mid-session.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


# Lazy-probe watchfiles. Importing the package at module load pulls in a
# Rust CPython extension (notify) whose destructor segfaults on Python
# 3.12 at interpreter shutdown on GitHub Actions runners — even when no
# watcher thread was ever started. Defer the real import to the thread
# that actually needs it.
_WATCHFILES_AVAILABLE = importlib.util.find_spec("watchfiles") is not None

WatcherMode = str  # "auto" | "on" | "off"


def resolve_mode() -> WatcherMode:
    """Read ``TOKEN_SAVIOR_WATCHER`` with a sane default.

    Unknown values collapse to ``auto`` so a typo never silently
    disables the watcher. The default is ``auto`` — existing
    deployments get the upgrade without having to opt in.
    """
    raw = os.environ.get("TOKEN_SAVIOR_WATCHER", "auto").strip().lower()
    if raw not in {"auto", "on", "off"}:
        return "auto"
    return raw


def _inotify_ceiling() -> int | None:
    """Return ``fs.inotify.max_user_watches`` if readable (Linux only).

    Returns ``None`` on non-Linux or when the proc file is unreadable;
    callers treat that as "no ceiling data available" and skip the
    pre-flight warning.
    """
    path = "/proc/sys/fs/inotify/max_user_watches"
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _build_pattern_filter(root: Path, exclude_patterns: list[str]):
    """Build a ``watchfiles``-compatible filter lazily.

    Subclassing ``DefaultFilter`` would require importing watchfiles at
    module scope. We construct the class inside ``_run`` so the Rust
    extension is only loaded in the watcher thread — not on every
    import of ``token_savior.server``.
    """
    from watchfiles import DefaultFilter  # type: ignore

    class _PatternFilter(DefaultFilter):
        def __init__(self) -> None:
            super().__init__()
            self._root = root
            self._patterns: list[str] = list(exclude_patterns)

        def __call__(self, change, path: str) -> bool:  # type: ignore[override]
            try:
                if not super().__call__(change, path):
                    return False
            except Exception:
                return False
            try:
                rel = str(Path(path).relative_to(self._root))
            except ValueError:
                return False
            import fnmatch
            for pattern in self._patterns:
                if fnmatch.fnmatch(rel, pattern):
                    return False
            return True

    return _PatternFilter()


class SlotWatcher:
    """Wraps a ``watchfiles`` thread and exposes a drain() API.

    Design choice: do NOT mutate the project index from the watcher
    thread. The thread accumulates change events in thread-safe sets
    and the main-thread ``SlotManager.maybe_update`` drains them on
    the next tool call. This keeps the index mutation serial with the
    rest of the slot lifecycle (cache_gen bump, graph rebuild, cache
    save) without needing a separate index lock.
    """

    def __init__(
        self,
        root: str,
        exclude_patterns: list[str],
    ) -> None:
        self.root_path = Path(root).resolve()
        self._exclude_patterns = list(exclude_patterns)
        self._dirty: set[str] = set()
        self._deleted: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Spawn the watcher thread. Returns True on success.

        A return of False means the caller should fall back to the mtime
        path. The reason is available on :attr:`failure_reason` and has
        already been logged to stderr.
        """
        if not _WATCHFILES_AVAILABLE:
            self._failure = "watchfiles package not installed"
            print(
                f"[token-savior] watcher disabled: {self._failure}. "
                "Falling back to mtime polling.",
                file=sys.stderr,
            )
            return False

        # Pre-flight inotify ceiling check. Count the project's .py-ish
        # files cheaply — if we're already above 50 % of the sysctl
        # limit the watcher will probably fail mid-session, so warn now
        # rather than silently later.
        ceiling = _inotify_ceiling()
        if ceiling is not None:
            watchable = _count_watchable_dirs(self.root_path)
            if watchable > ceiling // 2:
                print(
                    f"[token-savior] watcher: project has ~{watchable} "
                    f"directories, inotify max_user_watches={ceiling}. "
                    "Close to the limit — raise the sysctl if the "
                    "watcher starts dropping events: "
                    "`sudo sysctl fs.inotify.max_user_watches=524288`.",
                    file=sys.stderr,
                )

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"ts-watcher-{self.root_path.name}",
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # 3s gives the Rust notify backend time to drop inotify
            # watches cleanly on stressed CI runners (1s was occasionally
            # timing out and leaving a dangling thread).
            self._thread.join(timeout=3.0)

    @property
    def ok(self) -> bool:
        """True iff the watcher thread is alive and no failure was recorded."""
        return (
            self._failure is None
            and self._thread is not None
            and self._thread.is_alive()
        )

    @property
    def failure_reason(self) -> str | None:
        return self._failure

    # ── event handling ───────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            # Lazy-import watchfiles inside the thread so importing
            # token_savior.watcher does NOT load the Rust notify .so
            # — see module docstring.
            from watchfiles import watch  # type: ignore
            flt = _build_pattern_filter(self.root_path, self._exclude_patterns)
            # ``TS_WATCHER_FORCE_POLLING=1`` routes watchfiles through pure
            # Python mtime polling instead of the Rust notify backend.
            # Slower (~200 ms polling cycle vs real-time inotify) but
            # avoids a Rust-backend cleanup race that segfaults at
            # interpreter exit on some GitHub Actions runners. Tests
            # opt-in; production paths stay on inotify.
            force_polling = bool(os.environ.get("TS_WATCHER_FORCE_POLLING"))
            for changes in watch(
                str(self.root_path),
                watch_filter=flt,
                stop_event=self._stop,
                raise_interrupt=False,
                force_polling=force_polling,
            ):
                # Import Change lazily too — same reason as ``watch``.
                from watchfiles import Change  # type: ignore
                for change, path in changes:
                    try:
                        rel = str(Path(path).relative_to(self.root_path))
                    except ValueError:
                        continue
                    kind = _classify_change(change, Change)
                    with self._lock:
                        if kind == "deleted":
                            self._deleted.add(rel)
                            self._dirty.discard(rel)
                        else:
                            self._dirty.add(rel)
                            self._deleted.discard(rel)
        except OSError as e:
            ceiling = _inotify_ceiling()
            ceiling_note = (
                f" (fs.inotify.max_user_watches={ceiling})" if ceiling else ""
            )
            msg = (
                f"[token-savior] watcher OS error: {e}{ceiling_note}. "
                "Falling back to mtime polling. Raise "
                "fs.inotify.max_user_watches or set "
                "TOKEN_SAVIOR_WATCHER=off to silence this warning."
            )
            print(msg, file=sys.stderr)
            self._failure = str(e)
        except Exception as e:
            print(
                f"[token-savior] watcher crashed: {type(e).__name__}: {e}. "
                "Falling back to mtime polling.",
                file=sys.stderr,
            )
            self._failure = f"{type(e).__name__}: {e}"

    def drain(self) -> tuple[set[str], set[str]]:
        """Return (dirty, deleted) since the last drain, then reset.

        Both sets are relative paths under ``root_path``.
        """
        with self._lock:
            d = self._dirty
            x = self._deleted
            self._dirty = set()
            self._deleted = set()
        return d, x


def _classify_change(change, Change=None) -> str:
    """Map ``watchfiles.Change`` to a stable string the caller understands."""
    if Change is None:
        return "modified"
    if change == Change.added:
        return "added"
    if change == Change.deleted:
        return "deleted"
    return "modified"


def _count_watchable_dirs(root: Path) -> int:
    """Rough estimate of how many directories inotify would register.

    Counts directories under ``root`` while honoring ``EXCLUDED_DIRS``
    from the indexer. Not authoritative — watchfiles' internal filter
    differs slightly — but good enough to surface a "you're near the
    ceiling" warning.
    """
    try:
        from token_savior.project_indexer import EXCLUDED_DIRS
    except Exception:
        EXCLUDED_DIRS = {".git", "__pycache__", "node_modules"}
    count = 0
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        count += 1
        # Bail once we've proven we're well over any reasonable limit —
        # the caller only uses "count > ceiling/2" as a ceiling trigger.
        if count > 250_000:
            break
    return count
