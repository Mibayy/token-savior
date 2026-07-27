"""Async Node subprocess sandbox for Code Mode ts_execute.

Warm worker pool: a single long-lived Node process handles all ts_execute
calls. Each call serializes via an asyncio lock (MCP requests are serial).
A dead worker (EOF) or a timed-out worker is killed and respawned lazily on
the next call.

IPC protocol — see worker.mjs header for the full schema. Summary:
  - parent->worker: {type:"exec", exec_id, script_b64, allowed_tools}
                    {type:"result"|"error", id, ...}     (tool-call reply)
  - worker->parent: {type:"ready"}                       (boot handshake)
                    {type:"call", id, tool, args}        (tool dispatch)
                    {type:"log", exec_id, level, value}
                    {type:"final", exec_id, value}
                    {type:"error", exec_id, message, stack}
"""
from __future__ import annotations

import asyncio
import atexit
import base64
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

_WORKER_PATH = Path(__file__).parent / "worker.mjs"
_NODE_BIN = os.environ.get("TS_CODE_MODE_NODE", "node")




def _noter_appel(trace: list, outil: str, ok: bool, valeur) -> None:
    """Garde une trace compacte d'un appel deja servi.

    Bornee des deux cotes : les 25 derniers appels, 240 caracteres chacun. Une
    trace de secours qui ferait exploser la reponse serait pire que pas de
    trace du tout -- ce paquet existe pour economiser des tokens.
    """
    apercu = valeur if isinstance(valeur, str) else repr(valeur)
    if len(apercu) > 240:
        apercu = apercu[:240] + "..."
    trace.append({"tool": outil, "ok": ok, "apercu": apercu})
    if len(trace) > 25:
        del trace[0]
