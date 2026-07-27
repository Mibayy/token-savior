"""Server handlers for tool capture (sandbox of verbose tool outputs).

These are META category handlers (no slot needed): they only read/write the
shared SQLite memory DB. Hooks call ``capture_put`` directly via the helper
script, so the dispatcher only exposes the *retrieval* surface to agents.
"""
from __future__ import annotations

import json
from typing import Any

from token_savior._compat import types
from token_savior.memory import tool_capture


def _text(s: Any) -> list[types.TextContent]:
    if not isinstance(s, str):
        s = json.dumps(s, indent=2, default=str)
    return [types.TextContent(type="text", text=s)]


def _ts_capture_put(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Manual capture entrypoint -- used by hooks and rare manual calls.

    `tool_name` et `output` sont declares obligatoires dans le schema. Ils
    etaient lus en `.get(...) or <defaut>` : un appel sans aucun argument
    ecrivait une capture vide attribuee a "unknown" et repondait succes, avec
    un identifiant a la cle. Meme classe de defaut que le
    `capture_get(range=...)` corrige en v4.19.0 : un repli silencieux dans un
    outil dont le metier est justement de ne pas gaspiller.

    L'acces direct fait remonter une KeyError, que le dispatch traduit en
    message nommant l'argument manquant. C'est volontaire : un seul endroit
    formule ce message, pour tous les outils.
    """
    tool_name = arguments["tool_name"]
    output = arguments["output"]
    res = tool_capture.capture_put(
        tool_name=tool_name,
        output=output,
        args_summary=arguments.get("args_summary"),
        session_id=arguments.get("session_id"),
        project_root=arguments.get("project_root"),
        meta=arguments.get("meta"),
    )
    return _text(res)


def _ts_capture_search(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Recherche dans les captures.

    `query` est declare obligatoire. Il etait lu en `.get("query") or ""` :
    un appel sans requete rendait `{"count": 0, "results": []}`, c'est-a-dire
    une reponse d'apparence normale a une question qui n'a jamais ete posee.
    Pour lister sans filtrer, `capture_list` existe.
    """
    rows = tool_capture.capture_search(
        query=arguments["query"],
        limit=int(arguments.get("limit", 20)),
        session_id=arguments.get("session_id"),
        project_root=arguments.get("project_root"),
        tool_name=arguments.get("tool_name"),
    )
    return _text({"count": len(rows), "results": rows})


def _ts_capture_get(arguments: dict[str, Any]) -> list[types.TextContent]:
    cap_id = arguments.get("id")
    if cap_id is None:
        return _text({"error": "id required"})
    res = tool_capture.capture_get(
        int(cap_id),
        range_spec=arguments.get("range"),
        max_bytes=arguments.get("max_bytes"),
    )
    if res is None:
        return _text({"error": f"capture {cap_id} not found"})
    return _text(res)


def _ts_capture_aggregate(arguments: dict[str, Any]) -> list[types.TextContent]:
    cap_id = arguments.get("id")
    if cap_id is None:
        return _text({"error": "id required"})
    res = tool_capture.capture_aggregate(
        int(cap_id),
        transform=arguments.get("transform", "stats"),
        pattern=arguments.get("pattern"),
    )
    if res is None:
        return _text({"error": f"capture {cap_id} not found"})
    return _text(res)


def _ts_capture_list(arguments: dict[str, Any]) -> list[types.TextContent]:
    """Liste les captures. Une ligne doit identifier, pas restituer.

    Mesure du 27/07/2026 : par defaut, cet outil rendait 29 836 caracteres --
    vingt-quatre fois tout le code source du projet audite. Chaque entree
    pesait ~677 caracteres, dont ~310 pour la commande complete et ~205 pour
    un apercu, multiplies par une limite par defaut de 50.

    Dans un paquet dont le metier est d'economiser des tokens, un listage qui
    deverse 30 Ko sans qu'on ait rien demande est un defaut a lui seul. Le
    contenu reste integralement accessible : chaque ligne porte son `uri`,
    que `capture_get` prend directement.
    """
    rows = tool_capture.capture_list(
        session_id=arguments.get("session_id"),
        project_root=arguments.get("project_root"),
        tool_name=arguments.get("tool_name"),
        limit=int(arguments.get("limit", 20)),
    )
    return _text({"count": len(rows), "captures": [_ligne_de_liste(r) for r in rows]})


def _ligne_de_liste(ligne: dict, borne: int = 60) -> dict:
    """Raccourcit les deux champs longs d'une entree de liste.

    On ne retire rien : on borne. `args_summary` et `preview` servent a
    reconnaitre une capture parmi d'autres, 60 caracteres y suffisent. Pour la
    lire, il y a `capture_get(uri)`.
    """
    compacte = dict(ligne)
    for champ in ("args_summary", "preview"):
        valeur = compacte.get(champ)
        if isinstance(valeur, str) and len(valeur) > borne:
            compacte[champ] = valeur[:borne] + "..."
    return compacte


def _ts_capture_purge(arguments: dict[str, Any]) -> list[types.TextContent]:
    n = tool_capture.capture_purge(
        older_than_sec=arguments.get("older_than_sec"),
        session_id=arguments.get("session_id"),
        project_root=arguments.get("project_root"),
    )
    return _text({"deleted": n})


HANDLERS: dict[str, Any] = {
    "capture_put": _ts_capture_put,
    "capture_search": _ts_capture_search,
    "capture_get": _ts_capture_get,
    "capture_aggregate": _ts_capture_aggregate,
    "capture_list": _ts_capture_list,
    "capture_purge": _ts_capture_purge,
}
