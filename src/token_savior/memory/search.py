"""A1-3: hybrid search (FTS5 + sqlite-vec) with RRF fusion.

``hybrid_search`` is the search entry-point used by ``observation_search``.
Behaviour:

1. Always run an FTS5 query (existing keyword path).
2. If ``VECTOR_SEARCH_AVAILABLE`` is False, or embedding the query yields
   ``None``, or the ``obs_vectors`` table is missing, return FTS results
   untouched — full backwards compatibility.
3. Otherwise run a k-NN query against ``obs_vectors`` and fuse both rank
   lists with Reciprocal Rank Fusion (RRF, k=60, the reference constant
   from Cormack et al. 2009).

All helpers here are deliberately DB-agnostic with respect to how the
rows arrive — callers pass the SQL connection and the filter fragments,
which keeps the quarantine / type / project_root logic centralised at
one call-site in ``observation_search``.
"""

from __future__ import annotations

from typing import Any

RRF_K = 60  # standard Reciprocal Rank Fusion constant.


def rrf_merge(
    *ranked_lists: list[dict[str, Any]],
    limit: int = 20,
    k: int = RRF_K,
) -> list[dict[str, Any]]:
    """Fuse N rank-ordered result lists into a single list using RRF.

    Score per obs = Σ 1/(k + rank_i), where rank is 1-based within each
    individual list. The highest-scoring rows survive. Metadata is taken
    from the first list the id appears in (stable for debuggability).
    """
    scores: dict[int, float] = {}
    metadata: dict[int, dict[str, Any]] = {}
    for rows in ranked_lists:
        for rank, row in enumerate(rows, start=1):
            oid = row.get("id")
            if oid is None:
                continue
            scores[oid] = scores.get(oid, 0.0) + 1.0 / (k + rank)
            if oid not in metadata:
                metadata[oid] = row
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    out: list[dict[str, Any]] = []
    for oid, score in ranked[:limit]:
        row = dict(metadata[oid])
        row["_rrf_score"] = round(score, 6)
        out.append(row)
    return out


# Seuils mesures, pas choisis : voir le commentaire dans hybrid_search.
# Les deux valent 0,90 AUJOURD'HUI, et c'est delibere.
#
# Le decoupage en deux constantes nomme deux situations qui n'ont pas les
# memes exigences : un voisin vectoriel qui vient en appui d'un resultat
# lexical est deja cautionne par le leg lexical, tandis qu'un voisin seul est
# l'unique signal disponible et doit convaincre a lui tout seul. FUSION peut
# donc etre plus laxiste que SEULE ; l'inverse n'aurait pas de sens.
#
# Mais aucune mesure ne justifie a ce jour un ecart chiffre entre les deux, et
# on n'en invente pas un. Souleve par andrebrait dans #79, qui a mesure puis
# refute sa propre hypothese de depart : sur son corpus, 0,90 garde 90 % des
# voisins pertinents pour 10 % de non-pertinents. Le seul resultat qui
# survit est faible, et il refuse lui-meme d'agir dessus (n=20, corpus qu'il
# a ecrit, requetes proches des observations) : entre 0,84 et 0,90 la part de
# pertinents ne bouge pas alors que les non-pertinents passent de 2 % a 10 %.
#
# L'invariant FUSION >= SEULE est verifie dans
# tests/test_vector_distance_floor.py, pour que cette intention soit
# executable au lieu d'etre decorative, et pour qu'un futur ecart soit un
# choix delibere et non un accident de frappe.
# 0,90 -> 0,86 le 27/07/2026, parce que ce qu'on encode a change.
#
# La PR #91 fait entrer le TITRE de l'observation dans le vecteur, ce qui etait
# un vrai defaut : un titre qui n'atteint jamais le vecteur est du signal jete.
# Mais encoder plus deplace la distribution des distances, donc un seuil mesure
# avant ce changement ne decrit plus ce code. C'est exactement le piege de #79,
# ou une valeur avait ete jugee sur un corpus qui n'etait deja plus le bon.
#
# Balayage sur le corpus de tests/test_vector_distance_floor.py, titres encodes,
# contre les attentes exactes des tests existants :
#
#     seuil   requetes muettes   bonne 1re reponse   sans bruit
#     0,90        3/3                3/3               0/1   <- ancien
#     0,89        3/3                3/3               1/1
#     ...tout le plateau jusqu'a 0,82 est identique...
#     0,82        3/3                3/3               1/1
#
# A 0,90, « certificat qui expire » ramenait « Relancer les tests » en plus.
#
# Le choix de 0,86 et pas de 0,89 : 0,89 est la premiere valeur qui marche,
# donc elle est collee a la falaise. Un seuil pose au bord d'un genou casse au
# premier changement de corpus ou de modele. 0,86 est au milieu du plateau
# mesure, et tombe dans la zone que le balayage independant d'andrebrait dans
# #79 signalait deja comme « free precision » : entre 0,84 et 0,90 la part de
# voisins pertinents ne bougeait pas tandis que les non-pertinents passaient
# de 2 % a 10 %.
#
# Ce que cette mesure NE dit pas : la valeur juste. Cinq observations ecrites
# pour un test ne fondent pas un seuil de production. Elle etablit seulement
# que 0,90 est desormais faux, ce qui suffit a justifier de bouger, et que
# 0,86 est loin des deux bords de ce qu'on sait mesurer. Le vrai reglage
# demande le corpus de rappel reel, cf. #79.
_DISTANCE_MAX_FUSION = 0.86   # en appui d'un resultat lexical
_DISTANCE_MAX_SEULE = 0.86    # seul signal disponible : exiger la confiance
# Les deux valeurs sont egales aujourd'hui : la mesure ne montre aucune bande
# ou le vecteur serait fiable en appui mais pas seul. Elles restent distinctes
# parce que ce sont deux decisions differentes, et qu'un modele d'embedding
# mieux adapte a la langue justifierait de relacher la premiere.