class _Worker:
    """Long-lived Node worker. One exec at a time."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        # The asyncio.Lock is bound to whatever loop is running when it's
        # first used. We lazy-create it per-loop to support test environments
        # that call asyncio.run() repeatedly (each call = fresh loop).
        self._exec_lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._proc_loop: asyncio.AbstractEventLoop | None = None
        self._next_exec_id = 1
        # Test hook: count of spawn() invocations on this singleton instance.
        self.spawn_count = 0

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_event_loop()
        if self._exec_lock is None or self._lock_loop is not loop:
            self._exec_lock = asyncio.Lock()
            self._lock_loop = loop
        return self._exec_lock

    async def ensure_alive(self) -> None:
        loop = asyncio.get_event_loop()
        # Chaque passage ici est l'occasion de solder les transports laisses
        # par des boucles deja fermees, y compris ceux d'autres workers.
        _balayer_transports_morts()
        # asyncio.subprocess pipes are bound to the loop that spawned them.
        # If the loop changed (test harness re-runs `asyncio.run()`), the
        # existing proc handle is unusable — respawn.
        if (
            self.proc is not None
            and self.proc.returncode is None
            and self._proc_loop is loop
        ):
            return
        if self.proc is not None and self._proc_loop is not loop:
            # Stranded process on a dead loop. Best-effort kill via os.kill
            # without touching asyncio streams.
            try:
                import os as _os
                import signal as _signal
                _os.kill(self.proc.pid, _signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            self._neutraliser_transport(self.proc)
            self.proc = None
        await self.spawn()

    @staticmethod
    def _neutraliser_transport(proc) -> None:
        """Empeche le transport d'une boucle morte de se fermer au GC.

        Tuer le processus ne suffit pas : l'objet transport survit et reste
        rattache a la boucle qui l'a cree. Quand le ramasse-miettes finit par
        le collecter, `BaseSubprocessTransport.__del__` voit un transport non
        ferme et rappelle `close()`, qui descend jusqu'a
        `loop.call_soon()` sur une boucle deja fermee et leve

            RuntimeError: Event loop is closed

        a un moment arbitraire, imputee a un test qui n'y est pour rien. C'est
        le bruit qui remontait en annotation sur chaque run de CI.

        `__del__` ne fait rien si le transport se declare deja ferme. On pose
        donc ce drapeau nous-memes, apres le SIGKILL : il n'y a plus rien a
        fermer proprement. Ecrit en defensif, l'attribut est un detail
        d'implementation de CPython et son absence ne doit rien casser.
        """
        transport = getattr(proc, "_transport", None)
        if transport is None:
            return
        try:
            transport._closed = True
        except (AttributeError, TypeError):
            pass

    async def spawn(self) -> None:
        # Point de passage oblige : toute nouvelle instance remplace ici la
        # precedente. Le cas manquant etait un worker mort sur la *meme*
        # boucle -- ensure_alive ne prend alors ni la branche "vivant", ni la
        # branche "boucle changee", et arrivait directement ici en ecrasant
        # self.proc. Le transport orphelin se plaignait plus tard au GC.
        self._neutraliser_transport(self.proc)
        self.proc = await asyncio.create_subprocess_exec(
            _NODE_BIN,
            str(_WORKER_PATH),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NODE_OPTIONS": "--no-warnings"},
        )
        _suivre_transport(self.proc)
        self._proc_loop = asyncio.get_event_loop()
        self.spawn_count += 1
        # Wait for the {"type":"ready"} handshake so the first exec doesn't
        # race against an uninitialized stdin pipe.
        try:
            ready_line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=5)
        except TimeoutError as exc:
            await self._force_kill()
            raise RuntimeError("ts_execute worker did not signal ready in 5s") from exc
        if not ready_line:
            await self._force_kill()
            raise RuntimeError("ts_execute worker exited before ready handshake")
        # Best-effort: tolerate non-JSON noise on the first line.
        try:
            msg = json.loads(ready_line)
        except json.JSONDecodeError:
            msg = None
        if not msg or msg.get("type") != "ready":
            # Not fatal — older worker.mjs revisions may not send `ready`.
            # But our current worker.mjs always does, so log via stderr drain.
            pass

    async def _force_kill(self) -> None:
        if self.proc is None:
            return
        if self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2)
            except TimeoutError:
                pass
        # La boucle est encore vivante ici, mais le ramasse-miettes peut ne
        # collecter ce transport que bien plus tard, quand elle sera fermee.
        # Voir _neutraliser_transport.
        self._neutraliser_transport(self.proc)
        self.proc = None

    async def run(
        self,
        script: str,
        allowed_tools: list[str],
        dispatch: Callable[[str, dict], Any],
        timeout_ms: int,
        max_log_chars: int,
    ) -> dict:
        async with self._get_lock():
            return await self._run_locked(script, allowed_tools, dispatch, timeout_ms, max_log_chars)

    async def _run_locked(
        self,
        script: str,
        allowed_tools: list[str],
        dispatch: Callable[[str, dict], Any],
        timeout_ms: int,
        max_log_chars: int,
    ) -> dict:
        await self.ensure_alive()
        assert self.proc is not None and self.proc.stdin is not None and self.proc.stdout is not None

        exec_id = self._next_exec_id
        self._next_exec_id += 1

        script_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        req = {
            "type": "exec",
            "exec_id": exec_id,
            "script_b64": script_b64,
            "allowed_tools": allowed_tools,
        }
        try:
            self.proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            await self._force_kill()
            # Respawn once and retry.
            await self.ensure_alive()
            assert self.proc is not None and self.proc.stdin is not None
            self.proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

        logs: list[str] = []
        final_value: Any = None
        error: dict | None = None
        tool_calls = 0
        # Trace des appels deja servis. Un script qui expire rendait `value:
        # null` et rien d'autre : tout le travail accompli avant le delai etait
        # perdu, alors que le cote Python avait servi chaque resultat et les
        # jetait. Sur une batterie de 15 outils expiree au 9e, cela veut dire
        # 8 resultats a refaire. Constate deux fois le 27/07/2026, dont une
        # sur une batterie qui a du etre relancee en trois morceaux.
        appels_servis: list[dict] = []
        allowed_set = set(allowed_tools)
        loop = asyncio.get_event_loop()
        started = loop.time()
        timed_out = False

        def _trim_logs(new_entry: str) -> None:
            logs.append(new_entry)
            total = sum(len(s) for s in logs)
            if total > max_log_chars:
                while logs and total > max_log_chars:
                    dropped = logs.pop(0)
                    total -= len(dropped)
                logs.insert(0, "[...truncated...]")

        async def reader() -> None:
            nonlocal final_value, error, tool_calls
            assert self.proc is not None and self.proc.stdout is not None
            while True:
                line_b = await self.proc.stdout.readline()
                if not line_b:
                    # Worker died mid-exec.
                    error = {"message": "worker exited unexpectedly", "stack": ""}
                    return
                line = line_b.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    _trim_logs(f"[malformed]: {line[:200]}")
                    continue
                t = msg.get("type")
                # Ignore messages from older / stray exec_ids — defensive
                # against a respawn race.
                msg_exec_id = msg.get("exec_id")
                if t == "call":
                    tool_calls += 1
                    tool = msg.get("tool", "")
                    args = msg.get("args", {}) or {}
                    call_id = msg.get("id")
                    if tool not in allowed_set:
                        resp = {
                            "type": "error",
                            "id": call_id,
                            "error": f"tool '{tool}' not in code-mode allowlist",
                        }
                        _noter_appel(appels_servis, tool, False, resp["error"])
                    else:
                        try:
                            result = await asyncio.to_thread(dispatch, tool, args)
                            resp = {"type": "result", "id": call_id, "value": result}
                            _noter_appel(appels_servis, tool, True, result)
                        except Exception as e:
                            resp = {
                                "type": "error",
                                "id": call_id,
                                "error": f"{type(e).__name__}: {e}",
                            }
                            _noter_appel(appels_servis, tool, False, resp["error"])
                    try:
                        self.proc.stdin.write((json.dumps(resp) + "\n").encode("utf-8"))
                        await self.proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        error = {"message": "worker stdin closed mid-exec", "stack": ""}
                        return
                elif t == "log":
                    if msg_exec_id is not None and msg_exec_id != exec_id:
                        continue
                    level = msg.get("level", "info")
                    _trim_logs(f"[{level}] {msg.get('value', '')}")
                elif t == "final":
                    if msg_exec_id is not None and msg_exec_id != exec_id:
                        continue
                    final_value = msg.get("value")
                    return
                elif t == "error":
                    if msg_exec_id is not None and msg_exec_id != exec_id:
                        continue
                    error = {
                        "message": msg.get("message", ""),
                        "stack": msg.get("stack", ""),
                    }
                    return

        try:
            await asyncio.wait_for(reader(), timeout=timeout_ms / 1000.0)
        except TimeoutError:
            timed_out = True
            error = {"message": f"script timeout after {timeout_ms}ms", "stack": ""}

        if timed_out:
            # Cannot leave a runaway script blocking the warm worker. Kill,
            # let the next call respawn.
            await self._force_kill()

        duration_ms = int((loop.time() - started) * 1000)

        sortie = {
            "value": final_value,
            "logs": logs,
            "error": error,
            "tool_calls": tool_calls,
            "duration_ms": duration_ms,
        }
        # Seulement quand ca s'est mal passe : dans le cas nominal le script a
        # deja rendu ce qu'il voulait, et repeter chaque appel doublerait la
        # reponse pour rien.
        if error is not None and appels_servis:
            sortie["partial_results"] = appels_servis
        return sortie

    async def shutdown(self) -> None:
        if self.proc is None:
            return
        if self.proc.returncode is None:
            try:
                if self.proc.stdin is not None:
                    self.proc.stdin.write((json.dumps({"type": "shutdown"}) + "\n").encode("utf-8"))
                    await self.proc.stdin.drain()
                    self.proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2)
            except TimeoutError:
                await self._force_kill()
        self._neutraliser_transport(self.proc)
        self.proc = None


# Module-level singleton. Lazy: first ensure_alive() spawns the process.
_POOL: _Worker | None = None



# Transports de sous-processus crees par ce module, suivis en references
# faibles. On ne veut rien maintenir en vie, seulement pouvoir neutraliser
# ceux dont la boucle est morte AVANT que le ramasse-miettes ne les touche.
#
# Pourquoi un registre plutot que de neutraliser a chaque abandon : un worker
# reste volontairement chaud entre deux appels, donc son transport survit a la
# fermeture de la boucle qui l'a cree. Il n'est revisite que si un appel
# suivant passe par ensure_alive. Le dernier worker de chaque boucle, lui,
# n'est jamais revisite -- mesure sur tests/test_code_mode.py : 4 transports
# orphelins sur 10 crees. Chacun leve "RuntimeError: Event loop is closed" au
# GC, a un moment arbitraire, impute a un test qui n'y est pour rien.
_TRANSPORTS_SUIVIS: list = []


def _suivre_transport(proc) -> None:
    """Enregistre le transport d'un processus fraichement cree."""
    import weakref

    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    try:
        _TRANSPORTS_SUIVIS.append(weakref.ref(transport))
    except TypeError:
        pass


