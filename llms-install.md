# Installation

```bash
pip install "token-savior[mcp]"
```

# Configuration

Add to `.mcp.json`:

```json
{
  "mcpServers": {
    "token-savior": {
      "command": "token-savior",
      "env": {
        "WORKSPACE_ROOTS": "/path/to/project",
        "TOKEN_SAVIOR_CLIENT": "codex"
      }
    }
  }
}
```

Replace `/path/to/project` with one absolute path or a comma-separated list in `WORKSPACE_ROOTS`. Set `TOKEN_SAVIOR_CLIENT` to the MCP caller name you want to see in the dashboard, for example `codex` or `hermes`.
