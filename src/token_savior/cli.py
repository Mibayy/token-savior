"""
ts -- Token Savior CLI (hors MCP)

Strategie cold-start :
- Mode fork (default fallback) : 1.5s cold start par call (charge mcp.types
  + token_savior.server). Acceptable pour usage one-shot/script.
- Mode daemon : un process persistent ecoute sur /tmp/ts.sock. Cold start
  paye 1.5s UNE fois. Chaque call CLI < 10ms via socket. Recommande pour
  usage interactif / agentique.

Lifecycle daemon :
  ts daemon start    # demarre en background
  ts daemon status   # affiche etat (running, uptime, calls)
  ts daemon stop     # tue le daemon
  ts daemon restart  # stop + start

Usage commun :
  ts use <project_path>            # set active project (persist + push au daemon)
  ts get <symbol>                  # source d'un symbole
  ts search '<regex>' [--limit N]
  ts ctx <symbol> [--depth 1|2]
  ts structure <file>
  ts files [--glob '<pattern>']
  ts find-dead-code
  ts breaking <git-ref>
  ts git-status
  ts replace <symbol>              # nouveau source via stdin
  ts move <symbol> <new-file>
  ts add-field <model> <name> <type>
  ts projects
"""
from __future__ import annotations
import argparse
import contextlib
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

# ---------- paths ----------
_HERE = Path(__file__).resolve().parent
_TS_ROOT = _HERE.parent
_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "ts"
_ACTIVE_FILE = _CONFIG_DIR / "active"
_SOCK_PATH = os.environ.get("TS_SOCK", "/tmp/ts.sock")
_PID_FILE = _CONFIG_DIR / "daemon.pid"

# Defaults reduce noise
os.environ.setdefault("TS_NO_HINTS", "1")
os.environ.setdefault("TS_CAPTURE_DISABLED", "1")
os.environ.setdefault("TS_MEMORY_DISABLE", "1")


# ---------- project state ----------
def _read_active_project() -> str | None:
    if _ACTIVE_FILE.exists():
        return _ACTIVE_FILE.read_text().strip() or None
    return None


def _write_active_project(path: str) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _ACTIVE_FILE.write_text(path.strip() + "\n")


def _ensure_workspace_env(verbose: bool):
    """For fork-mode dispatcher : surface active project via WORKSPACE_ROOTS."""
    if os.environ.get("WORKSPACE_ROOTS"):
        return
    active = _read_active_project()
    if active:
        os.environ["WORKSPACE_ROOTS"] = active


# ---------- wire protocol (daemon <-> CLI) ----------
def _send_frame(sock: socket.socket, obj) -> None:
    data = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_frame(sock: socket.socket, timeout: float = 60.0):
    sock.settimeout(timeout)
    hdr = b""
    while len(hdr) < 4:
        chunk = sock.recv(4 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    (length,) = struct.unpack(">I", hdr)
    buf = b""
    while len(buf) < length:
        chunk = sock.recv(min(65536, length - len(buf)))
        if not chunk:
            return None
        buf += chunk
    return json.loads(buf.decode("utf-8"))


# ---------- daemon command ----------
def _is_ts_in_path() -> bool:
    """True si `ts` est dans le PATH (pip install OK)."""
    import shutil
    return shutil.which("ts") is not None


def _daemon_running() -> bool:
    if not os.path.exists(_SOCK_PATH):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.2)
        s.connect(_SOCK_PATH)
        _send_frame(s, {"cmd": "ping"})
        resp = _recv_frame(s, timeout=0.5)
        s.close()
        return bool(resp and resp.get("ok"))
    except Exception:
        return False


def _try_daemon_call(tool: str, args: dict, timeout: float = 60.0):
    """Return (text, used_daemon: bool). text=None if daemon path failed."""
    if not os.path.exists(_SOCK_PATH):
        return None, False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(_SOCK_PATH)
        _send_frame(s, {"cmd": "call", "tool": tool, "args": args})
        resp = _recv_frame(s, timeout=timeout)
        s.close()
        if resp is None:
            return None, False
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "daemon error"))
        return resp.get("text", ""), True
    except (socket.error, OSError):
        return None, False