def _balayer_transports_morts() -> None:
    """Neutralise tout transport suivi dont la boucle est fermee.

    Le processus derriere un tel transport est inutilisable : ses tuyaux sont
    rattaches a une boucle qui ne tournera plus. On le tue au passage, sinon
    chaque boucle abandonnee laisse un worker Node orphelin.
    """
    import os as _os
    import signal as _signal

    encore_vivants = []
    for reference in _TRANSPORTS_SUIVIS:
        transport = reference()
        if transport is None:
            continue  # deja collecte
        boucle = getattr(transport, "_loop", None)
        if boucle is None or not boucle.is_closed():
            encore_vivants.append(reference)
            continue
        pid = getattr(transport, "_pid", None)
        if pid:
            try:
                _os.kill(pid, _signal.SIGKILL)
            except (ProcessLookupError, OSError, TypeError):
                pass
        try:
            transport._closed = True
        except (AttributeError, TypeError):
            pass
    _TRANSPORTS_SUIVIS[:] = encore_vivants

def _get_pool() -> _Worker:
    global _POOL
    if _POOL is None:
        _POOL = _Worker()
    return _POOL


async def shutdown() -> None:
    """Graceful shutdown — called at process exit."""
    global _POOL
    if _POOL is not None:
        await _POOL.shutdown()
        _POOL = None


