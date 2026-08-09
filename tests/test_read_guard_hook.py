"""Le hook qui borne une lecture non bornee.

Il vit en PreToolUse parce que c'est le seul moment ou une economie est
possible : en PostToolUse la sortie est deja partie vers le modele. Les tests
ci-dessous portent donc autant sur ce qu'il NE fait pas (refuser, ecraser une
borne posee par l'appelant, toucher a autre chose que du code) que sur ce
qu'il fait.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "read_guard_hook.py"


def _lancer(evenement: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(evenement),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _lancer_env(evenement: dict, env: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(evenement),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, **env},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _fichier(tmp_path: Path, nom: str, lignes: int) -> str:
    p = tmp_path / nom
    p.write_text("\n".join(f"ligne {i}" for i in range(lignes)), encoding="utf-8")
    return str(p)


def _evt(chemin: str, **extra) -> dict:
    return {"tool_name": "Read", "tool_input": {"file_path": chemin, **extra}}


class TestBornage:
    def test_un_gros_fichier_de_code_est_borne(self, tmp_path: Path) -> None:
        chemin = _fichier(tmp_path, "gros.py", 2000)
        sortie = _lancer(_evt(chemin))
        hso = sortie["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow", "ce hook ne refuse jamais"
        assert hso["updatedInput"]["limit"] == 400
        assert hso["updatedInput"]["file_path"] == chemin

    def test_la_raison_nomme_l_outil_a_utiliser_ensuite(self, tmp_path: Path) -> None:
        """Borner sans dire ou aller ensuite ne fait que degrader la reponse."""
        chemin = _fichier(tmp_path, "gros.ts", 2000)
        raison = _lancer(_evt(chemin))["hookSpecificOutput"]["permissionDecisionReason"]
        assert "read_lines" in raison
        assert "get_function_source" in raison

    def test_le_plafond_est_reglable(self, tmp_path: Path) -> None:
        chemin = _fichier(tmp_path, "gros.py", 2000)
        sortie = _lancer_env(_evt(chemin), {"TS_READ_MAX_LINES": "50"})
        assert sortie["hookSpecificOutput"]["updatedInput"]["limit"] == 50


class TestTexteNonCode:
    """La doc et la config pesent 9,3 % des jetons rendus : elles comptent
    aussi, mais pas au meme seuil que le code."""

    def test_un_journal_enorme_est_borne(self, tmp_path: Path) -> None:
        chemin = _fichier(tmp_path, "sortie.log", 5000)
        hso = _lancer(_evt(chemin))["hookSpecificOutput"]
        assert hso["updatedInput"]["limit"] == 600

    def test_la_raison_ne_promet_pas_un_symbole_dans_un_journal(
        self, tmp_path: Path
    ) -> None:
        """Il n'y a pas de fonction a viser dans un CSV : proposer
        get_function_source enverrait dans le mur."""
        chemin = _fichier(tmp_path, "donnees.csv", 5000)
        raison = _lancer(_evt(chemin))["hookSpecificOutput"]["permissionDecisionReason"]
        assert "read_lines" in raison
        assert "get_function_source" not in raison

    def test_le_seuil_du_texte_est_reglable(self, tmp_path: Path) -> None:
        chemin = _fichier(tmp_path, "notes.md", 800)
        sortie = _lancer_env(_evt(chemin), {"TS_READ_MIN_LINES_TEXTE": "100",
                                            "TS_READ_MAX_LINES_TEXTE": "70"})
        assert sortie["hookSpecificOutput"]["updatedInput"]["limit"] == 70


class TestCeQuIlNeTouchePas:
    def test_un_petit_fichier_passe_intact(self, tmp_path: Path) -> None:
        chemin = _fichier(tmp_path, "petit.py", 50)
        assert _lancer(_evt(chemin)) == {"continue": True}

    def test_une_limite_explicite_n_est_jamais_ecrasee(self, tmp_path: Path) -> None:
        """L'appelant qui a deja choisi sa borne a raison contre le hook."""
        chemin = _fichier(tmp_path, "gros.py", 2000)
        assert _lancer(_evt(chemin, limit=1200)) == {"continue": True}

    def test_un_offset_signale_une_lecture_ciblee(self, tmp_path: Path) -> None:
        chemin = _fichier(tmp_path, "gros.py", 2000)
        assert _lancer(_evt(chemin, offset=900)) == {"continue": True}

    def test_un_markdown_de_taille_normale_passe(self, tmp_path: Path) -> None:
        """Un README de 800 lignes se lit en entier pour de bonnes raisons :
        le plancher du texte est bien plus haut que celui du code."""
        chemin = _fichier(tmp_path, "readme.md", 800)
        assert _lancer(_evt(chemin)) == {"continue": True}

    def test_une_extension_inconnue_passe(self, tmp_path: Path) -> None:
        chemin = _fichier(tmp_path, "truc.xyz", 5000)
        assert _lancer(_evt(chemin)) == {"continue": True}

    def test_une_image_n_est_pas_touchee(self, tmp_path: Path) -> None:
        """Une image est facturee a la surface : la borner en lignes n'a aucun sens."""
        chemin = tmp_path / "capture.png"
        chemin.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 500_000)
        assert _lancer(_evt(str(chemin))) == {"continue": True}

    def test_un_autre_outil_passe(self, tmp_path: Path) -> None:
        chemin = _fichier(tmp_path, "gros.py", 2000)
        evt = {"tool_name": "Edit", "tool_input": {"file_path": chemin}}
        assert _lancer(evt) == {"continue": True}

    def test_un_fichier_absent_ne_casse_rien(self, tmp_path: Path) -> None:
        assert _lancer(_evt(str(tmp_path / "jamais.py"))) == {"continue": True}


class TestGardeFous:
    def test_le_drapeau_coupe_tout(self, tmp_path: Path) -> None:
        chemin = _fichier(tmp_path, "gros.py", 2000)
        assert _lancer_env(_evt(chemin), {"TS_READ_GUARD": "0"}) == {"continue": True}

    def test_actif_par_defaut(self, tmp_path: Path) -> None:
        """Le contre-exemple est dans le meme dossier : un rewriter livre
        desactive, installe depuis des mois, qui n'a jamais rien reecrit."""
        chemin = _fichier(tmp_path, "gros.py", 2000)
        env = {k: v for k, v in os.environ.items() if k != "TS_READ_GUARD"}
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(_evt(chemin)),
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )
        assert "updatedInput" in proc.stdout

    def test_une_entree_illisible_ne_bloque_pas(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input="pas du json",
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == {"continue": True}
