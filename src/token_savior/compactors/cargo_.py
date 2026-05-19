"""Cargo (test/build/clippy) compactor."""
from __future__ import annotations

import re

from .base import Compactor


class CargoTestCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*cargo\s+test\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = stdout + ("\n" + stderr if stderr else "")
        lines = haystack.splitlines()
        # Two passes: collect failure-stdout blocks (between `---- X stdout ----`
        # markers) and keep the per-test FAILED line + the test-result summary.
        out: list[str] = []
        in_failure_body = False
        failure_name: str | None = None
        in_second_failures_list = False
        for raw in lines:
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                if in_second_failures_list:
                    in_second_failures_list = False
                continue
            if stripped.startswith("Compiling ") or stripped.startswith("Finished ") or stripped.startswith("Running ") or stripped.startswith("running "):
                continue
            if re.match(r"^test\s+\S+\s+\.\.\.\s+ok$", stripped):
                continue
            if stripped == "failures:":
                in_second_failures_list = True
                continue
            if in_second_failures_list:
                continue
            m = re.match(r"^----\s+(\S+)\s+stdout\s+----$", stripped)
            if m:
                failure_name = m.group(1)
                in_failure_body = True
                out.append(f"# {failure_name}")
                continue
            if in_failure_body and stripped.startswith("thread '"):
                # Extract panic message; the rest of the panic line is redundant
                out.append(stripped)
                continue
            if stripped.startswith("test result:"):
                in_failure_body = False
                out.append(stripped)
                continue
            if "FAILED" in stripped and re.match(r"^test\s+\S+\s+\.\.\.\s+FAILED$", stripped):
                out.append(stripped)
                continue
            if in_failure_body:
                out.append(stripped)
        return "\n".join(out)


class CargoBuildCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*cargo\s+(build|check|clippy|run)\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        haystack = stdout + ("\n" + stderr if stderr else "")
        lines = haystack.splitlines()
        out: list[str] = []
        for raw in lines:
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("Compiling ") or stripped.startswith("Finished ") or stripped.startswith("Building "):
                continue
            # Error/warning header (e.g. `error[E0308]: mismatched types`)
            if stripped.startswith("error") or stripped.startswith("warning:") or stripped.startswith("note:") or stripped.startswith("help:"):
                out.append(stripped)
                continue
            # Location pointer (e.g. `--> src/main.rs:12:18`) — keep, it tells the agent where
            if stripped.startswith("-->"):
                out.append(stripped)
                continue
            # Final summary
            if "aborting due to" in stripped:
                out.append(stripped)
                continue
            # Drop ASCII pointer/context lines: pure `|`, `^^^`, numbered source lines, etc.
        return "\n".join(out)
