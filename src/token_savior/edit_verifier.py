"""Proof-Carrying Edits: produce an EditSafety certificate before mutation.

Pure static analysis -- no LLM, no runtime. We answer four cheap questions
before allowing a symbol replacement to land:

1. Is the public signature preserved?
2. Are tests available for this symbol (so a regression would be caught)?
3. Is the set of raised exceptions unchanged?
4. Are external side effects (open/print/subprocess/...) unchanged?

If all four hold, the edit is "SAFE TO APPLY". Otherwise the agent sees a
"REVIEW REQUIRED" certificate listing exactly which invariant moved.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass


_SIDE_EFFECT_RE = re.compile(
    r"\b(open|print|requests\.|subprocess\.|os\.system|socket\.|shutil\.|"
    r"urllib\.|httpx\.|smtplib\.|threading\.|multiprocessing\.|asyncio\.create_subprocess|"
    r"input)\b"
)
_RAISE_RE = re.compile(r"\braise\s+(\w+)")


@dataclass
class EditSafety:
    signature_preserved: bool
    signature_diff: str        # empty if preserved
    tests_available: bool
    exceptions_unchanged: bool
    exceptions_diff: str
    side_effects_unchanged: bool
    all_ok: bool

    def format(self) -> str:
        lines = ["EditSafety Certificate:"]
        sig_msg = (
            "preserved"
            if self.signature_preserved
            else f"CHANGED -- {self.signature_diff}"
        )
        lines.append(f"  Signature    : {sig_msg}")
        lines.append(
            f"  Tests        : {'available' if self.tests_available else 'none found'}"
        )
        exc_msg = (
            "unchanged"
            if self.exceptions_unchanged
            else f"CHANGED -- {self.exceptions_diff}"
        )
        lines.append(f"  Exceptions   : {exc_msg}")
        lines.append(
            f"  Side-effects : {'unchanged' if self.side_effects_unchanged else 'may have changed'}"
        )
        status = "SAFE TO APPLY" if self.all_ok else "REVIEW REQUIRED"
        lines.append(f"  -> {status}")
        return "\n".join(lines)


def _extract_signature(source: str) -> str:
    """Return ``name(params)`` for the first def found, AST-first then regex fallback."""
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params = [a.arg for a in node.args.args]
                if node.args.vararg:
                    params.append(f"*{node.args.vararg.arg}")
                for a in node.args.kwonlyargs:
                    params.append(a.arg)
                if node.args.kwarg:
                    params.append(f"**{node.args.kwarg.arg}")
                return f"{node.name}({', '.join(params)})"
    except SyntaxError:
        pass
    m = re.search(r"def\s+(\w+)\s*\(([^)]*)\)", source)
    return f"{m.group(1)}({m.group(2).strip()})" if m else ""


def _tests_exist(symbol_name: str, project_root: str) -> bool:
    """Cheap heuristic: does any obvious test file exist for *symbol_name*?"""
    if not project_root or not os.path.isdir(project_root):
        return False
    base = symbol_name.split(".")[-1]
    candidates = [
        f"tests/test_{base}.py",
        f"tests/{base}_test.py",
        f"test_{base}.py",
        f"tests/test_{base.lower()}.py",
    ]
    for c in candidates:
        if os.path.exists(os.path.join(project_root, c)):
            return True
    # Last-resort: grep test files for "def test_<base>" or "Test<Base>".
    tests_dir = os.path.join(project_root, "tests")
    if os.path.isdir(tests_dir):
        needles = [f"def test_{base}", f"def test_{base.lower()}", f"Test{base.title()}"]
        for root, _dirs, files in os.walk(tests_dir):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                try:
                    with open(os.path.join(root, fn), "r", encoding="utf-8", errors="ignore") as fh:
                        content = fh.read()
                except OSError:
                    continue
                if any(n in content for n in needles):
                    return True
    return False


def verify_edit(
    old_source: str,
    new_source: str,
    symbol_name: str,
    project_root: str,
) -> EditSafety:
    """Build the EditSafety certificate for an in-flight symbol replacement."""
    old_sig = _extract_signature(old_source)
    new_sig = _extract_signature(new_source)
    sig_preserved = old_sig == new_sig and old_sig != ""
    sig_diff = "" if sig_preserved else f"{old_sig} -> {new_sig}"

    tests_available = _tests_exist(symbol_name, project_root)

    old_raises = set(_RAISE_RE.findall(old_source))
    new_raises = set(_RAISE_RE.findall(new_source))
    exc_unchanged = old_raises == new_raises
    if exc_unchanged:
        exc_diff = ""
    else:
        added = sorted(new_raises - old_raises)
        removed = sorted(old_raises - new_raises)
        parts: list[str] = []
        if removed:
            parts.append(f"removed {removed}")
        if added:
            parts.append(f"added {added}")
        exc_diff = ", ".join(parts) or "differing"

    old_effects = bool(_SIDE_EFFECT_RE.search(old_source))
    new_effects = bool(_SIDE_EFFECT_RE.search(new_source))
    effects_unchanged = old_effects == new_effects

    all_ok = sig_preserved and exc_unchanged and effects_unchanged

    return EditSafety(
        signature_preserved=sig_preserved,
        signature_diff=sig_diff,
        tests_available=tests_available,
        exceptions_unchanged=exc_unchanged,
        exceptions_diff=exc_diff,
        side_effects_unchanged=effects_unchanged,
        all_ok=all_ok,
    )
