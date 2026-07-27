"""The sweep must not signal a PID it no longer owns.

`_balayer_transports_morts` reads a PID off a transport whose loop has since
been closed and sends it SIGKILL. The premise is that the worker behind it is
still running and would otherwise be orphaned. That premise only holds half
the time: since the child watchers were removed, asyncio reaps subprocesses
from a thread, independently of the loop's lifetime. Measured on 3.14.6, one
second after `loop.close()`:

    child that had already exited -> os.waitpid raises ECHILD (reaped, PID free)
    long-lived worker             -> still running, still ours

In the first case the PID is back in the OS pool and the kill can land on
whatever was assigned it since -- an arbitrary process of the user's, killed
by a sweep triggered by an unrelated later call, with nothing tying the two
together. Catching ProcessLookupError does not help: the failure mode is the
call *succeeding* against the wrong process.

The transport keeps the `subprocess.Popen` it wrapped, and `Popen.poll()`
answers exactly the right question -- it reports the process as finished once
reaped, and refuses to signal a process it has already buried.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest

from token_savior.code_mode import sandbox


@pytest.fixture(autouse=True)
def _registre_propre():
    sauvegarde = list(sandbox._TRANSPORTS_SUIVIS)
    sandbox._TRANSPORTS_SUIVIS.clear()
    yield
    sandbox._TRANSPORTS_SUIVIS[:] = sauvegarde


def _processus_sur_boucle_fermee(commande: list[str]):
    """Spawn on a private loop, close it, return the asyncio Process."""
    async def demarrer():
        return await asyncio.create_subprocess_exec(
            *commande, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

    boucle = asyncio.new_event_loop()
    try:
        proc = boucle.run_until_complete(demarrer())
    finally:
        boucle.close()
    sandbox._suivre_transport(proc)
    return proc


def test_a_reaped_process_is_not_signalled(monkeypatch) -> None:
    proc = _processus_sur_boucle_fermee([sys.executable, "-c", "pass"])
    pid = proc.pid
    # Give the reaper thread time to collect it: from here the PID is free.
    date_limite = time.time() + 10
    while time.time() < date_limite:
        if proc._transport._proc.poll() is not None:
            break
        time.sleep(0.05)
    assert proc._transport._proc.poll() is not None, "the child never exited"

    signaux: list[tuple[int, int]] = []
    vrai_kill = os.kill
    monkeypatch.setattr(os, "kill", lambda p, s: signaux.append((p, s)))

    sandbox._balayer_transports_morts()

    monkeypatch.setattr(os, "kill", vrai_kill)
    assert not [s for s in signaux if s[0] == pid], (
        f"signalled PID {pid}, which the OS may have reassigned"
    )


def test_a_live_worker_is_still_killed() -> None:
    """The sweep's actual job: no orphan worker per abandoned loop."""
    proc = _processus_sur_boucle_fermee(
        [sys.executable, "-c", "import time; time.sleep(120)"]
    )
    popen = proc._transport._proc
    assert popen.poll() is None, "the worker was not running to begin with"
    try:
        sandbox._balayer_transports_morts()
        date_limite = time.time() + 10
        while time.time() < date_limite and popen.poll() is None:
            time.sleep(0.05)
        assert popen.poll() is not None, "the orphan worker survived the sweep"
    finally:
        if popen.poll() is None:  # pragma: no cover - safety net
            popen.kill()
            popen.wait(timeout=10)


def test_a_transport_on_a_live_loop_is_left_alone() -> None:
    """Only loops that are closed are swept; a warm worker must survive."""
    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(120)",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        sandbox._suivre_transport(proc)
        sandbox._balayer_transports_morts()
        return proc

    boucle = asyncio.new_event_loop()
    try:
        proc = boucle.run_until_complete(scenario())
        popen = proc._transport._proc
        assert popen.poll() is None, "a warm worker on a live loop was killed"
        popen.kill()
        boucle.run_until_complete(proc.wait())
    finally:
        boucle.close()
