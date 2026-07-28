"""La vitrine publique annonce une version installable, pas un travail en cours.

Le defaut ferme ici : `_latest_entry` prenait la premiere section du CHANGELOG.
Des qu'une PR ouvrait une section `## Unreleased`, la page annoncait ce titre de
travail comme derniere version, et la verification de derive cassait a chaque
merge — un rouge recurrent qui pousse a synchroniser sans regarder, donc a
publier l'information fausse.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]


@pytest.fixture
def site_sync(tmp_path, monkeypatch):
    """Charge scripts/site_sync.py avec un CHANGELOG controle."""
    spec = importlib.util.spec_from_file_location(
        "site_sync_sous_test", RACINE / "scripts" / "site_sync.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["site_sync_sous_test"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def _ecrire(tmp_path, contenu: str):
    (tmp_path / "CHANGELOG.md").write_text(contenu, encoding="utf-8")


def test_section_unreleased_est_sautee(site_sync, tmp_path):
    _ecrire(tmp_path, """# Changelog

## Unreleased — One server, many worktrees, no stolen calls

du travail en cours.

## v4.21.0 — The server stops keeping what it knows to itself (2026-07-28)

publie.
""")
    assert site_sync._latest_entry() == "The server stops keeping what it knows to itself"


def test_plusieurs_sections_non_publiees_sautees(site_sync, tmp_path):
    """Deux PR ouvertes en meme temps ne doivent pas non plus masquer la release."""
    _ecrire(tmp_path, """# Changelog

## Unreleased — un truc

## Next — un autre truc

## v4.20.0 — What the tools promise, now measured (2026-07-27)
""")
    assert site_sync._latest_entry() == "What the tools promise, now measured"


def test_version_en_tete_est_prise(site_sync, tmp_path):
    _ecrire(tmp_path, """# Changelog

## v4.21.0 — The server stops keeping what it knows to itself (2026-07-28)
""")
    assert site_sync._latest_entry() == "The server stops keeping what it knows to itself"


def test_changelog_sans_version_publiee(site_sync, tmp_path):
    """Aucune version publiee : on rend vide plutot qu'un titre de travail."""
    _ecrire(tmp_path, "# Changelog\n\n## Unreleased — rien encore\n")
    assert site_sync._latest_entry() == ""


def test_le_vrai_changelog_donne_une_version_publiee():
    """Garde-fou sur le depot reel, pas seulement sur des cas fabriques."""
    spec = importlib.util.spec_from_file_location(
        "site_sync_reel", RACINE / "scripts" / "site_sync.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    titre = module._latest_entry()
    assert titre, "aucune entree publiee trouvee dans le CHANGELOG"
    assert "unreleased" not in titre.lower()