def _daemon_start() -> str:
    if _daemon_running():
        return "daemon already running"
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if os.path.exists(_SOCK_PATH):
        os.remove(_SOCK_PATH)
    log = _CONFIG_DIR / "daemon.log"
    # spawn detached daemon. env propage WORKSPACE_ROOTS si actif.
    env = os.environ.copy()
    active = _read_active_project()
    if active and not env.get("WORKSPACE_ROOTS"):
        env["WORKSPACE_ROOTS"] = active
    # Re-exec via le module CLI (pip install fournit `ts` comme entry_point).
    # En dev (sans pip install), fallback sur `python -m token_savior.cli`.
    cmd = ["ts", "_daemon-serve"] if _is_ts_in_path() else [sys.executable, "-m", "token_savior.cli", "_daemon-serve"]
    proc = subprocess.Popen(
        cmd,
        stdout=open(log, "ab"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    # wait up to 3s for socket to come up
    for _ in range(60):
        if _daemon_running():
            _PID_FILE.write_text(str(proc.pid))
            return f"daemon started (pid {proc.pid})"
        time.sleep(0.05)
    return f"daemon failed to come up (check {log})"


def _daemon_stop() -> str:
    if not _daemon_running():
        return "daemon not running"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(_SOCK_PATH)
        _send_frame(s, {"cmd": "shutdown"})
        _recv_frame(s, timeout=2.0)
        s.close()
    except Exception:
        pass
    # cleanup
    if os.path.exists(_SOCK_PATH):
        try:
            os.remove(_SOCK_PATH)
        except OSError:
            pass
    if _PID_FILE.exists():
        try:
            _PID_FILE.unlink()
        except OSError:
            pass
    return "daemon stopped"


def _daemon_status() -> dict:
    running = _daemon_running()
    pid = None
    if _PID_FILE.exists():
        try:
            pid = int(_PID_FILE.read_text().strip())
        except Exception:
            pass
    if running:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(_SOCK_PATH)
            _send_frame(s, {"cmd": "status"})
            resp = _recv_frame(s, timeout=1.0) or {}
            s.close()
            return {"running": True, "pid": pid, **resp.get("status", {})}
        except Exception:
            return {"running": True, "pid": pid}
    return {"running": False}


def _daemon_serve() -> None:
    """Internal command — runs the server loop. Not for direct use."""
    # Charger le dispatcher UNE fois (cout 1.5s)
    # En install pip, token_savior est déjà importable. Pas de path patch.
    with contextlib.redirect_stdout(sys.stderr):
        from token_savior.server import _dispatch_tool  # type: ignore

    started = time.time()
    calls = 0

    if os.path.exists(_SOCK_PATH):
        os.remove(_SOCK_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(_SOCK_PATH)
    os.chmod(_SOCK_PATH, 0o600)
    srv.listen(8)

    print(f"[ts-daemon] listening on {_SOCK_PATH}", file=sys.stderr, flush=True)

    while True:
        try:
            conn, _ = srv.accept()
        except KeyboardInterrupt:
            break
        try:
            req = _recv_frame(conn, timeout=5.0)
            if not req:
                conn.close()
                continue
            cmd = req.get("cmd")
            if cmd == "ping":
                _send_frame(conn, {"ok": True})
            elif cmd == "status":
                _send_frame(conn, {"ok": True, "status": {
                    "uptime_s": int(time.time() - started),
                    "calls": calls,
                }})
            elif cmd == "shutdown":
                _send_frame(conn, {"ok": True})
                conn.close()
                break
            elif cmd == "call":
                try:
                    with contextlib.redirect_stdout(sys.stderr):
                        result = _dispatch_tool(req["tool"], req.get("args", {}), "")
                    parts = []
                    for r in result:
                        t = getattr(r, "text", None)
                        parts.append(t if t is not None else str(r))
                    _send_frame(conn, {"ok": True, "text": "\n".join(parts)})
                    calls += 1
                except Exception as e:
                    _send_frame(conn, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            else:
                _send_frame(conn, {"ok": False, "error": f"unknown cmd: {cmd}"})
        except Exception as e:
            try:
                _send_frame(conn, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    if os.path.exists(_SOCK_PATH):
        os.remove(_SOCK_PATH)


# ---------- fork-mode dispatcher (fallback) ----------
def _silence_imports(verbose: bool):
    if verbose:
        return contextlib.nullcontext()
    return contextlib.redirect_stdout(sys.stderr)


def _import_dispatcher(verbose: bool):
    # En install pip, token_savior est déjà importable. Pas de path patch.
    with _silence_imports(verbose):
        from token_savior.server import _dispatch_tool  # type: ignore
    return _dispatch_tool


def _fork_call(tool: str, args: dict, verbose: bool) -> str:
    _ensure_workspace_env(verbose)
    dispatcher = _import_dispatcher(verbose)
    args.setdefault("hints", False)
    if verbose:
        result = dispatcher(tool, args, "")
    else:
        with contextlib.redirect_stdout(sys.stderr):
            result = dispatcher(tool, args, "")
    parts = []
    for r in result:
        text = getattr(r, "text", None)
        parts.append(text if text is not None else str(r))
    return "\n".join(parts)


# ---------- unified call ----------
def _call(tool: str, args: dict, verbose: bool, prefer_daemon: bool = True) -> str:
    args.setdefault("hints", False)
    if prefer_daemon:
        text, used = _try_daemon_call(tool, args)
        if used:
            return text
    return _fork_call(tool, args, verbose)


# ---------- print ----------
def _print(out: str, as_text: bool):
    if as_text:
        print(out)
        return
    out = (out or "").strip()
    try:
        embedded = json.loads(out)
        payload = {"ok": True, "result": embedded}
    except (json.JSONDecodeError, ValueError):
        payload = {"ok": True, "result": out}
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def _error(msg: str, code: int = 1):
    json.dump({"ok": False, "error": msg}, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(code)


# ---------- main ----------
def main():
    # Hidden subcommand : _daemon-serve (utilise par ts daemon start)
    if len(sys.argv) == 2 and sys.argv[1] == "_daemon-serve":
        _daemon_serve()
        return

    # `ts init ...` -- merge hook config into agent settings (F3).
    # Routed early because it has its own argparse and does not need the
    # dispatcher / daemon at all.
    if len(sys.argv) >= 2 and sys.argv[1] == "init":
        from token_savior.cli_init import run as _init_run
        sys.exit(_init_run(sys.argv[2:]))

    p = argparse.ArgumentParser(prog="ts", description="Token Savior CLI")
    p.add_argument("--text", action="store_true", help="Sortie brute au lieu de JSON")
    p.add_argument("--verbose", "-v", action="store_true", help="Afficher les logs moteur")
    p.add_argument("--no-daemon", action="store_true", help="Force fork mode (skip daemon)")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Init -- merge TS hook config into agent settings (handled above with
    # its own parser; declared here so `ts --help` advertises it).
    sub.add_parser("init", help="Install TS hooks into your AI agent settings")

    # Daemon
    d = sub.add_parser("daemon", help="Gestion du daemon")
    d.add_argument("action", choices=["start", "stop", "status", "restart", "warm"])
    d.add_argument("path", nargs="?", default=None,
                   help="Pour warm : chemin projet a precharger (defaut: actif)")

    # Project
    s = sub.add_parser("use", help="Selectionner le projet actif")
    s.add_argument("path")

    sub.add_parser("projects", help="Lister les projets")

    # Read
    s = sub.add_parser("get", help="Source d'une fonction/classe (auto)")
    s.add_argument("name")
    s.add_argument("--max-lines", type=int, default=0)

    s = sub.add_parser("search", help="Regex search dans le code")
    s.add_argument("pattern")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--semantic", action="store_true")

    s = sub.add_parser("ctx", help="get_full_context : source + deps + callers")
    s.add_argument("name")
    s.add_argument("--depth", type=int, default=1)

    s = sub.add_parser("structure", help="Structure d'un fichier")
    s.add_argument("file")

    s = sub.add_parser("files", help="Lister les fichiers")
    s.add_argument("--glob", default=None)

    # Audit
    sub.add_parser("find-dead-code", help="Tools/fonctions jamais appelees")
    s = sub.add_parser("breaking", help="Breaking changes vs git ref")
    s.add_argument("ref", default="HEAD~1", nargs="?")

    # Git
    sub.add_parser("git-status", help="Git status structure")

    # Edit
    s = sub.add_parser("replace", help="Remplacer source d'un symbole (stdin)")
    s.add_argument("name")

    s = sub.add_parser("move", help="Deplacer un symbole vers un fichier")
    s.add_argument("name")
    s.add_argument("dest_file")

    s = sub.add_parser("add-field", help="Ajouter un champ a un modele")
    s.add_argument("model")
    s.add_argument("field_name")
    s.add_argument("field_type")

    args = p.parse_args()
    verbose = args.verbose
    prefer_daemon = not args.no_daemon

    # Daemon management
    if args.cmd == "daemon":
        if args.action == "start":
            msg = _daemon_start()
        elif args.action == "stop":
            msg = _daemon_stop()
        elif args.action == "restart":
            _daemon_stop()
            time.sleep(0.2)
            msg = _daemon_start()
        elif args.action == "status":
            st = _daemon_status()
            _print(json.dumps(st), args.text)
            return
        elif args.action == "warm":
            # Precharge l'index symbolique pour un projet :
            #   1. switch_project (fait que ce projet est actif)
            #   2. get_project_summary (declenche le reindex si necessaire)
            # Cible : daemon prêt à servir get/search/ctx sans premier call lent.
            path = args.path or _read_active_project()
            if not path:
                _error("warm: argument <path> requis (ou faire ts use d'abord)")
                return
            if not _daemon_running():
                msg = _daemon_start()
                _print(msg, args.text)
                if not _daemon_running():
                    _error("warm: impossible de demarrer le daemon")
                    return
            t0 = time.time()
            try:
                _call("switch_project", {"name": path}, verbose, prefer_daemon=True)
                _call("get_project_summary", {}, verbose, prefer_daemon=True)
                elapsed_ms = int((time.time() - t0) * 1000)
                _print(f"daemon warmed for {path} (took {elapsed_ms}ms)", args.text)
            except Exception as e:
                _error(f"warm: {type(e).__name__}: {e}")
            return
        _print(msg, args.text)
        return

    # `use` ne necessite pas de dispatcher
    if args.cmd == "use":
        if not os.path.isabs(args.path):
            _error(f"Path must be absolute: {args.path}")
        if not os.path.isdir(args.path):
            _error(f"Not a directory: {args.path}")
        _write_active_project(args.path)
        # Si daemon up : pousse switch_project
        if prefer_daemon and _daemon_running():
            try:
                _call("switch_project", {"name": args.path}, verbose, prefer_daemon=True)
            except Exception:
                pass
        _print(f"Active project set to: {args.path}", args.text)
        return

    try:
        if args.cmd == "projects":
            out = _call("list_projects", {}, verbose, prefer_daemon)
        elif args.cmd == "get":
            cargs = {"name": args.name}
            if args.max_lines:
                cargs["max_lines"] = args.max_lines
            try:
                out = _call("get_function_source", cargs, verbose, prefer_daemon)
                if not out or out.startswith("Error") or "not found" in out.lower():
                    out = _call("get_class_source", cargs, verbose, prefer_daemon)
            except Exception:
                out = _call("get_class_source", cargs, verbose, prefer_daemon)
        elif args.cmd == "search":
            cargs = {"pattern": args.pattern, "max_results": args.limit}
            if args.semantic:
                cargs["semantic"] = True
            out = _call("search_codebase", cargs, verbose, prefer_daemon)
        elif args.cmd == "ctx":
            out = _call("get_full_context", {"name": args.name, "depth": args.depth}, verbose, prefer_daemon)
        elif args.cmd == "structure":
            out = _call("get_structure_summary", {"file_path": args.file}, verbose, prefer_daemon)
        elif args.cmd == "files":
            cargs = {}
            if args.glob:
                cargs["pattern"] = args.glob
            out = _call("list_files", cargs, verbose, prefer_daemon)
        elif args.cmd == "find-dead-code":
            out = _call("find_dead_code", {}, verbose, prefer_daemon)
        elif args.cmd == "breaking":
            out = _call("detect_breaking_changes", {"ref": args.ref}, verbose, prefer_daemon)
        elif args.cmd == "git-status":
            out = _call("get_git_status", {}, verbose, prefer_daemon)
        elif args.cmd == "replace":
            new_source = sys.stdin.read()
            out = _call("replace_symbol_source", {"symbol_name": args.name, "new_source": new_source}, verbose, prefer_daemon)
        elif args.cmd == "move":
            out = _call("move_symbol", {"symbol_name": args.name, "destination_file": args.dest_file}, verbose, prefer_daemon)
        elif args.cmd == "add-field":
            out = _call("add_field_to_model", {
                "model": args.model,
                "field_name": args.field_name,
                "field_type": args.field_type,
            }, verbose, prefer_daemon)
        else:
            _error(f"Unknown command: {args.cmd}")
            return

        _print(out, args.text)
    except Exception as e:
        _error(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
