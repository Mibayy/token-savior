"""Hook entrypoints. In the wheel this directory ships as token_savior.hooks
(pyproject force-include), so installed hooks run as
`python3 -m token_savior.hooks.<module>` — no interpreter-versioned
site-packages path in the agent settings to go stale on a venv rebuild."""
