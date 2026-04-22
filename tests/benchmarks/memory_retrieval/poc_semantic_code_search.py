"""POC: semantic code search over the token-savior source tree.

Demonstrates the architecture for `search_codebase(semantic=True)` without
touching production code. Builds an ad-hoc in-memory index of the project's
Python symbols, embeds each, then ranks 5 descriptive queries.

    python tests/benchmarks/memory_retrieval/poc_semantic_code_search.py

Intentionally independent of the MCP server — this is a design proof, not
an integration. If the numbers look good, we wire the real thing via the
design in docs/design-semantic-code-tools.md.
"""
from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

SRC = Path("/root/token-savior/src/token_savior")

# Descriptive queries (no keyword overlap with target symbol names)
QUERIES = [
    {
        "query": "function that removes duplicate memory observations",
        "expect": ["dedup", "duplicate", "deduplicate"],
    },
    {
        "query": "convert a plain text into a dense vector embedding",
        "expect": ["embed"],
    },
    {
        "query": "fuse two ranked lists of search results",
        "expect": ["rrf_merge", "rrf"],
    },
    {
        "query": "detect strongly-connected components in an import graph",
        "expect": ["find_import_cycles", "tarjan", "scc"],
    },
    {
        "query": "backfill missing vector rows for existing observations",
        "expect": ["backfill_obs_vectors", "backfill"],
    },
]


def _extract_symbols(py_path: Path) -> list[dict]:
    """Yield {kind, name, qname, docstring, body_head, lineno} for every
    function / class / method in ``py_path``. Best-effort parse; silent
    failure on weird syntax."""
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    out: list[dict] = []

    def _visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name
                qname = f"{prefix}{name}" if prefix else name
                kind = "class" if isinstance(child, ast.ClassDef) else "func"
                doc = ast.get_docstring(child) or ""
                # First 3 non-blank non-docstring lines as body hint
                src_lines = ast.unparse(child).splitlines()
                body_head: list[str] = []
                seen_doc = False
                for line in src_lines[1:]:  # skip signature line
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.startswith(('"""', "'''")) and not seen_doc:
                        seen_doc = True
                        continue
                    body_head.append(stripped)
                    if len(body_head) >= 3:
                        break
                out.append({
                    "kind": kind, "name": name, "qname": qname,
                    "file": str(py_path.relative_to(Path("/root/token-savior"))),
                    "lineno": child.lineno, "docstring": doc,
                    "body_head": "\n".join(body_head),
                })
                if isinstance(child, ast.ClassDef):
                    _visit(child, prefix=f"{qname}.")

    _visit(tree)
    return out


def _build_doc(sym: dict) -> str:
    """Format a symbol for embedding input. Keeps it tight: kind + name +
    docstring (first 2 lines) + body head. Mirrors what the real indexer
    would feed to the embedder.
    """
    doc_head = "\n".join(sym["docstring"].splitlines()[:2])
    return (
        f"search_document: {sym['kind']} {sym['qname']}\n"
        f"{doc_head}\n"
        f"{sym['body_head']}"
    ).strip()


def _cosine(a, b) -> float:
    # Vectors are already L2-normalized by our embed() helper, so dot = cos.
    return sum(x * y for x, y in zip(a, b, strict=False))


def _embed_query(text: str, embed_fn) -> list[float]:
    return embed_fn(text, as_query=True)


def main() -> None:
    sys.path.insert(0, "/root/token-savior/src")
    from token_savior.memory.embeddings import embed

    # Collect symbols
    t0 = time.perf_counter()
    syms: list[dict] = []
    for py in SRC.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        syms.extend(_extract_symbols(py))
    collect_s = time.perf_counter() - t0
    print(f"[poc] collected {len(syms)} symbols from {SRC} in {collect_s:.1f}s")

    # Embed all
    t0 = time.perf_counter()
    # FastEmbed is faster in batches; but we call embed() per-symbol here
    # to match the real API shape. Batch optimisation is part of the real
    # implementation, not this POC.
    for sym in syms:
        sym["doc"] = _build_doc(sym)
        sym["vec"] = embed(sym["doc"])
    embed_s = time.perf_counter() - t0
    vecless = sum(1 for s in syms if s["vec"] is None)
    print(f"[poc] embedded {len(syms)} symbols in {embed_s:.1f}s "
          f"(P50 {embed_s * 1000 / max(1, len(syms)):.0f} ms/sym, "
          f"{vecless} empty)")

    # Run queries
    print()
    print("## Retrieval quality on descriptive queries\n")
    print("| Query | Top-1 | Top-1 score | Expect-hit in top-5 |")
    print("|---|---|---|---|")
    hits = 0
    for q in QUERIES:
        qvec = _embed_query(q["query"], embed)
        if qvec is None:
            continue
        scored = [
            (_cosine(qvec, s["vec"]), s)
            for s in syms if s["vec"] is not None
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        top5 = scored[:5]
        top1_score, top1_sym = top5[0]
        expect_terms = [t.lower() for t in q["expect"]]
        hit = any(
            any(term in s["qname"].lower() for term in expect_terms)
            for _, s in top5
        )
        if hit:
            hits += 1
        print(f"| {q['query'][:50]} | `{top1_sym['qname']}` | "
              f"{top1_score:.3f} | {'✅' if hit else '❌'} |")

    print()
    print(f"**Hit rate (expect term in top-5): {hits}/{len(QUERIES)}**")
    print()
    print("Detailed top-3 per query:")
    for q in QUERIES:
        qvec = _embed_query(q["query"], embed)
        if qvec is None:
            continue
        scored = [
            (_cosine(qvec, s["vec"]), s)
            for s in syms if s["vec"] is not None
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        print(f"\n### {q['query']}")
        for score, s in scored[:3]:
            print(f"  {score:.3f}  {s['qname']:60s}  {s['file']}:{s['lineno']}")


if __name__ == "__main__":
    main()
