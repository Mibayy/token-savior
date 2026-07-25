"""VPS watcher — the proactive layer (unit C).

READ-ONLY. Scans the box for things that need attention without being asked:
failed systemd services, dead timers (crons), TLS certs about to expire, and
recent errors in key service logs. Produces a structured report; it does not
fix or restart anything (that stays a deliberate, separate step).

Run:  python3 scripts/vps_watcher.py
"""
from __future__ import annotations

import glob
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_ERROR_MARKERS = re.compile(
    r"traceback|not found|no such file|\bfatal:|\berror\b|\berr!|permission denied|"
    r"cannot |could not |failed\b|exit code [1-9]", re.IGNORECASE)


def _looks_like_error(line: str) -> bool:
    return bool(line) and bool(_ERROR_MARKERS.search(line))


# --- pure parsers (testable) ------------------------------------------------

def parse_failed_services(text: str) -> list[str]:
    out: list[str] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or "loaded units listed" in ln or ln.startswith("UNIT"):
            continue
        m = re.match(r"([\w@.\-]+\.(?:service|socket|mount|timer|target))\b", ln)
        if m:
            out.append(m.group(1))
    return out


def days_left_from_epoch(notafter_epoch: int, *, now_epoch: int) -> int:
    return int((notafter_epoch - now_epoch) // 86400)


def expiring_certs(certs: list[dict], *, threshold_days: int = 14) -> list[dict]:
    return [c for c in certs if c.get("days_left", 9999) <= threshold_days]


# Ubuntu/system timers that are commonly inactive by design — not our concern.
IGNORE_TIMERS = {
    "apport-autoreport.timer", "snapd.snap-repair.timer", "ua-timer.timer",
    "motd-news.timer", "fstrim.timer", "e2scrub_all.timer",
}


def parse_dead_timers(text: str, ignore: set[str] | None = None) -> list[str]:
    """A timer whose NEXT column is '-' has no future activation → dead.
    System timers in `ignore` (default IGNORE_TIMERS) are skipped."""
    ig = IGNORE_TIMERS if ignore is None else ignore
    dead: list[str] = []
    for ln in (text or "").splitlines():
        if ".timer" not in ln or ln.startswith("NEXT"):
            continue
        m = re.search(r"([\w@.\-]+\.timer)", ln)
        if m and re.match(r"\s*-\s", ln) and m.group(1) not in ig:
            dead.append(m.group(1))
    return dead


def build_report(*, failed: list[str], certs_exp: list[dict],
                 dead_timers: list[str], svc_errors: list[dict]) -> dict:
    needs = bool(failed or certs_exp or dead_timers or svc_errors)
    return {
        "needs_attention": needs,
        "failed_services": failed,
        "expiring_certs": certs_exp,
        "dead_timers": dead_timers,
        "service_errors": svc_errors,
    }


# --- live collectors (read-only system reads) -------------------------------

def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def _collect_failed() -> list[str]:
    return parse_failed_services(_run(["systemctl", "--failed", "--no-legend", "--plain"]))


def _collect_dead_timers() -> list[str]:
    return parse_dead_timers(_run(["systemctl", "list-timers", "--all", "--no-pager"]))


def _collect_certs(now_epoch: int) -> list[dict]:
    from datetime import datetime, timezone
    certs: list[dict] = []
    for cert in glob.glob("/etc/letsencrypt/live/*/cert.pem"):
        name = Path(cert).parent.name
        out = _run(["openssl", "x509", "-enddate", "-noout", "-in", cert])
        m = re.search(r"notAfter=(.+)", out)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z")
            end = int(dt.replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            continue
        certs.append({"name": name, "days_left": days_left_from_epoch(end, now_epoch=now_epoch)})
    return certs


def _collect_service_errors(services: list[str]) -> list[dict]:
    hits = []
    for svc in services:
        out = _run(["journalctl", "-u", svc, "-n", "150", "--no-pager", "-p", "err"])
        errs = [ln for ln in out.splitlines() if _looks_like_error(ln)]
        if errs:
            hits.append({"service": svc, "count": len(errs), "last": errs[-1][:120]})
    return hits


def main() -> int:
    from datetime import datetime, timezone
    now = int(datetime.now(tz=timezone.utc).timestamp())

    failed = _collect_failed()
    dead = _collect_dead_timers()
    certs = expiring_certs(_collect_certs(now), threshold_days=14)
    svc_err = _collect_service_errors(
        ["intel-api", "gw2cc", "claude-telegram", "scribe", "token-savior-dashboard"])
    rep = build_report(failed=failed, certs_exp=certs, dead_timers=dead, svc_errors=svc_err)

    print("=== VPS watcher (read-only) ===")
    if not rep["needs_attention"]:
        print("✓ Rien à signaler : services OK, timers vivants, certs valides, pas d'erreurs.")
        return 0
    if failed:
        print(f"⚠ Services en échec : {failed}")
    if dead:
        print(f"⚠ Timers morts (pas de prochaine exécution) : {dead}")
    if certs:
        for c in certs:
            print(f"⚠ Cert {c['name']} expire dans {c['days_left']} j")
    for e in svc_err:
        print(f"⚠ {e['service']} : {e['count']} erreurs récentes — {e['last']}")
    print("\n→ Signalement uniquement. Aucun service redémarré ni modifié.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
