"""kubectl output compactors."""
from __future__ import annotations

import re

from .base import Compactor


class KubectlGetCompactor(Compactor):
    """Compact `kubectl get pods` / `kubectl get services` columnar output.

    Keeps the minimum columns the model needs to reason about state:
      - pods       -> NAME STATUS AGE
      - services   -> NAME TYPE CLUSTER-IP AGE
    Drops READY/RESTARTS/IP/NODE/NOMINATED NODE/READINESS GATES etc.

    Namespaces: if the user did not pass ``-n``/``--namespace``/``-A``, the
    output is single-namespace and we drop any leading ``NAMESPACE`` column.
    """

    _CMD_RE = re.compile(r"^\s*kubectl\s+get\s+(pods?|services?|svc|deployments?|deploy)\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        lines = [line.rstrip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            return ""
        header = lines[0]
        # Split header by 2+ spaces to learn column names
        col_names = re.split(r"\s{2,}", header.strip())
        if not col_names or col_names[0] not in {"NAME", "NAMESPACE"}:
            return stdout

        # kubectl only prefixes a NAMESPACE column when the user passed -A or
        # --all-namespaces (or in some -n-vs-context combos). When it's there
        # the agent asked for it, so we keep it. Otherwise drop entirely.
        keep_ns = col_names[0] == "NAMESPACE"

        # Decide which columns to keep based on resource shape
        if "STATUS" in col_names:  # pods
            wanted = ["NAME", "STATUS", "AGE"]
        elif "TYPE" in col_names and "CLUSTER-IP" in col_names:  # services
            wanted = ["NAME", "TYPE", "CLUSTER-IP", "AGE"]
        elif "READY" in col_names and "UP-TO-DATE" in col_names:  # deployments
            wanted = ["NAME", "READY", "AGE"]
        else:
            wanted = col_names

        if keep_ns and "NAMESPACE" not in wanted:
            wanted = ["NAMESPACE", *wanted]

        # kubectl rows can have variable whitespace (longer status names push
        # neighbours rightward), so split each row by whitespace and zip with
        # the header column names. This is robust to ``CrashLoopBackOff`` etc.
        out_lines = ["  ".join(wanted)]
        wanted_set = set(wanted)
        for row in lines[1:]:
            cells = row.split()
            if len(cells) < len(col_names):
                continue
            mapping = dict(zip(col_names, cells))
            out_lines.append("  ".join(mapping.get(n, "") for n in wanted if n in wanted_set))
        return "\n".join(out_lines)


class KubectlLogsCompactor(Compactor):
    """Dedupe repeated log lines (same docker logic, applied to kubectl)."""

    _CMD_RE = re.compile(r"^\s*kubectl\s+logs\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    @staticmethod
    def _normalize(line: str) -> str:
        # Strip ISO timestamp prefix kubectl injects with --timestamps
        return re.sub(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z?\s*",
            "",
            line,
        )

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = stdout + (("\n" + stderr) if stderr else "")
        out: list[str] = []
        prev_norm: str | None = None
        prev_raw: str | None = None
        count = 0
        for raw in haystack.splitlines():
            norm = self._normalize(raw)
            if norm == prev_norm:
                count += 1
                continue
            if prev_raw is not None:
                out.append(f"{prev_raw} (x{count})" if count > 1 else prev_raw)
            prev_raw = raw
            prev_norm = norm
            count = 1
        if prev_raw is not None:
            out.append(f"{prev_raw} (x{count})" if count > 1 else prev_raw)
        return "\n".join(out)
