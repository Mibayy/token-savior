"""AST-normalised semantic hashing for Python source.

Two functions that differ only in local-variable names should hash to the
same value. We achieve this by walking the AST, alpha-renaming local
identifiers to canonical ``_v0, _v1, ...`` slots, stripping docstrings,
and serialising the normalised tree.

Non-Python sources are handled gracefully: ``ast.parse`` raises
``SyntaxError`` and we fall back to a whitespace-collapsed text hash.
"""

from __future__ import annotations

import ast
import hashlib
import re
from typing import Any


class ASTNormalizer(ast.NodeTransformer):
    """Normalise an AST so that semantically equivalent code maps to the same dump.

    Steps:
    1. alpha-conversion of local identifiers (start with underscore or
       are entirely lowercase -- public CamelCase names are preserved
       as part of the API surface).
    2. Drop docstrings (string-only Expr at the top of a body).
    3. Reset the scope per FunctionDef so identical helpers reuse v0, v1...
    """

    def __init__(self) -> None:
        self.var_counter = 0
        self.var_map: dict[str, str] = {}

    def _canonical_name(self, name: str) -> str:
        # Preserve CamelCase / dunder / public ALL_CAPS as API-significant.
        if name.startswith("_") or name.islower():
            if name not in self.var_map:
                self.var_map[name] = f"_v{self.var_counter}"
                self.var_counter += 1
            return self.var_map[name]
        return name

    def visit_Name(self, node: ast.Name) -> ast.Name:
        node.id = self._canonical_name(node.id)
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.arg = self._canonical_name(node.arg)
        return node

    def visit_Expr(self, node: ast.Expr) -> Any:
        # Drop docstring-style standalone strings.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return None
        return self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        saved_map = self.var_map
        saved_counter = self.var_counter
        self.var_map = {}
        self.var_counter = 0
        result = self.generic_visit(node)
        self.var_map = saved_map
        self.var_counter = saved_counter
        return result  # type: ignore[return-value]

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]


def semantic_hash(source: str) -> str:
    """Return a 16-char hex digest of *source*'s normalised AST.

    On non-Python or syntactically broken input, falls back to a whitespace-
    collapsed text hash so callers always receive a stable identifier.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        normalised_text = re.sub(r"\s+", " ", source).strip()
        return hashlib.sha256(normalised_text.encode("utf-8")).hexdigest()[:16]

    normaliser = ASTNormalizer()
    normalised = normaliser.visit(tree)
    ast.fix_missing_locations(normalised)
    canonical = ast.dump(normalised, indent=None)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def are_semantically_equivalent(source_a: str, source_b: str) -> bool:
    """True iff *source_a* and *source_b* hash to the same semantic value."""
    return semantic_hash(source_a) == semantic_hash(source_b)
