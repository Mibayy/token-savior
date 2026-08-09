"""Symbol-level vector index for `search_codebase(semantic=True)`.

Mirrors the design of ``memory/embeddings.py`` but scoped to code symbols
parsed from a project tree rather than memory observations.

Schema (created in ``db_core.run_migrations``):

* ``symbols(id, project_root, symbol_key, file_path, lineno, kind,
  signature, docstring_head, content_hash, updated_at_epoch)``
* ``symbol_vectors(symbol_id INTEGER PK, embedding FLOAT[768])``

Flow:

1. ``collect_project_symbols(root)`` walks ``*.py`` files, extracts
   ``FunctionDef`` / ``AsyncFunctionDef`` / ``ClassDef`` via ``ast``, and
   returns descriptors suitable for embedding.
2. ``reindex_project_symbols(root)`` embeds new/changed symbols (batched)
   and upserts rows. Rows whose ``content_hash`` already matches the
   stored hash are skipped — cheap incremental reindex.
3. ``search_symbols_semantic(query, root, limit=10)`` embeds the query
   (``as_query=True`` prefix), runs k-NN against ``symbol_vectors``,
   joins ``symbols`` for metadata, returns hits with scores.

The module is a no-op when sqlite-vec or fastembed isn't loadable — the
caller gets ``status="unavailable"`` and should fall back to the regex
path. Same degradation contract as ``memory/embeddings.py``.
"""
from __future__ import annotations

import ast
import hashlib
import logging
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_BATCH_SIZE = 32
_MAX_DOC_CHARS = 1200
_EXCLUDE_DIRS = frozenset({
    "__pycache__", ".git", "node_modules", "dist", "build", ".next",
    "venv", ".venv", "env", ".tox",
})


# ---------------------------------------------------------------------------
# AST walker — produces one descriptor per function/class/method
# ---------------------------------------------------------------------------


def _iter_symbols_in_file(py_path: Path, project_root: Path) -> Iterable[dict]:
    try:
        source = py_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return
    rel_path = py_path.relative_to(project_root).as_posix()

    def _visit(node: ast.AST, prefix: str = "") -> Iterable[dict]:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            qname = f"{prefix}{child.name}" if prefix else child.name
            kind = "class" if isinstance(child, ast.ClassDef) else "func"
            doc = (ast.get_docstring(child) or "").strip()
            doc_head = "\n".join(doc.splitlines()[:2])

            try:
                sig_source = ast.unparse(child)
            except Exception:
                sig_source = ""
            sig_lines = sig_source.splitlines()
            signature = sig_lines[0] if sig_lines else f"{kind} {qname}"

            body_head: list[str] = []
            skipped_doc = False
            for line in sig_lines[1:]:
                stripped = line.strip()
                if not stripped:
                    continue
                if not skipped_doc and stripped.startswith(('"""', "'''")):
                    skipped_doc = True
                    continue
                body_head.append(stripped)
                if len(body_head) >= 3:
                    break
            body_excerpt = "\n".join(body_head)

            symbol_key = f"{rel_path}::{qname}"
            # ``embed()`` already prepends "search_document: " — we only
            # supply the raw payload. Double-prefix would confuse the
            # Nomic task router.
            embed_doc = (
                f"{kind} {qname}\n"
                f"{signature}\n"
                f"{doc_head}\n"
                f"{body_excerpt}"
            ).strip()[:_MAX_DOC_CHARS]
            content_hash = hashlib.sha1(
                embed_doc.encode("utf-8", "replace")
            ).hexdigest()[:16]

            yield {
                "project_root": str(project_root),
                "symbol_key": symbol_key,
                "file_path": rel_path,
                "lineno": child.lineno,
                "kind": kind,
                "signature": signature[:400],
                "docstring_head": doc_head[:400],
                "content_hash": content_hash,
                "embed_doc": embed_doc,
            }
            if isinstance(child, ast.ClassDef):
                yield from _visit(child, prefix=f"{qname}.")

    yield from _visit(tree)


