#!/usr/bin/env bash
set -euo pipefail

SERVER_NAME="token-savior-recall"
SCOPE="user"
CLIENT_NAME="claude-code"

usage() {
  cat <<'USAGE'
Usage:
  scripts/add-token-savior-roots.sh /absolute/project/path [/another/project/path ...]

Adds one or more project folders to the token-savior-recall Claude Code MCP
server by updating its WORKSPACE_ROOTS environment setting.

From the workspace you want to add:
  scripts/add-token-savior-roots.sh "$PWD"
USAGE
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "Error: claude CLI not found on PATH." >&2
  exit 1
fi

server_info="$(claude mcp get "$SERVER_NAME" 2>/dev/null || true)"
if [[ -z "$server_info" ]]; then
  echo "Error: MCP server '$SERVER_NAME' is not registered." >&2
  echo "Register it first with: claude mcp add $SERVER_NAME /path/to/token-savior" >&2
  exit 1
fi

command_path="$(printf '%s\n' "$server_info" | sed -n 's/^  Command: //p' | head -n 1)"
if [[ -z "$command_path" ]]; then
  echo "Error: could not find command path for '$SERVER_NAME'." >&2
  exit 1
fi

current_roots="$(printf '%s\n' "$server_info" | sed -n 's/^    WORKSPACE_ROOTS=//p' | head -n 1)"

declare -a roots=()
if [[ -n "$current_roots" ]]; then
  IFS=',' read -r -a roots <<< "$current_roots"
fi

for root in "$@"; do
  if [[ "$root" != /* ]]; then
    echo "Error: root must be an absolute path: $root" >&2
    exit 1
  fi
  if [[ ! -d "$root" ]]; then
    echo "Error: root is not a directory: $root" >&2
    exit 1
  fi
  roots+=("$root")
done

declare -A seen=()
declare -a unique_roots=()
for root in "${roots[@]}"; do
  [[ -z "$root" ]] && continue
  if [[ -z "${seen[$root]+x}" ]]; then
    seen[$root]=1
    unique_roots+=("$root")
  fi
done

joined_roots="$(IFS=','; printf '%s' "${unique_roots[*]}")"

claude mcp remove "$SERVER_NAME" -s "$SCOPE" >/dev/null
claude mcp add "$SERVER_NAME" "$command_path" \
  -s "$SCOPE" \
  -e "WORKSPACE_ROOTS=$joined_roots" \
  -e "TOKEN_SAVIOR_CLIENT=$CLIENT_NAME" >/dev/null

echo "Updated $SERVER_NAME WORKSPACE_ROOTS:"
printf '  %s\n' "${unique_roots[@]}"
echo
echo "Restart Claude Code or restart the MCP server from /mcp."
