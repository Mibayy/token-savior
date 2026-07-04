"""Token Savior — structural code indexer with MCP server for AI-assisted development."""

# Single source of truth is pyproject.toml; read it from installed metadata so
# __version__ never drifts (it sat stale at "3.4.0" through the whole v4.x line,
# which masked that a stale build was installed -- audit 2026-07-04).
try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("token-savior-recall")
except Exception:  # not installed (e.g. running straight from a source tree)
    __version__ = "0.0.0+unknown"