def collect_project_symbols(project_root: str | Path) -> list[dict]:
    """Walk project_root for .py files and return symbol descriptors.

    Excludes common noise dirs (``__pycache__``, ``node_modules``, venvs).
    Silently skips files with syntax or encoding errors — collection
    should never fail the whole reindex over one bad file.
    """
    root = Path(project_root).resolve()
    if not root.is_dir():
        return []
    out: list[dict] = []
    for py in root.rglob("*.py"):
        if any(part in _EXCLUDE_DIRS for part in py.parts):
            continue
        out.extend(_iter_symbols_in_file(py, root))
    return out



# ---------------------------------------------------------------------------
# Collecte multi-langage, adossee a l'index structurel deja construit
# ---------------------------------------------------------------------------


def _tete_du_corps(lignes: list, debut: int) -> str:
    """Les trois premieres lignes non vides du corps, a partir de `debut`.

    Etait une fonction imbriquee qui capturait `lignes` depuis la boucle
    englobante. Elle n'etait appelee que dans l'iteration qui la definissait,
    donc sans consequence aujourd'hui, mais la capture d'une variable de boucle
    ne survit pas a un deplacement de l'appel (B023). Prendre `lignes` en
    parametre rend la dependance visible.
    """
    tete: list[str] = []
    for brute in lignes[debut : debut + 12]:
        nette = str(brute).strip()
        if nette:
            tete.append(nette)
        if len(tete) >= 3:
            break
    return "\n".join(tete)


def collect_symbols_from_index(project_root: str | Path, index) -> list[dict]:
    """Descripteurs de symboles pour TOUS les langages que le projet indexe.

    `collect_project_symbols` ne voyait que les fichiers `.py` : elle marche au
    `ast` de Python. Sur un projet TypeScript, l'index semantique se remplissait
    donc avec les rares scripts Python du depot, et la recherche rendait des
    resultats plausibles issus du mauvais langage, sans jamais dire que le
    langage principal n'etait pas indexe.

    Mesure du 06/08/2026 sur /root/estalle (305 fichiers TypeScript, 1 fichier
    Python) : `search_codebase(semantic=True)` trouvait 1 cible sur 6, et
    l'unique fichier qu'il rendait etait ce `tests/test_api.py`.

    Ici on ne reecrit aucun analyseur : l'index structurel connait deja les
    fonctions et les classes de chaque langage supporte. On se contente de
    fabriquer le meme document d'embedding a partir de lui.
    """
    root = Path(project_root).resolve()
    out: list[dict] = []
    fichiers = getattr(index, "files", None) or {}

    for rel_path, meta in fichiers.items():
        lignes = None
        try:
            lignes = list(getattr(meta, "lines", []) or [])
        except Exception:
            lignes = []

        elements = []
        for fn in getattr(meta, "functions", []) or []:
            elements.append(("function", fn, None))
        for cl in getattr(meta, "classes", []) or []:
            elements.append(("class", cl, None))
            for m in getattr(cl, "methods", []) or []:
                elements.append(("method", m, getattr(cl, "name", None)))

        for kind, el, parent in elements:
            nom = getattr(el, "qualified_name", None) or getattr(el, "name", "")
            if not nom:
                continue
            if parent and not str(nom).startswith(f"{parent}."):
                nom = f"{parent}.{nom}"
            portee = getattr(el, "line_range", None)
            debut = getattr(portee, "start", 0) or 0
            params = getattr(el, "parameters", None)
            signature = (
                f"{getattr(el, 'name', nom)}({', '.join(params)})"
                if params is not None
                else str(getattr(el, "name", nom))
            )
            doc_head = (getattr(el, "docstring", None) or "").strip().split("\n")[0][:200]

            # Le CHEMIN est en tete, et c'est le point qui fait la difference.
            # Un handler de route Next.js s'appelle `POST` : ni son nom ni sa
            # signature ne portent le moindre sens. Tout le sens est dans
            # `app/api/oodrive/webhook/route.ts`, qui contient justement les
            # mots de la question posee. Mesure du 06/08/2026 sur 3 questions
            # a verite terrain : 2 cibles sur 6 sans le chemin, 3 sur 6 avec.
            chemin_mots = str(rel_path).replace("/", " ").replace("_", " ").replace("-", " ")
            embed_doc = (
                f"{rel_path}\n"
                f"{chemin_mots}\n"
                f"{kind} {nom}\n"
                f"{signature}\n"
                f"{doc_head}\n"
                f"{_tete_du_corps(lignes, max(debut, 1) - 1 + 1)}"
            ).strip()[:_MAX_DOC_CHARS]

            out.append({
                "project_root": str(root),
                "symbol_key": f"{rel_path}::{nom}",
                "file_path": rel_path,
                "lineno": debut or 1,
                "kind": kind,
                "signature": signature[:400],
                "docstring_head": doc_head[:400],
                "content_hash": hashlib.sha1(
                    embed_doc.encode("utf-8", "replace")
                ).hexdigest()[:16],
                "embed_doc": embed_doc,
            })
    return out


