"""AWS CLI output compactors.

AWS CLI defaults to JSON. We parse and re-emit a minimal table or one-liner
per common subcommand. If JSON parsing fails (e.g. ``--output text``), we
fall back to returning the raw output unchanged.

Subcommand priority (most-specific first):

    aws sts get-caller-identity
    aws ec2 describe-instances
    aws lambda list-functions
    aws logs get-log-events
    aws iam list-roles
    aws dynamodb scan
    aws s3 ls
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base import Compactor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _try_json(stdout: str) -> Any | None:
    if not stdout.strip():
        return None
    try:
        return json.loads(stdout)
    except (ValueError, json.JSONDecodeError):
        return None


def _unwrap_ddb(value: Any) -> Any:
    """Recursively unwrap DynamoDB type-annotated JSON.

    ``{"S": "foo"}``      -> ``"foo"``
    ``{"N": "42"}``       -> ``42`` (or string if not parseable)
    ``{"BOOL": true}``    -> ``True``
    ``{"NULL": true}``    -> ``None``
    ``{"L": [...]}``      -> recursive list
    ``{"M": {...}}``      -> recursive dict
    Plain dicts (no DDB tag) recurse over values.
    """
    if isinstance(value, dict):
        if len(value) == 1:
            (tag, inner), = value.items()
            if tag == "S":
                return inner
            if tag == "N":
                try:
                    if "." in inner:
                        return float(inner)
                    return int(inner)
                except (ValueError, TypeError):
                    return inner
            if tag == "BOOL":
                return bool(inner)
            if tag == "NULL":
                return None
            if tag == "L" and isinstance(inner, list):
                return [_unwrap_ddb(item) for item in inner]
            if tag == "M" and isinstance(inner, dict):
                return {k: _unwrap_ddb(v) for k, v in inner.items()}
            if tag in {"SS", "NS", "BS"} and isinstance(inner, list):
                return inner
        return {k: _unwrap_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_ddb(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Specific compactors (registered most-specific first in __init__.py)
# ---------------------------------------------------------------------------


class AwsStsIdentityCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*aws\s+sts\s+get-caller-identity\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        data = _try_json(stdout)
        if not isinstance(data, dict):
            return stdout.strip()
        account = data.get("Account", "")
        arn = data.get("Arn", "")
        user = data.get("UserId", "")
        return f"account={account} arn={arn} user={user}"


class AwsEc2DescribeInstancesCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*aws\s+ec2\s+describe-instances\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        data = _try_json(stdout)
        if not isinstance(data, dict):
            return stdout
        rows = ["InstanceId  State  Type  LaunchTime  Name"]
        for reservation in data.get("Reservations", []) or []:
            for inst in reservation.get("Instances", []) or []:
                iid = inst.get("InstanceId", "")
                state = (inst.get("State") or {}).get("Name", "")
                itype = inst.get("InstanceType", "")
                launch = inst.get("LaunchTime", "")
                name = ""
                for tag in inst.get("Tags", []) or []:
                    if tag.get("Key") == "Name":
                        name = tag.get("Value", "")
                        break
                rows.append(f"{iid}  {state}  {itype}  {launch}  {name}")
        if len(rows) == 1:
            return "no instances"
        return "\n".join(rows)


class AwsLambdaListFunctionsCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*aws\s+lambda\s+list-functions\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        data = _try_json(stdout)
        if not isinstance(data, dict):
            return stdout
        rows = ["FunctionName  Runtime  MemorySize  LastModified"]
        for fn in data.get("Functions", []) or []:
            rows.append(
                f"{fn.get('FunctionName', '')}  "
                f"{fn.get('Runtime', '')}  "
                f"{fn.get('MemorySize', '')}  "
                f"{fn.get('LastModified', '')}"
            )
        if len(rows) == 1:
            return "no functions"
        return "\n".join(rows)


class AwsLogsGetLogEventsCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*aws\s+logs\s+get-log-events\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        data = _try_json(stdout)
        if not isinstance(data, dict):
            return stdout
        out: list[str] = []
        for ev in data.get("events", []) or []:
            ts = ev.get("timestamp", "")
            msg = (ev.get("message") or "").rstrip("\n")
            out.append(f"{ts} {msg}")
        return "\n".join(out)


class AwsIamListRolesCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*aws\s+iam\s+list-roles\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        data = _try_json(stdout)
        if not isinstance(data, dict):
            return stdout
        rows = ["RoleName  Arn  CreateDate"]
        for role in data.get("Roles", []) or []:
            rows.append(
                f"{role.get('RoleName', '')}  "
                f"{role.get('Arn', '')}  "
                f"{role.get('CreateDate', '')}"
            )
        if len(rows) == 1:
            return "no roles"
        return "\n".join(rows)


class AwsDynamoDbScanCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*aws\s+dynamodb\s+(scan|query|get-item)\b")

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        data = _try_json(stdout)
        if not isinstance(data, dict):
            return stdout
        # Items is the chunky bit — unwrap type annotations
        items = data.get("Items")
        if items is not None:
            data = {**data, "Items": [_unwrap_ddb(it) for it in items]}
        # LastEvaluatedKey can also carry DDB type tags
        if "LastEvaluatedKey" in data:
            data["LastEvaluatedKey"] = _unwrap_ddb(data["LastEvaluatedKey"])
        if "Item" in data:
            data["Item"] = _unwrap_ddb(data["Item"])
        return json.dumps(data, separators=(",", ":"), default=str)


class AwsS3LsCompactor(Compactor):
    _CMD_RE = re.compile(r"^\s*aws\s+s3\s+ls\b")
    _HEAD_KEEP = 30
    _TAIL_KEEP = 20
    _THRESHOLD = 50

    def matches(self, command: str) -> bool:
        return bool(self._CMD_RE.search(command))

    def compact(self, stdout: str, stderr: str = "") -> str:
        lines = [line for line in stdout.splitlines() if line.strip()]
        if len(lines) <= self._THRESHOLD:
            return "\n".join(lines)
        skipped = len(lines) - self._HEAD_KEEP - self._TAIL_KEEP
        out = lines[: self._HEAD_KEEP]
        out.append(f"... {skipped} items skipped ...")
        out.extend(lines[-self._TAIL_KEEP :])
        return "\n".join(out)