def vec_search_rows(
    conn: Any,
    query_vec: list[float],
    project_root: str,
    *,
    limit: int = 40,
    type_filter: str | None = None,
    include_quarantine: bool = False,
    max_distance: float | None = None,
) -> list[dict[str, Any]]:
    """k-NN over obs_vectors → list of dicts matching observation_search shape.

    ``max_distance`` ecarte les voisins trop lointains. Sans lui, un k-NN rend
    toujours k resultats, quelle que soit la distance : une requete sans aucun
    rapport avec la base ressort avec les observations les moins eloignees,
    presentees comme des souvenirs pertinents.

    Returns [] on any failure (missing table, unloaded extension, bad vec
    serialization) so the hybrid path degrades gracefully.
    """
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError:
        return []
    try:
        blob = sqlite_vec.serialize_float32(query_vec)
    except Exception:
        return []

    sql = (
        "SELECT o.id, o.type, o.title, o.importance, o.symbol, o.file_path, "
        "  substr(COALESCE(o.narrative, o.content), 1, 160) AS excerpt, "
        "  o.created_at, o.created_at_epoch, o.is_global, o.agent_id, "
        "  c.quarantine, c.stale_suspected, v.distance "
        "FROM obs_vectors AS v "
        "JOIN observations AS o ON o.id = v.obs_id "
        "LEFT JOIN consistency_scores AS c ON c.obs_id = o.id "
        "WHERE v.embedding MATCH ? AND k = ? "
        "  AND o.archived = 0 "
        "  AND (o.project_root = ? OR o.is_global = 1) "
    )
    params: list[Any] = [blob, int(limit), project_root]
    if not include_quarantine:
        sql += "AND (c.quarantine IS NULL OR c.quarantine = 0) "
    if type_filter:
        sql += "AND o.type = ? "
        params.append(type_filter)
    sql += "ORDER BY v.distance"

    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        return []
    resultats = [dict(r) for r in rows]
    if max_distance is not None:
        resultats = [r for r in resultats
                     if r.get("distance") is None or r["distance"] <= max_distance]
    return resultats


def hybrid_search(
    conn: Any,
    fts_rows: list[dict[str, Any]],
    query: str,
    project_root: str,
    *,
    limit: int = 20,
    type_filter: str | None = None,
    include_quarantine: bool = False,
) -> list[dict[str, Any]]:
    """Fuse FTS rows with a vector-search pass when available.

    ``fts_rows`` is computed by the caller (``observation_search``) using
    the existing SQL so quarantine/type/global filtering stays DRY. If the
    vector stack is unavailable, the FTS list is returned untouched
    (truncated to ``limit``) — fully backwards compatible.
    """
    from token_savior.db_core import VECTOR_SEARCH_AVAILABLE
    if not VECTOR_SEARCH_AVAILABLE:
        return fts_rows[:limit]
    try:
        from token_savior.memory.embeddings import embed
    except Exception:
        return fts_rows[:limit]
    vec = embed(query, as_query=True)
    if vec is None:
        return fts_rows[:limit]
    # Mesure sur des observations francaises : les distances des voisins
    # PERTINENTS vont de 0,85 a 0,99, celles des voisins SANS RAPPORT de 0,97
    # a 1,07. Les bandes se recouvrent presque entierement, et une requete
    # absurde obtenait un meilleur score que la bonne reponse d'une requete
    # legitime. Le vecteur n'apporte donc quelque chose que lorsqu'il est
    # franchement confiant ; ailleurs c'est le lexical qui sait.
    #
    # Quand le lexical n'a rien trouve, le seuil est resserre : sans lui,
    # toute requete rendait un resultat, ce qui transforme une memoire en
    # generateur de souvenirs plausibles.
    seuil = _DISTANCE_MAX_SEULE if not fts_rows else _DISTANCE_MAX_FUSION
    vec_rows = vec_search_rows(
        conn, vec, project_root,
        limit=limit * 2,
        type_filter=type_filter,
        include_quarantine=include_quarantine,
        max_distance=seuil,
    )
    if not vec_rows:
        return fts_rows[:limit]
    return rrf_merge(fts_rows, vec_rows, limit=limit)
