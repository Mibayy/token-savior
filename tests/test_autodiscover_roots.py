"""Auto-discovery of project roots when nothing is configured.

Returning an empty list when `WORKSPACE_ROOTS` is unset was the single largest
source of missed adoption: a fresh install indexed nothing until the user
hand-wrote every project path. Measured on one real workstation, **28% of all
code reads went to projects that were never listed** — one of them a 767-file
repository that had existed for months.

The risk on the other side is a guard that over-collects: recursing a home
directory to find every `.git` is slow and picks up vendored clones nobody
wants indexed. These tests pin both edges.
"""
from __future__ import annotations

import os

import pytest

from token_savior.server_runtime import (
    MAX_AUTODISCOVERED,
    autodiscover_roots,
    is_project_dir,
    project_root_of,
)


def make_project(base, name: str, marker: str = ".git"):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    if marker == ".git":
        (d / ".git").mkdir()
    else:
        (d / marker).write_text("{}", encoding="utf-8")
    return d


# --- project_root_of ------------------------------------------------------ #

@pytest.mark.parametrize("marker", [".git", "pyproject.toml", "package.json",
                                    "Cargo.toml", "go.mod", "Gemfile"])
def test_reconnait_les_marqueurs_usuels(tmp_path, marker: str) -> None:
    p = make_project(tmp_path, "proj", marker)
    assert is_project_dir(str(p))
    assert project_root_of(str(p / "src" / "deep" / "file.py")) == str(p)


def test_remonte_depuis_un_fichier_profond(tmp_path) -> None:
    p = make_project(tmp_path, "proj")
    profond = p / "a" / "b" / "c"
    profond.mkdir(parents=True)
    assert project_root_of(str(profond)) == str(p)


def test_ne_remonte_pas_depuis_une_dependance_vendorisee(tmp_path) -> None:
    """Un paquet dans node_modules a souvent son propre package.json. L'indexer
    reviendrait a indexer les dependances de tout le monde."""
    p = make_project(tmp_path, "proj")
    vendu = p / "node_modules" / "paquet"
    vendu.mkdir(parents=True)
    (vendu / "package.json").write_text("{}", encoding="utf-8")
    assert project_root_of(str(vendu)) is None


def test_rend_none_hors_de_tout_projet(tmp_path) -> None:
    nu = tmp_path / "sans_marqueur"
    nu.mkdir()
    assert project_root_of(str(nu)) is None


# --- autodiscover_roots --------------------------------------------------- #

def test_trouve_le_projet_du_repertoire_courant(tmp_path) -> None:
    p = make_project(tmp_path, "courant")
    roots = autodiscover_roots(cwd=str(p / "sous"), home=str(tmp_path / "vide"))
    assert str(p) in roots


def test_balaye_les_dossiers_de_code_usuels(tmp_path) -> None:
    home = tmp_path / "home"
    for parent in ("projects", "dev", "src"):
        make_project(home / parent, f"app-{parent}")
    (home / "projects" / "pas-un-projet").mkdir()
    roots = autodiscover_roots(cwd=str(tmp_path / "ailleurs"), home=str(home))
    noms = {os.path.basename(r) for r in roots}
    assert {"app-projects", "app-dev", "app-src"} <= noms
    assert "pas-un-projet" not in noms


def test_ne_descend_pas_de_deux_niveaux(tmp_path) -> None:
    """Un balayage profond transforme un home en exploration de disque."""
    home = tmp_path / "home"
    make_project(home / "projects" / "groupe", "trop-profond")
    roots = autodiscover_roots(cwd=str(tmp_path / "ailleurs"), home=str(home))
    assert not any("trop-profond" in r for r in roots)


def test_ignore_les_dossiers_caches(tmp_path) -> None:
    home = tmp_path / "home"
    make_project(home / "projects", ".cache-clone")
    roots = autodiscover_roots(cwd=str(tmp_path / "ailleurs"), home=str(home))
    assert not any(".cache-clone" in r for r in roots)


def test_plafonne_le_nombre_de_projets(tmp_path) -> None:
    """Au-dela, la disposition est inhabituelle et merite une config explicite."""
    home = tmp_path / "home"
    for i in range(MAX_AUTODISCOVERED + 15):
        make_project(home / "projects", f"p{i:03d}")
    roots = autodiscover_roots(cwd=str(tmp_path / "ailleurs"), home=str(home))
    assert len(roots) <= MAX_AUTODISCOVERED


def test_pas_de_doublon_entre_cwd_et_balayage(tmp_path) -> None:
    home = tmp_path / "home"
    p = make_project(home / "projects", "commun")
    roots = autodiscover_roots(cwd=str(p), home=str(home))
    assert roots.count(str(p)) == 1


def test_rend_une_liste_vide_sans_rien_trouver(tmp_path) -> None:
    vide = tmp_path / "rien"
    vide.mkdir()
    assert autodiscover_roots(cwd=str(vide), home=str(vide)) == []


# --- integration avec la config ------------------------------------------- #

def test_la_config_explicite_l_emporte(tmp_path, monkeypatch) -> None:
    """La decouverte ne doit jamais s'ajouter a une config explicite."""
    from token_savior.server_runtime import _parse_workspace_roots

    explicite = make_project(tmp_path, "explicite")
    make_project(tmp_path / "home" / "projects", "decouvert")
    monkeypatch.setenv("WORKSPACE_ROOTS", str(explicite))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert _parse_workspace_roots() == [str(explicite)]


def test_parse_ne_devine_jamais(tmp_path, monkeypatch) -> None:
    """`_parse_workspace_roots` lit la configuration et rien d'autre.

    Y avoir mis la decouverte a casse deux tests sans rapport : cette fonction
    sert aussi a resoudre un projet en cours d'execution, et elle repondait
    soudain avec ce qui trainait pres du repertoire courant.
    """
    from token_savior.server_runtime import _parse_workspace_roots

    make_project(tmp_path / "home" / "projects", "decouvert")
    monkeypatch.delenv("WORKSPACE_ROOTS", raising=False)
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert _parse_workspace_roots() == []


def test_le_demarrage_decouvre_quand_rien_n_est_configure(tmp_path, monkeypatch) -> None:
    """C'est `_register_roots`, le chemin de demarrage, qui decouvre."""
    import token_savior.server_runtime as rt

    p = make_project(tmp_path / "home" / "projects", "decouvert")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TOKEN_SAVIOR_AUTODISCOVER", raising=False)
    monkeypatch.chdir(tmp_path)
    recus: list[list[str]] = []
    monkeypatch.setattr(rt.s._slot_mgr, "register_roots", lambda r: recus.append(list(r)))
    rt.s._slot_mgr.projects.clear()
    rt.autodiscover_and_register()
    assert recus and str(p) in recus[0]


def test_le_demarrage_se_debraye(tmp_path, monkeypatch) -> None:
    import token_savior.server_runtime as rt

    make_project(tmp_path / "home" / "projects", "decouvert")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TOKEN_SAVIOR_AUTODISCOVER", "0")
    monkeypatch.chdir(tmp_path)
    recus: list[list[str]] = []
    monkeypatch.setattr(rt.s._slot_mgr, "register_roots", lambda r: recus.append(list(r)))
    rt.s._slot_mgr.projects.clear()
    assert rt.autodiscover_and_register() == []
    assert recus == []
