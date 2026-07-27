"""`TOKEN_SAVIOR_STATS_DIR` must mean the same thing everywhere in one process.

Six modules read it. Four froze the answer at import (`server_state`,
`dashboard`, `slot_manager`, `server_handlers/tool_search`) and two resolved it
at use (`telemetry`, `query_api`), so a process that sets the variable after
any of the four are imported writes its statistics to two directories at once
and reads them back from whichever half it asks. Reported as #90; same family
as the data-directory defect fixed in #84.

Not academic: `TCAEngine.__init__` calls `mkdir(parents=True)`, so importing
`token_savior.server_state` creates the frozen directory on disk before anyone
has had a chance to say where it should be.

Run in a clean interpreter rather than by touching module state: `conftest`
sets this variable at import time precisely so the suite's own modules agree,
which is the workaround this test exists to make unnecessary. What is under
test is the case where nobody arranged the import order, and only a fresh
process has it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

# Every site the issue lists, probed through something that exists whether or
# not the fix is in: the engines expose the directory they were built with,
# and the path helpers can simply be asked for a path.
_PROGRAMME = """
import json, os
from pathlib import Path

from token_savior import server_state          # freezes the four, if they freeze
from token_savior import dashboard, slot_manager, telemetry
from token_savior.server_handlers import tool_search

os.environ["TOKEN_SAVIOR_STATS_DIR"] = {cible!r}

# tool_search: where does a save actually land? Asked of the filesystem rather
# than of a module attribute, since the attribute is also the override hook.
tool_search._save_disk_embeds("sig-sonde", {{"outil": [0.0]}})
_candidats = [{cible!r}, os.path.expanduser("~/.local/share/token-savior")]
_ou_tool_search = next(
    (d for d in _candidats if os.path.exists(os.path.join(d, "tool_embeddings.json"))),
    "nulle part",
)

# dashboard: plant a project payload in the new directory and see if it is read.
Path({cible!r}).mkdir(parents=True, exist_ok=True)
Path({cible!r}, "sonde-0000.json").write_text(json.dumps({{
    "project": "/opt/projet-sonde", "total_calls": 1, "sessions": 1,
    "total_chars_returned": 10, "total_naive_chars": 100,
}}))
lu_par_dashboard = [
    r["project_root"] for r in dashboard.collect_dashboard_data()["projects"]
]

print(json.dumps({{
    "prefetcher": str(server_state._prefetcher.stats_dir),
    "tca": str(server_state._tca_engine.data_dir),
    "leiden": str(server_state._leiden.stats_dir),
    "linucb": str(server_state._linucb.stats_dir),
    "warm_start": str(server_state._warm_start.stats_dir),
    "slot_manager": os.path.dirname(slot_manager._get_stats_file("/opt/projet")),
    "tool_search": _ou_tool_search,
    "telemetry": os.path.dirname(str(telemetry._counter_path())),
    "dashboard": lu_par_dashboard,
}}))
"""


def _dans_un_interprete_neuf(programme: str, home) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("TOKEN_SAVIOR_STATS_DIR", None)
    # Anything still frozen resolves to ~; keep that out of the real home.
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, "-c", programme],
        capture_output=True, text=True, env=env, timeout=180, check=False,
    )


def test_stats_dir_set_after_import_is_honoured_everywhere(tmp_path) -> None:
    cible = tmp_path / "ailleurs"
    home = tmp_path / "home"
    home.mkdir()
    resultat = _dans_un_interprete_neuf(_PROGRAMME.format(cible=str(cible)), home)
    assert resultat.returncode == 0, resultat.stderr
    ou = json.loads(resultat.stdout.strip().splitlines()[-1])

    desaccords = {
        nom: chemin for nom, chemin in ou.items()
        if nom != "dashboard" and chemin != str(cible)
    }
    assert not desaccords, (
        f"ces sites ecrivent ailleurs que dans {cible} : {desaccords}"
    )
    assert "/opt/projet-sonde" in ou["dashboard"], (
        f"le dashboard n'a pas lu {cible} : {ou['dashboard']}"
    )
    assert (cible / "tool_embeddings.json").exists(), "tool_search a ecrit ailleurs"


def test_an_explicit_rebinding_still_wins(tmp_path) -> None:
    """The per-test overrides the suite depends on must keep winning.

    `test_registered_persistence` and `test_tool_embed_disk_cache` rebind
    `_REGISTERED_ROOTS_FILE` and `_EMBED_CACHE_FILE` to a tmp path. Resolving
    the environment at use time must not quietly take that away, or those tests
    go back to writing into the user's real statistics directory.
    """
    impose = tmp_path / "impose"
    impose.mkdir()
    ignore = tmp_path / "ignore"
    home = tmp_path / "home"
    home.mkdir()
    programme = (
        "import os\n"
        "from token_savior import slot_manager\n"
        "from token_savior.server_handlers import tool_search\n"
        f"os.environ['TOKEN_SAVIOR_STATS_DIR'] = {str(ignore)!r}\n"
        f"slot_manager._REGISTERED_ROOTS_FILE = {str(impose / 'reg.json')!r}\n"
        f"tool_search._EMBED_CACHE_FILE = {str(impose / 'embeds.json')!r}\n"
        "slot_manager._persist_registered_root('/opt')\n"
        "tool_search._save_disk_embeds('sig', {'outil': [0.0]})\n"
    )
    resultat = _dans_un_interprete_neuf(programme, home)
    assert resultat.returncode == 0, resultat.stderr
    assert (impose / "reg.json").exists(), resultat.stderr
    assert (impose / "embeds.json").exists(), resultat.stderr
    assert not (ignore / "registered_projects.json").exists(), (
        "the environment overrode an override"
    )
    assert not (ignore / "tool_embeddings.json").exists(), (
        "the environment overrode an override"
    )
