"""Bash output compactors — opt-in compression layer for the tool-capture hook.

Each compactor is a pure function (no I/O, no globals) that recognizes a
known command family and returns a token-efficient rendering of the output.
The dispatcher returns ``None`` when no compactor matches, leaving the
existing sandbox path untouched.
"""
from __future__ import annotations

# F1b — cloud/package compactors (v4.2.0)
from .aws import (
    AwsDynamoDbScanCompactor,
    AwsEc2DescribeInstancesCompactor,
    AwsIamListRolesCompactor,
    AwsLambdaListFunctionsCompactor,
    AwsLogsGetLogEventsCompactor,
    AwsS3LsCompactor,
    AwsStsIdentityCompactor,
)
from .base import Compactor, CompactResult
from .biome import BiomeCompactor
from .cargo_ import CargoBuildCompactor, CargoTestCompactor

# v4.3.0 F3b additions — grep / find / cat compactors
from .cat_ import CatCompactor
from .compound import pick_meaningful_segment
from .curl import CurlCompactor
from .docker import DockerLogsCompactor, DockerPsCompactor
from .eslint import EslintCompactor
from .find_ import FindCompactor
from .gh import (
    GhIssueViewCompactor,
    GhPrDiffCompactor,
    GhPrViewCompactor,
    GhRepoViewCompactor,
    GhRunListCompactor,
    GhRunViewCompactor,
)
from .git import (
    GitAddCompactor,
    GitBranchCompactor,
    GitCheckoutCompactor,
    GitCommitCompactor,
    GitDiffCompactor,
    GitFetchCompactor,
    GitLogCompactor,
    GitPushPullCompactor,
    GitStashListCompactor,
    GitStatusCompactor,
    GitWorktreeListCompactor,
)
from .grep_ import GrepCompactor
from .jest import JestCompactor
from .kubectl import KubectlGetCompactor, KubectlLogsCompactor
from .pkg_list import NpmListCompactor, PipListCompactor
from .pytest_ import PytestCompactor
from .tsc import TscCompactor
from .vitest import VitestCompactor

# Order matters: more-specific patterns first so `gh run view` does not
# fall through to a hypothetical generic `gh` matcher.
registry: list[Compactor] = [
    # v4.3.0 F3a — gh extras MUST sit before GhRun* so the more-specific
    # `gh pr diff` / `gh pr view` / `gh issue view` / `gh repo view`
    # forms win over any future generic `gh` fallback. `gh pr diff` is
    # listed before `gh pr view` so it cannot be swallowed.
    GhPrDiffCompactor(),
    GhPrViewCompactor(),
    GhIssueViewCompactor(),
    GhRepoViewCompactor(),
    GhRunViewCompactor(),
    GhRunListCompactor(),
    GitStatusCompactor(),
    GitDiffCompactor(),
    GitLogCompactor(),
    GitPushPullCompactor(),
    GitCommitCompactor(),
    GitAddCompactor(),
    PytestCompactor(),
    CargoTestCompactor(),
    CargoBuildCompactor(),
    TscCompactor(),
    DockerPsCompactor(),
    DockerLogsCompactor(),
    # test/lint compactors (v4.2.0)
    JestCompactor(),
    VitestCompactor(),
    EslintCompactor(),
    BiomeCompactor(),
    # AWS — most-specific subcommands first
    AwsStsIdentityCompactor(),
    AwsEc2DescribeInstancesCompactor(),
    AwsLambdaListFunctionsCompactor(),
    AwsLogsGetLogEventsCompactor(),
    AwsIamListRolesCompactor(),
    AwsDynamoDbScanCompactor(),
    AwsS3LsCompactor(),
    # kubectl
    KubectlGetCompactor(),
    KubectlLogsCompactor(),
    # package managers
    NpmListCompactor(),
    PipListCompactor(),
    # curl
    CurlCompactor(),
    # v4.3.0 — git extras (existing GitPushPull/GitAdd matchers were
    # narrowed to no longer claim `fetch` / `checkout` so these dedicated
    # compactors actually fire).
    GitFetchCompactor(),
    GitCheckoutCompactor(),
    GitBranchCompactor(),
    GitWorktreeListCompactor(),
    GitStashListCompactor(),
    # v4.3.0 — generic shell catch-alls (last in registry on purpose)
    GrepCompactor(),
    FindCompactor(),
    CatCompactor(),
]


def _try_compact(
    command: str,
    stdout: str,
    stderr: str,
    *,
    original: str,
    original_bytes: int,
) -> CompactResult | None:
    """Run the registry against ``command``; return a CompactResult or None.

    ``original`` / ``original_bytes`` are passed through so the result
    reports the unmodified shell output, even when the dispatcher matched
    against a stripped sub-segment of a compound command.
    """
    for c in registry:
        if c.matches(command):
            text = c.compact(stdout, stderr)
            if not text.strip():
                # A matched compactor that renders non-empty output as nothing
                # failed to parse it (e.g. an unrecognized output format).
                # Reporting "" as a ~100% saving would tell the agent the
                # command produced no output — fail open to the sandbox path.
                return None
            compact_bytes = len(text.encode("utf-8"))
            savings = 100.0 * (1.0 - compact_bytes / max(1, original_bytes))
            return CompactResult(
                text=text,
                original_bytes=original_bytes,
                compact_bytes=compact_bytes,
                savings_pct=savings,
                original_text=original,
            )
    return None


def compact(command: str, stdout: str, stderr: str = "") -> CompactResult | None:
    if not command or not (stdout or stderr):
        return None
    original = (stdout or "") + (stderr or "")
    original_bytes = len(original.encode("utf-8"))
    if original_bytes == 0:
        return None
    result = _try_compact(
        command,
        stdout,
        stderr,
        original=original,
        original_bytes=original_bytes,
    )
    if result is not None:
        return result
    # F3c — compound command splitting: if the raw command did not match
    # any compactor, try the last meaningful segment of the chain.
    segment = pick_meaningful_segment(command)
    if segment and segment != command:
        return _try_compact(
            segment,
            stdout,
            stderr,
            original=original,
            original_bytes=original_bytes,
        )
    return None


__all__ = [
    "CompactResult",
    "Compactor",
    "compact",
    "pick_meaningful_segment",
    "registry",
]
