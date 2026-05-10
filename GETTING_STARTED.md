# Getting Started

## Install

```bash
git clone https://github.com/Mibayy/token-savior
cd token-savior
python3 -m venv .venv
.venv/bin/pip install -e ".[mcp]"
```

## Add To Claude Code

Register the MCP server with Claude Code:

```bash
claude mcp add token-savior-recall /absolute/path/to/token-savior/.venv/bin/token-savior \
  -s user \
  -e WORKSPACE_ROOTS=/absolute/path/to/your/project \
  -e TOKEN_SAVIOR_CLIENT=claude-code
```

For multiple projects, use a comma-separated `WORKSPACE_ROOTS` value:

```bash
-e WORKSPACE_ROOTS=/path/project1,/path/project2
```

## Verify

```bash
claude mcp list
```

You should see:

```text
token-savior-recall: ... - ✓ Connected
```

Restart Claude Code after adding the server. In Claude Code, run `/mcp` to
confirm the server is available.

## Add Workspace Roots

`WORKSPACE_ROOTS` is a comma-separated list of absolute project paths. To add or
change roots, re-register the server with the full list:

```bash
claude mcp remove token-savior-recall -s user

claude mcp add token-savior-recall /absolute/path/to/token-savior/.venv/bin/token-savior \
  -s user \
  -e WORKSPACE_ROOTS=/path/project1,/path/project2,/path/project3 \
  -e TOKEN_SAVIOR_CLIENT=claude-code
```

If a path contains spaces, quote the whole env argument:

```bash
-e 'WORKSPACE_ROOTS=/path/project1,/path/Project With Spaces'
```

You can also append roots to an existing `token-savior-recall` Claude Code
registration with the helper scripts:

```bash
scripts/add-token-savior-roots.sh /absolute/path/to/project [/another/project]
```

```powershell
.\scripts\add-token-savior-roots.ps1 C:\absolute\path\to\project [C:\another\project]
```

From the workspace you want to add, pass the current directory.

```bash
/absolute/path/to/token-savior/scripts/add-token-savior-roots.sh "$PWD"
```

```powershell
& C:\path\to\token-savior\scripts\add-token-savior-roots.ps1 $PWD.Path
```

The scripts require absolute paths. In Bash, `"$PWD"` is absolute in a normal
shell and quoting it handles spaces in directory names. In PowerShell, use
`$PWD.Path` to pass the current directory as a string.

Then restart Claude Code or restart the MCP server from `/mcp`.

## First Use In A Workspace

Token Savior registers `WORKSPACE_ROOTS` when the MCP server starts, but it
indexes projects lazily. A fresh workspace does not need a manual setup step;
the first index-backed tool call builds the index for that project.

Good first calls are:

```text
list_projects
switch_project(name="/absolute/path/to/project")
list_files(project="/absolute/path/to/project")
find_symbol(name="SomeSymbol", project="/absolute/path/to/project")
```

After that, use Claude Code normally. Token Savior is available on demand when
the agent chooses tools such as `find_symbol`, `search_codebase`,
`get_full_context`, or `list_files`. It does not replace ordinary reads or
greps automatically unless those MCP tools are used.

Set `TS_WARM_START=1` only if you want Token Savior to build indexes at startup
instead of waiting for first use.

## Common Gotcha

Do not rely on manually adding `mcpServers` to `~/.claude/settings.json`.
Current Claude Code user MCP registrations are stored in `~/.claude.json`.
Use `claude mcp add` so Claude writes the correct registry entry.