# ---------------------------------------------------------------------------
# Indexing — batched embed + upsert into symbols / symbol_vectors
# ---------------------------------------------------------------------------


def _embed_batch(docs: list[str]) -> list[list[float] | None]:
    """Embed documents sequentially via the shared ``embed()`` helper.

    Sequential calls are used instead of ``model.embed(docs)`` because
    FastEmbed's ONNX backend holds onto intermediate buffers across a
    materialised batch — a single ``list(model.embed(100_docs))`` spiked
    this process past 3 GB RSS on 4 GB-available VPS and tripped the OOM
    killer. One-at-a-time keeps the peak at ~500 MB (matches the
    memory_retrieval bench measurements) at the cost of ~100 ms per
    symbol on CPU, which is fine for a one-off reindex.
    """
    from token_savior.memory.embeddings import embed
    return [embed(doc) for doc in docs]


def _serialize_vec(vec: list[float]) -> Any | None:
    try:
        import sqlite_vec
        return sqlite_vec.serialize_float32(vec)
    except Exception:
        return None


def reindex_project_symbols(
    project_root: str | Path,
    *,
    db_path: str | Path | None = None,
    batch_size: int = _BATCH_SIZE,
    index: Any = None,
) -> dict[str, Any]:
    """Collect, embed, and upsert all symbols in ``project_root``.

    Fast path: a symbol whose ``content_hash`` already matches the stored
    row is skipped (no re-embed). This makes re-running the indexer cheap
    when only a handful of files changed.

    Le lot est valide des qu'il est ecrit, et jamais pendant que le lot
    suivant s'embedde. Ce n'est pas la connexion qui verrouille la base,
    c'est la transaction : sqlite3 en ouvre une au premier INSERT et la
    garde jusqu'au commit. Sans commit par lot, le premier INSERT posait
    un verrou d'ecriture tenu pendant toute la boucle d'embedding, soit
    ~100 ms par symbole en sequentiel (voir _embed_batch, sequentiel par
    choix pour ne pas declencher l'OOM killer).

    Mesure du 26/07/2026 sur ce VPS : un `search_codebase(semantic=True)`
    a tenu la base memoire verrouillee plus de 25 minutes. Pendant ce
    temps aucun autre processus ne pouvait ecrire, un second client Token
    Savior ne pouvait plus demarrer du tout (run_migrations echouait sur
    "database is locked"), et le WAL est passe de 8 a 15 Mo sans jamais
    pouvoir etre resorbe. C'est le scenario multi-clients de la v4.11 qui
    tombait des qu'une recherche semantique tournait.

    Consequence assumee d'un commit par lot : une interruption en cours de
    route laisse l'index partiellement ecrit. C'est sans danger, le
    prochain passage repart du content_hash et ne re-embedde que ce qui
    manque -- l'alternative, tout perdre au rollback, etait pire.

    Returns a summary dict:
      * status     : "ok" | "unavailable"
      * total      : symbols seen
      * indexed    : symbols newly embedded and written
      * skipped    : symbols whose hash was unchanged
      * elapsed_s  : wall clock
      * reason     : filled when status != "ok"
    """
    from token_savior.db_core import VECTOR_SEARCH_AVAILABLE
    from token_savior.memory._facade import memory_db
    from token_savior.memory.embeddings import is_available

    if not VECTOR_SEARCH_AVAILABLE:
        return {"status": "unavailable", "reason": "sqlite-vec not loadable"}
    if not is_available():
        return {"status": "unavailable", "reason": "embedding model not loadable"}

    t0 = time.perf_counter()
    root_str = str(Path(project_root).resolve())
    # Avec l'index structurel on couvre tous les langages du projet ; sans
    # lui on retombe sur la marche `.py`, qui reste correcte pour un depot
    # Python et garde le comportement des appels historiques.
    symbols = (
        collect_symbols_from_index(root_str, index)
        if index is not None
        else collect_project_symbols(root_str)
    )
    now_epoch = int(time.time())

    indexed = 0
    skipped = 0
    to_embed: list[dict] = []

    def conn_factory():
        return memory_db.db_session(db_path) if db_path else memory_db.db_session()
    with conn_factory() as conn:
        existing = dict(conn.execute(
            "SELECT symbol_key, content_hash FROM symbols WHERE project_root=?",
            (root_str,),
        ).fetchall())

        for sym in symbols:
            if existing.get(sym["symbol_key"]) == sym["content_hash"]:
                skipped += 1
                continue
            to_embed.append(sym)

        for i in range(0, len(to_embed), batch_size):
            chunk = to_embed[i:i + batch_size]
            # Hors transaction : aucun verrou n'est tenu pendant l'embedding.
            vecs = _embed_batch([s["embed_doc"] for s in chunk])
            for sym, vec in zip(chunk, vecs, strict=False):
                if vec is None:
                    continue
                blob = _serialize_vec(vec)
                if blob is None:
                    continue
                cur = conn.execute(
                    "INSERT INTO symbols("
                    "  project_root, symbol_key, file_path, lineno, kind,"
                    "  signature, docstring_head, content_hash, updated_at_epoch"
                    ") VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(project_root, symbol_key) DO UPDATE SET "
                    "  file_path=excluded.file_path,"
                    "  lineno=excluded.lineno,"
                    "  kind=excluded.kind,"
                    "  signature=excluded.signature,"
                    "  docstring_head=excluded.docstring_head,"
                    "  content_hash=excluded.content_hash,"
                    "  updated_at_epoch=excluded.updated_at_epoch "
                    "RETURNING id",
                    (
                        sym["project_root"], sym["symbol_key"],
                        sym["file_path"], sym["lineno"], sym["kind"],
                        sym["signature"], sym["docstring_head"],
                        sym["content_hash"], now_epoch,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    continue
                symbol_id = row[0]
                # DELETE puis INSERT, et non "INSERT OR REPLACE".
                # symbol_vectors est une table virtuelle vec0 : le REPLACE ne
                # passait que tant que la ligne visee venait d'etre inseree
                # dans la meme transaction non validee. Des qu'on valide par
                # lot, la ligne est committee et vec0 rend
                # "UNIQUE constraint failed on symbol_vectors primary key"
                # au reindex d'un symbole modifie. Le bench code_retrieval a
                # attrape exactement ca en CI le 26/07/2026.
                conn.execute(
                    "DELETE FROM symbol_vectors WHERE symbol_id=?", (symbol_id,),
                )
                conn.execute(
                    "INSERT INTO symbol_vectors(symbol_id, embedding) "
                    "VALUES (?, ?)",
                    (symbol_id, blob),
                )
                indexed += 1
            # Le verrou d'ecriture s'arrete ici, pas a la fin de la boucle.
            conn.commit()
        # Prune rows for symbols that vanished from the project tree.
        live_keys = {s["symbol_key"] for s in symbols}
        stale_keys = [k for k in existing if k not in live_keys]
        removed = 0
        for key in stale_keys:
            cur = conn.execute(
                "DELETE FROM symbols WHERE project_root=? AND symbol_key=? "
                "RETURNING id",
                (root_str, key),
            )
            row = cur.fetchone()
            if row is not None:
                conn.execute(
                    "DELETE FROM symbol_vectors WHERE symbol_id=?", (row[0],),
                )
                removed += 1
        conn.commit()

    return {
        "status": "ok",
        "total": len(symbols),
        "indexed": indexed,
        "skipped": skipped,
        "removed": removed,
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }


# ---------------------------------------------------------------------------
# Query — k-NN over symbol_vectors with safety metadata
# ---------------------------------------------------------------------------


# Un reclassement lexical a vecu ici du 06 au 09/08/2026 : il melangeait la
# proximite vectorielle (0,65) au recouvrement des mots de la question avec
# le chemin du fichier (0,35), pour qu'une route Next.js nommee `GET` ne soit
# pas diluee. Retire, parce qu'il comptait DEUX FOIS le meme signal : le
# chemin avait ete ajoute au document vectorise dans le meme commit, donc le
# vecteur le voyait deja. Mesure sur le banc code_retrieval (30 questions) :
# MRR@10 0,690 -> 0,635 et R@3 0,733 -> 0,633 avec le reclassement, soit la
# porte de qualite en echec pendant douze jours. Son auteur ecrivait lui-meme
# qu'aucun chiffre ne le soutenait ; il en existe un maintenant, et il dit
# non. A ressusciter le jour ou un banc a verite terrain TypeScript montre un
# gain -- l'historique git garde le code.


def search_symbols_semantic(
    query: str,
    project_root: str | Path,
    *,
    limit: int = 10,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """k-NN over symbol_vectors for ``project_root``.

    Returns ``{"status": str, "hits": [...], "warning": None}``. The
    ``warning`` key is preserved in the shape for backwards compatibility
    with callers that test ``result.get("warning")``, but is always
    ``None`` on this path.

    Each hit carries the disambiguation metadata required by the safety
    contract (see docs/design-semantic-code-tools.md): signature,
    docstring head, file:line, score.

    No low-confidence warning is emitted. tests/benchmarks/code_retrieval
    (30 queries, 1002 symbols) showed that on code symbols the top1
    score and top1-top2 gap distributions overlap between correct and
    wrong retrievals (correct top1 0.59-0.78, wrong top1 0.63-0.69),
    giving the warning 12% precision and 25% recall at tuned thresholds.
    A signal that noisy teaches the agent to ignore it. The safety
    contract still holds via the read-only schema + ``find_symbol``
    verification gate before any destructive operation on a hit.
    """
    from token_savior.db_core import VECTOR_SEARCH_AVAILABLE
    from token_savior.memory._facade import memory_db
    from token_savior.memory.embeddings import embed

    if not VECTOR_SEARCH_AVAILABLE:
        return {"status": "unavailable", "reason": "sqlite-vec not loadable", "hits": []}
    qvec = embed(query, as_query=True)
    if qvec is None:
        return {"status": "unavailable", "reason": "query embed failed", "hits": []}
    blob = _serialize_vec(qvec)
    if blob is None:
        return {"status": "unavailable", "reason": "vec serialize failed", "hits": []}

    root_str = str(Path(project_root).resolve())
    k = max(int(limit), 5)

    sql = (
        "SELECT s.symbol_key, s.file_path, s.lineno, s.kind, s.signature,"
        "       s.docstring_head, v.distance "
        "FROM symbol_vectors v "
        "JOIN symbols s ON s.id = v.symbol_id "
        "WHERE v.embedding MATCH ? AND k = ? AND s.project_root = ? "
        "ORDER BY v.distance"
    )
    def conn_factory():
        return memory_db.db_session(db_path) if db_path else memory_db.db_session()
    with conn_factory() as conn:
        rows = conn.execute(sql, (blob, k, root_str)).fetchall()

    hits: list[dict] = []
    for r in rows:
        # sqlite-vec vec0 FLOAT[N] stores L2 distance by default. Our
        # vectors are L2-normalised upstream, so the identity
        #   |a - b|^2 = 2 - 2·(a·b)
        # gives  cos(a, b) = 1 - L2^2 / 2  on the [-1, 1] interval.
        l2 = float(r["distance"])
        cos_score = max(0.0, 1.0 - (l2 * l2) / 2.0)
        _, qname = r["symbol_key"].split("::", 1) if "::" in r["symbol_key"] else ("", r["symbol_key"])
        hits.append({
            "symbol": qname,
            "kind": r["kind"],
            "file": r["file_path"],
            "line": r["lineno"],
            "signature": r["signature"],
            "docstring_head": r["docstring_head"],
            "score": round(cos_score, 4),
        })

    # No low-confidence warning on this path — see docstring. Kept in the
    # return shape as None for callers that check ``.get("warning")``.
    return {
        "status": "ok",
        "hits": hits,
        "warning": None,
    }
