"""Persistent per-tool call counter scoped by MCP client (A5).

Motivation — AUDIT.md Phase 2b and Phase 4 A5: we have no data to back
"which 14 tools are actually hot" when we flip the default profile in
v3.0. Session-scoped counters in ``get_stats`` evaporate when the
server stops, so decisions fall on author intuition (``_ULTRA_INCLUDES``).

This module adds a JSON-file counter at
``$TOKEN_SAVIOR_STATS_DIR/tool-calls.json``, bumped on every tool call.
The counter is scoped by ``TOKEN_SAVIOR_CLIENT`` (`claude-code`,
`cursor`, `cline`, …) so we can spot divergence between clients when
trimming the default profile.

Format (stable across versions; bump ``schema_version`` on breaking
changes):

    {
      "schema_version": 1,
      "last_updated_epoch": 1729701234,
      "counts": {
        "claude-code": {"find_symbol": 1234, "get_function_source": 567},
        "cursor":      {"find_symbol":   45}
      }
    }

The file is written with a temp-then-rename so the JSON on disk is
always valid even under crashes. Writes happen on every call; the cost
is a single ~5 KB rewrite per call (~1 ms on SSD) which is negligible
against tool-call latencies measured in hundreds of ms.

Failures (disk full, permission denied, JSON corruption from an older
version) are swallowed silently — telemetry must never break the
server. Errors surface via ``telemetry_health()`` for a future
``memory_doctor`` probe.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path

_SCHEMA_VERSION = 1


def stats_dir() -> Path:
    """Ou vivent les statistiques, demande au moment ou on ecrit.

    Reference unique pour `TOKEN_SAVIOR_STATS_DIR`. La variable etait lue dans
    six modules : quatre figeaient la reponse a l'import (`server_state`,
    `dashboard`, `slot_manager`, `server_handlers/tool_search`), deux la
    relisaient a l'usage. Un processus qui posait la variable apres l'import de
    l'un des quatre ecrivait donc dans deux repertoires a la fois et relisait
    dans celui des deux qu'il interrogeait (#90).

    Ce module est une feuille -- il n'importe rien du paquet -- donc les cinq
    autres peuvent s'y adresser sans cycle.
    """
    raw = os.environ.get("TOKEN_SAVIOR_STATS_DIR", "~/.local/share/token-savior")
    return Path(os.path.expanduser(raw))


def _counter_path() -> Path:
    return stats_dir() / "tool-calls.json"


def _resolve_client() -> str:
    """Client id from env, defaulting to ``unknown``.

    The MCP server registry advertises ``TOKEN_SAVIOR_CLIENT`` as the
    canonical variable (see server.json). Empty strings count as
    unknown so a ``TOKEN_SAVIOR_CLIENT=`` in a broken .env doesn't
    silently pin every call to "".
    """
    raw = os.environ.get("TOKEN_SAVIOR_CLIENT", "").strip()
    return raw or "unknown"


# ── in-process cache ───────────────────────────────────────────────────────
#
# We do NOT re-read the JSON on every bump — that would make the counter
# N² over the session. The cache is loaded once lazily and flushed on
# every bump via atomic rename. Multi-process concurrent writes (two
# servers sharing a stats dir) would lose increments to each other; this
# is acceptable for the v2.8/v3.0 goal of "is this tool ever called".
#
_lock = threading.Lock()
_state: dict | None = None
_last_error: str | None = None


def _load() -> dict:
    global _last_error
    path = _counter_path()
    try:
        if not path.exists():
            return {
                "schema_version": _SCHEMA_VERSION,
                "last_updated_epoch": int(time.time()),
                "counts": {},
            }
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schema_version") != _SCHEMA_VERSION:
            # Incompatible schema: start fresh but keep the old file as
            # a .bak so a human can recover if needed.
            try:
                path.rename(path.with_suffix(path.suffix + ".bak"))
            except OSError:
                pass
            return {
                "schema_version": _SCHEMA_VERSION,
                "last_updated_epoch": int(time.time()),
                "counts": {},
            }
        counts = data.get("counts")
        if not isinstance(counts, dict):
            data["counts"] = {}
        return data
    except (OSError, json.JSONDecodeError) as e:
        _last_error = f"load: {type(e).__name__}: {e}"
        return {
            "schema_version": _SCHEMA_VERSION,
            "last_updated_epoch": int(time.time()),
            "counts": {},
        }


def _save(data: dict) -> None:
    global _last_error
    path = _counter_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp.replace(path)
        _last_error = None
    except OSError as e:
        _last_error = f"save: {type(e).__name__}: {e}"


# ── public API ─────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _verrou(path: Path):
    """Verrou exclusif inter-processus sur ``<path>.lock``.

    Sans lui, deux processus qui incrementent le meme compteur font chacun
    lire-modifier-ecrire sur leur propre copie et le dernier ecrivain ecrase
    l'autre. Mesure du 27/07/2026 avant correction, huit processus de
    cinquante incrementations chacun, temoin mono-processus exact a 400/400 :

        2 processus -> 212 / 400 comptees   (47 % perdues)
        8 processus -> 112, 104, 74 / 400   (72 a 81 % perdues)

    Aucune exception, aucun processus en echec : la perte etait totalement
    silencieuse.

    Degrade en douceur. Si le verrou ne peut pas etre pris -- systeme sans
    ``fcntl`` ni ``msvcrt``, repertoire en lecture seule, montage reseau qui
    refuse le verrouillage -- on execute quand meme le bloc. Un compteur de
    telemetrie ne doit jamais empecher un appel d'outil d'aboutir, et perdre
    une incrementation reste infiniment preferable a lever une exception dans
    le chemin de dispatch.
    """
    fichier = path.with_suffix(path.suffix + ".lock")
    try:
        fichier.parent.mkdir(parents=True, exist_ok=True)
        poignee = fichier.open("a+")
    except OSError:
        yield
        return
    try:
        try:
            import fcntl

            fcntl.flock(poignee.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            try:
                import msvcrt

                msvcrt.locking(poignee.fileno(), msvcrt.LK_LOCK, 1)
            except Exception:
                pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(poignee.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            poignee.close()
        except OSError:
            pass


def record_tool_call(tool_name: str) -> None:
    """Increment the counter for ``(tool_name, client)``.

    Never raises. Call on every successful tool invocation. Failure to
    persist is recorded in :func:`telemetry_health` but never propagates
    -- we absolutely must not crash the tool-dispatch path for a
    telemetry write.

    L'etat est relu depuis le disque a chaque incrementation, sous verrou.
    Il y avait auparavant un cache en memoire, garde pour eviter un cout
    dit ``N²``. Ce cache etait le defaut : il retenait un instantane ABSOLU
    du compteur, donc chaque processus reecrivait le fichier avec sa propre
    vision du total et effacait celle des autres.

    Le cout evite n'existait d'ailleurs pas. Le fichier ne grossit pas avec
    le nombre d'appels mais avec le nombre d'outils DISTINCTS, environ 70 :
    relire est lineaire, jamais quadratique. On a paye une mesure fausse
    pour economiser un cout imaginaire.

    Les deux verrous sont necessaires et ne font pas le meme travail :
    ``_lock`` serialise les threads du processus courant, ``_verrou``
    serialise les processus entre eux.
    """
    if not tool_name:
        return
    client = _resolve_client()
    global _state
    chemin = _counter_path()
    with _lock, _verrou(chemin):
        etat = _load()  # toujours frais, jamais un instantane garde
        bucket = etat["counts"].setdefault(client, {})
        bucket[tool_name] = bucket.get(tool_name, 0) + 1
        etat["last_updated_epoch"] = int(time.time())
        _save(etat)
        _state = etat


def _nudge_path() -> Path:
    return stats_dir() / "nudge-stats.json"


_nudge_lock = threading.Lock()
_nudge_state: dict | None = None


def record_nudge(kind: str) -> None:
    """Persist a fired chain-nudge by kind so its effectiveness can be audited.

    Adoption is measured by comparing the nudge fire count here against the
    target tool's count in tool-calls.json over time (e.g. does get_edit_context
    usage rise after the edit-context nudge starts firing?). Never raises.

    Meme correction que :func:`record_tool_call`, et elle compte double ici :
    ce compteur sert a juger l'efficacite des nudges. Un compteur de nudges
    sous-evalue et un compteur d'outils sous-evalue, perdus dans des
    proportions differentes selon la concurrence du moment, donnent un
    RATIO faux. C'est sur ce genre de ratio qu'on decide de garder ou de
    retirer un mecanisme.
    """
    if not kind:
        return
    global _nudge_state
    chemin = _nudge_path()
    with _nudge_lock, _verrou(chemin):
        etat = _load_json(chemin)  # frais, pas un instantane garde
        counts = etat.setdefault("counts", {})
        counts[kind] = counts.get(kind, 0) + 1
        etat["last_updated_epoch"] = int(time.time())
        _save_json(chemin, etat)
        _nudge_state = etat


def nudge_counts() -> dict[str, int]:
    """Return persisted per-kind nudge fire counts. Never raises.

    Relit le disque, pour la meme raison que :func:`aggregate_counts` : le
    cache ne voyait que ce que CE processus avait ecrit.
    """
    global _nudge_state
    with _nudge_lock:
        _nudge_state = _load_json(_nudge_path())
        counts = _nudge_state.get("counts", {}) or {}
    return {k: v for k, v in counts.items() if isinstance(v, int)}


def _load_json(path: Path) -> dict:
    try:
        if not path.exists():
            return {"schema_version": _SCHEMA_VERSION, "counts": {}}
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("counts"), dict):
            return {"schema_version": _SCHEMA_VERSION, "counts": {}}
        return data
    except (OSError, json.JSONDecodeError):
        return {"schema_version": _SCHEMA_VERSION, "counts": {}}


def _save_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp.replace(path)
    except OSError:
        pass


def telemetry_health() -> dict:
    """Snapshot of the counter for a future ``memory_doctor`` section.

    Returns ``{"ok": bool, "path": str, "clients": int, "distinct_tools":
    int, "error": str | None}``.

    Relit le disque : un diagnostic qui rapporte l'etat cache du processus
    courant plutot que l'etat reel du fichier est precisement le diagnostic
    qu'on ne veut pas avoir sous les yeux quand on cherche pourquoi les
    chiffres ne collent pas.
    """
    global _state
    with _lock:
        _state = _load()
        counts = _state.get("counts", {})
        clients = list(counts.keys())
        distinct_tools = len({
            tool for bucket in counts.values() for tool in bucket
        })
        return {
            "ok": _last_error is None,
            "path": str(_counter_path()),
            "clients": len(clients),
            "distinct_tools": distinct_tools,
            "error": _last_error,
        }


def aggregate_counts() -> dict[str, int]:
    """Sum tool-call counters across all clients.

    Used by the `auto` profile to size the hot-tools layer from real
    historical usage. Returns ``{tool_name: total_count}``. Never raises.

    Relit le disque plutot que de servir le cache. Le cache appartenait au
    processus courant : un serveur demarre il y a une heure servait sa vision
    d'il y a une heure, en ignorant tout ce que les autres processus avaient
    compte depuis. Dimensionner la couche des outils chauds sur cette vision
    revenait a la dimensionner sur une fraction arbitraire de l'usage reel.
    Cette fonction est appelee au demarrage d'un profil, pas dans une boucle
    chaude : la relecture ne coute rien.
    """
    global _state
    with _lock:
        _state = _load()
        counts = _state.get("counts", {}) or {}
    aggregate: dict[str, int] = {}
    for bucket in counts.values():
        if not isinstance(bucket, dict):
            continue
        for tool, n in bucket.items():
            if not isinstance(tool, str) or not isinstance(n, int):
                continue
            aggregate[tool] = aggregate.get(tool, 0) + n
    return aggregate


def reset_for_tests() -> None:
    """Only for tests: clear in-process cache so _load() re-runs."""
    global _state, _last_error
    with _lock:
        _state = None
        _last_error = None
