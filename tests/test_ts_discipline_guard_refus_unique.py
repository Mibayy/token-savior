"""Un refus enseigne une fois ; deux fois, il empêche.

Le 27/07/2026, trois gardes bloquants ont ete retires de ce poste apres avoir
bloque quatre fois du travail correct en une session. Deux de ces blocages
portaient sur un cas que Token Savior ne sait pas traiter :
`replace_symbol_source` ne porte que sur les fonctions et les classes, pas sur
une constante ni sur un dictionnaire de module. Le garde interdisait donc la
seule voie qui restait.

La sortie de secours documentee, `TS_GUARD_OFF=1`, vit dans l'environnement de
la session : on ne la pose pas entre deux appels. Elle voulait dire en pratique
« demande a l'utilisateur d'eteindre le garde ».

D'ou le contrat teste ici : le premier appel est refuse et nomme la meilleure
route, le second identique passe. C'est ce qui separe un ralentisseur d'un mur.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "ts_discipline_guard.py"


def lancer(payload: dict, etat: Path) -> dict | None:
    env = dict(os.environ)
    env.pop("TS_GUARD_OFF", None)
    env["TS_DISCIPLINE_GUARD"] = "1"
    env["XDG_STATE_HOME"] = str(etat)
    out = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
        timeout=15, check=False,
    ).stdout.strip()
    return json.loads(out) if out else None


@pytest.fixture
def projet(tmp_path: Path) -> Path:
    (tmp_path / ".token-savior-cache.json").write_text("{}", encoding="utf-8")
    return tmp_path


def _lecture(projet: Path, nom: str = "mod.py") -> dict:
    f = projet / nom
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    return {"session_id": "s1", "tool_name": "Read",
            "tool_input": {"file_path": str(f)}}


def _edition_native(projet: Path, nom: str = "conf.py") -> dict:
    f = projet / nom
    f.write_text("TABLE = {\n  'a': 1,\n}\n", encoding="utf-8")
    return {"session_id": "s1", "tool_name": "Edit",
            "tool_input": {"file_path": str(f), "old_string": "1",
                           "new_string": "2"}}


class TestRefusUnique:
    def test_le_premier_appel_est_refuse(self, projet: Path, tmp_path: Path) -> None:
        etat = tmp_path / "etat"
        v = lancer(_lecture(projet), etat)
        assert v is not None
        assert v["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_le_second_appel_identique_passe(self, projet: Path, tmp_path: Path) -> None:
        """Le cas qui a fait retirer les gardes du 27/07 : plus d'impasse."""
        etat = tmp_path / "etat"
        appel = _lecture(projet)
        assert lancer(appel, etat) is not None
        assert lancer(appel, etat) is None, (
            "insister sur le meme appel doit passer, sinon le garde est un mur"
        )

    def test_l_edition_native_suit_la_meme_regle(self, projet: Path, tmp_path: Path) -> None:
        """C'est le cas precis du dictionnaire de module, que
        replace_symbol_source ne sait pas modifier."""
        etat = tmp_path / "etat"
        appel = _edition_native(projet)
        assert lancer(appel, etat) is not None
        assert lancer(appel, etat) is None

    def test_le_refus_dit_comment_insister(self, projet: Path, tmp_path: Path) -> None:
        etat = tmp_path / "etat"
        raison = lancer(_lecture(projet), etat)["hookSpecificOutput"][
            "permissionDecisionReason"]
        assert "relancez le meme appel" in raison

    def test_un_autre_fichier_est_refuse_a_son_tour(self, projet: Path, tmp_path: Path) -> None:
        """Le pardon porte sur l'appel, pas sur la session entiere."""
        etat = tmp_path / "etat"
        assert lancer(_lecture(projet, "a.py"), etat) is not None
        assert lancer(_lecture(projet, "a.py"), etat) is None
        assert lancer(_lecture(projet, "b.py"), etat) is not None

    def test_les_sessions_ne_partagent_pas_leurs_pardons(
        self, projet: Path, tmp_path: Path
    ) -> None:
        etat = tmp_path / "etat"
        appel = _lecture(projet)
        assert lancer(appel, etat) is not None
        assert lancer(appel, etat) is None
        autre = {**appel, "session_id": "s2"}
        assert lancer(autre, etat) is not None, (
            "une nouvelle session n'a pas recu l'enseignement"
        )
