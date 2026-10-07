"""Le projet actif survit au redémarrage du serveur (07/10/2026).

Chaque message Telegram relance un `claude -p` et donc un serveur MCP neuf :
le projet actif retombait sur la première racine configurée. Relevé sur 30
jours : 37 sessions ont commencé sans switch_project, 9 ont échoué ainsi.
"""
from __future__ import annotations

import token_savior.server_runtime as rt
import token_savior.server_state as st
import token_savior.slot_manager as sm


def test_retenu_puis_restaure_pour_le_meme_dossier(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sm, "_stats_dir", lambda: str(tmp_path / "stats"))
    projet = tmp_path / "paid2call"; projet.mkdir()
    lancement = tmp_path / "maison"; lancement.mkdir()
    sm.memoriser_actif(str(projet), cwd=str(lancement))
    assert sm.dernier_actif(cwd=str(lancement)) == str(projet)
    assert sm.dernier_actif(cwd=str(tmp_path)) is None, "un autre dossier de lancement n'hérite pas"

    monkeypatch.chdir(lancement)
    monkeypatch.setattr(rt, "_active_hint_source", "")
    avant = st._slot_mgr.active_root
    try:
        rt._restaurer_dernier_actif(None)
        assert st._slot_mgr.active_root == str(projet)
    finally:
        st._slot_mgr.active_root = avant
        st._slot_mgr.projects.pop(str(projet), None)


def test_rien_n_est_restaure_si_le_lancement_est_un_projet(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sm, "_stats_dir", lambda: str(tmp_path / "stats"))
    projet = tmp_path / "p"; projet.mkdir()
    sm.memoriser_actif(str(projet), cwd=str(tmp_path))
    monkeypatch.chdir(tmp_path)
    avant = st._slot_mgr.active_root
    rt._restaurer_dernier_actif(str(tmp_path / "autre"))
    assert st._slot_mgr.active_root == avant


def test_un_projet_disparu_n_est_pas_restaure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sm, "_stats_dir", lambda: str(tmp_path / "stats"))
    sm.memoriser_actif(str(tmp_path / "efface"), cwd=str(tmp_path))
    assert sm.dernier_actif(cwd=str(tmp_path)) is None


def test_les_tests_n_ecrivent_pas_le_fichier_utilisateur() -> None:
    """Une suite qui passe par autodiscover_and_register active la mémorisation :
    elle doit alors écrire dans le dossier de stats isolé des tests, jamais
    dans ~/.local/share/token-savior de l'utilisateur."""
    import os
    reel = os.path.realpath(os.path.expanduser("~/.local/share/token-savior"))
    assert not os.path.realpath(sm._stats_dir()).startswith(reel)
