"""grep / ripgrep output compactor — group results by filename."""
from __future__ import annotations

import re
from collections import defaultdict



# Recursive grep / ripgrep output looks like:
#   src/a.py:14:  found match here
#   src/a.py:22:  another match
#   src/b.py:8:   match
#
# Some variants use NUL or "-" separator (for context lines). We accept ":" only
# since that's the default and what Claude's bash output always shows.
_HIT_RE = re.compile(r"^(?P<path>[^\s:][^:]*?):(?P<line>\d+):(?P<rest>.*)$")

_SHELL_COMPOSITION_RE = re.compile(r"[|;&]|&&|\|\|")


class GrepCompactor:
    """Group recursive grep/ripgrep hits by filename.

    Strategy:
      - Recognize ``grep``, ``grep -rn``, ``rg``, ``ripgrep`` invocations.
      - Reject commands that contain shell composition (``|``, ``;``, ``&&``,
        ``||``) or a ``-c`` / ``--count`` flag (already counted).
      - When stdout has ≥ 5 ``path:line:rest`` lines, regroup as
        ``path (Nx): L<line>, L<line>, …``.
      - Otherwise drop blank lines and runs of ``--`` context separators.
      - Pass-through (return original) when there's nothing to gain.
    """

    # Match `grep`, `rg`, `ripgrep` as a standalone verb (not `pgrep`,
    # `bzgrep`, `xargs grep`-fine, `egrep` and `fgrep` accepted).
    _CMD_RE = re.compile(
        r"(?:^|[\s;&|])"
        r"(?:rg|ripgrep|grep|egrep|fgrep)"
        r"(?![\w\-])"
    )

    _COUNT_FLAG_RE = re.compile(r"(?:^|\s)(?:-c|--count)(?:\s|=|$)")

    def matches(self, command: str) -> bool:
        if not command:
            return False
        if _SHELL_COMPOSITION_RE.search(command):
            return False
        if self._COUNT_FLAG_RE.search(command):
            return False
        # The verb itself must appear; use a fresh match anchored at the start
        # of the command (after optional leading whitespace).
        head = command.lstrip()
        m = re.match(r"(?:rg|ripgrep|grep|egrep|fgrep)(?![\w\-])", head)
        return bool(m)

    def compact(self, stdout: str, stderr: str = "") -> str:
        text = stdout or ""
        if stderr:
            text = (text + "\n" + stderr) if text else stderr

        lines = text.splitlines()
        # Strip blank lines and "--" separators (rg context separator)
        meaningful = [
            ln for ln in lines if ln.strip() and ln.strip() != "--"
        ]

        # Try to parse hits with path:line:rest shape.
        hits: dict[str, list[str]] = defaultdict(list)
        non_hit_lines: list[str] = []
        for ln in meaningful:
            m = _HIT_RE.match(ln)
            if m:
                hits[m.group("path")].append(m.group("line"))
            else:
                non_hit_lines.append(ln)

        total_hits = sum(len(v) for v in hits.values())

        # Pass-through cases:
        #   - fewer than 5 lines of output — nothing to gain.
        #   - no recognizable path:line:rest hits — single-file grep, just
        #     collapse blank lines.
        if len(meaningful) < 5:
            return "\n".join(meaningful)

        if total_hits == 0:
            # Single-file grep (no path prefix) or unparsable: return the
            # blank-line-stripped version. Already a small win on noisy output.
            return "\n".join(meaningful)

        # Group by filename. Preserve first-seen order of paths so the output
        # mirrors traversal order from the original grep run.
        ordered_paths: list[str] = []
        seen: set[str] = set()
        for ln in meaningful:
            m = _HIT_RE.match(ln)
            if m:
                p = m.group("path")
                if p not in seen:
                    seen.add(p)
                    ordered_paths.append(p)

        out: list[str] = []
        for path in ordered_paths:
            line_nums = hits[path]
            locs = ", ".join(f"L{n}" for n in line_nums)
            out.append(f"{path} ({len(line_nums)}x): {locs}")

        # Keep any non-hit lines that aren't separators (rare: summary lines
        # from ``rg --stats``). Cap to 5 to avoid leaking noise.
        if non_hit_lines:
            for extra in non_hit_lines[:5]:
                out.append(extra)

        return "\n".join(out)