def _atexit_shutdown() -> None:
    """Best-effort sync shutdown at interpreter exit."""
    # Dernier filet : tout transport dont la boucle est fermee est solde ici,
    # y compris ceux de workers qui ne sont plus le pool courant.
    _balayer_transports_morts()
    if _POOL is None or _POOL.proc is None:
        return
    proc = _POOL.proc
    if proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass
    # A ce stade la boucle qui portait ce transport est fermee depuis
    # longtemps. Le laisser vivant garantit un
    # "RuntimeError: Event loop is closed" au ramasse-miettes de sortie.
    _Worker._neutraliser_transport(proc)
    _POOL.proc = None


atexit.register(_atexit_shutdown)


async def run_script_async(
    script: str,
    allowed_tools: list[str],
    dispatch: Callable[[str, dict], Any],
    timeout_ms: int = 30000,
    max_log_chars: int = 8000,
) -> dict:
    """Run a user JS script in the warm Node sandbox.

    The script body is wrapped as `(async () => { <body> })()` inside a fresh
    `vm` context (no global leakage between scripts). It can
    `await tools.<name>(args)` for any tool in `allowed_tools`. Each tool call
    is dispatched back to Python via `dispatch(tool_name, args)`.

    Returns: {"value": <any>, "logs": [...], "error": {"message", "stack"} | None,
              "tool_calls": int, "duration_ms": int}.
    """
    pool = _get_pool()
    return await pool.run(script, allowed_tools, dispatch, timeout_ms, max_log_chars)
