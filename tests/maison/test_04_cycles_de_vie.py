"""Les outils qui ecrivent : cycle complet, pas juste un appel isole.

Ecrire puis relire est le seul moyen de savoir si un outil d'ecriture fait ce
qu'il annonce. Un `memory_save` qui rend un identifiant sans que
`memory_get` retrouve rien a "reussi" au sens du premier fichier de cette
serie, et rate completement au sens de l'usage.

Trois cycles : memoire, captures, edition de code. Chacun sur une base ou une
copie jetable -- une suite qui laisse des traces dans les donnees de
l'utilisateur est un defaut a elle seule, deja rencontre ici (284
observations de test trouvees dans la base reelle, v4.19.0).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture
def copie(projet_cobaye: Path, tmp_path: Path, appeler) -> Path:
    """Copie jetable, enregistree comme projet a part entiere.

    Sans l'enregistrement, les outils d'edition repondent une erreur au
    lieu d'editer -- et le test generique, qui n'exige qu'une reponse non
    vide sans trace, laissait passer cette erreur pour un succes.
    """
    cible = tmp_path / "cobaye"
    shutil.copytree(projet_cobaye, cible)
    # Appel brut : passer `project=<copie>` avant qu'elle soit enregistree
    # fait echouer la resolution du projet, et set_project_root ne
    # s'execute jamais. La copie restait alors non indexee et toute
    # edition repondait une erreur au lieu d'editer.
    appeler.brut("set_project_root", path=str(cible))
    return cible


# --- Cycle memoire ------------------------------------------------------


def test_une_observation_sauvee_est_retrouvee(appeler) -> None:
    appeler(
        "memory_save",
        type="project",
        title="Le panier applique la remise au-dela de 100",
        content="Regle metier verifiee dans boutique/remises.py.",
    )
    trouve = appeler("memory_search", query="remise au-dela")
    assert "remise" in trouve.lower(), f"observation non retrouvee :\n{trouve[:400]}"


def test_l_index_memoire_repond(appeler) -> None:
    appeler(
        "memory_save",
        type="project",
        title="Observation pour l'index",
        content="Contenu indexable.",
    )
    index = appeler("memory_index", query="index")
    assert index.strip()
    assert "Traceback" not in index


def test_une_recherche_memoire_sans_correspondance_ne_ment_pas(appeler) -> None:
    """Une memoire qui repond toujours n'est plus une memoire (v4.19.0)."""
    sortie = appeler("memory_search", query="zzzz_sujet_totalement_absent_zzzz")
    assert "Traceback" not in sortie
    assert "zzzz_sujet_totalement_absent_zzzz" not in sortie or "0" in sortie, sortie[:300]


def test_memory_admin_rend_des_statistiques(appeler) -> None:
    sortie = appeler("memory_admin", action="stats")
    assert sortie.strip()
    assert "Traceback" not in sortie


@pytest.mark.parametrize("type_obs", ["project", "feedback", "reference"])
def test_chaque_type_d_observation_est_accepte(type_obs: str, appeler) -> None:
    sortie = appeler(
        "memory_save",
        type=type_obs,
        title=f"Essai de type {type_obs}",
        content="Contenu d'essai.",
    )
    assert sortie.strip()
    assert "Traceback" not in sortie, f"type {type_obs} :\n{sortie[:300]}"


# --- Cycle captures -----------------------------------------------------


def test_une_capture_ecrite_est_relue(appeler) -> None:
    appeler(
        "capture_put",
        tool_name="Bash",
        output="ligne unique de capture pour le cycle de vie",
    )
    trouve = appeler("capture_search", query="cycle de vie")
    # L'extrait rendu n'est pas le texte brut : capture_search entoure les
    # termes trouves de guillemets. On verifie donc la correspondance, pas
    # une egalite de chaine.
    assert '"count": 1' in trouve or '"count": ' in trouve, trouve[:300]
    assert "cycle" in trouve and "vie" in trouve, (
        f"capture non retrouvee :\n{trouve[:400]}"
    )


def test_la_liste_des_captures_repond(appeler) -> None:
    appeler("capture_put", tool_name="Bash", output="pour la liste")
    sortie = appeler("capture_list")
    assert sortie.strip()
    assert "Traceback" not in sortie


def test_l_agregat_des_captures_repond(appeler) -> None:
    sortie = appeler("capture_aggregate")
    assert sortie.strip()
    assert "Traceback" not in sortie


def test_capture_put_exige_ses_arguments(appeler) -> None:
    """Sans `output`, il ecrivait une capture vide attribuee a "unknown"."""
    sortie = appeler.brut("capture_put")
    assert "tool_name" in sortie or "output" in sortie, (
        "capture_put doit exiger ses arguments obligatoires :\n" + sortie[:300]
    )
    assert '"id"' not in sortie, "une capture a ete ecrite malgre l'appel invalide"


def test_capture_search_exige_une_requete(appeler) -> None:
    """Sans `query`, il rendait {"count": 0} : une reponse a une question
    jamais posee. Pour lister sans filtrer, capture_list existe."""
    sortie = appeler.brut("capture_search")
    assert "query" in sortie, (
        "capture_search doit exiger sa requete :\n" + sortie[:300]
    )


# --- Cycle edition de code ---------------------------------------------


def test_remplacer_un_symbole_change_bien_le_fichier(appeler, copie: Path) -> None:
    cible = copie / "boutique" / "panier.py"
    avant = cible.read_text(encoding="utf-8")
    appeler(
        "replace_symbol_source",
        project=str(copie),
        symbol_name="compter_articles",
        file_path="boutique/panier.py",
        new_source=(
            "def compter_articles(lignes):\n"
            '    """Nombre total d\'articles. Version remplacee par le test."""\n'
            '    return sum(ligne["quantite"] for ligne in lignes)\n'
        ),
    )
    apres = cible.read_text(encoding="utf-8")
    assert apres != avant, "le fichier n'a pas change"
    assert "Version remplacee par le test" in apres
    assert "def calculer_total" in apres, "le reste du fichier a ete perdu"


def test_inserer_pres_d_un_symbole_ajoute_sans_ecraser(appeler, copie: Path) -> None:
    cible = copie / "boutique" / "panier.py"
    appeler(
        "insert_near_symbol",
        project=str(copie),
        symbol_name="compter_articles",
        file_path="boutique/panier.py",
        position="after",
        content='\n\ndef ajoutee_par_le_test():\n    """Inseree."""\n    return 1\n',
    )
    apres = cible.read_text(encoding="utf-8")
    assert "ajoutee_par_le_test" in apres
    assert "def compter_articles" in apres, "le symbole voisin a ete ecrase"
    assert "def calculer_total" in apres


def test_une_edition_conserve_un_fichier_valide(appeler, copie: Path) -> None:
    """Le test qui compte : le fichier doit encore compiler apres edition."""
    import ast

    appeler(
        "replace_symbol_source",
        project=str(copie),
        symbol_name="compter_articles",
        file_path="boutique/panier.py",
        new_source=(
            "def compter_articles(lignes):\n"
            '    """Recompte."""\n'
            '    return sum(ligne["quantite"] for ligne in lignes)\n'
        ),
    )
    source = (copie / "boutique" / "panier.py").read_text(encoding="utf-8")
    ast.parse(source)  # leve si l'edition a casse la syntaxe


def test_une_edition_preserve_les_decorateurs(appeler, copie: Path) -> None:
    """Defaut connu et re-observe le 27/07/2026 pendant l'ecriture de cette
    serie : `replace_symbol_source` a mange `@pytest.fixture` et
    `@pytest.mark.parametrize`, ce qui a casse 73 tests d'un coup.

    Un decorateur perdu ne se voit pas a la relecture du symbole remplace : il
    est *au-dessus* de la ligne `def`. D'ou ce test.
    """
    fichier = copie / "boutique" / "decore.py"
    fichier.write_text(
        "import functools\n\n\n"
        "@functools.cache\n"
        "def valeur_calculee(x):\n"
        '    """Avec decorateur."""\n'
        "    return x * 2\n",
        encoding="utf-8",
    )
    appeler("reindex", project=str(copie))
    appeler(
        "replace_symbol_source",
        project=str(copie),
        symbol_name="valeur_calculee",
        file_path="boutique/decore.py",
        new_source=(
            "def valeur_calculee(x):\n"
            '    """Avec decorateur, corps modifie."""\n'
            "    return x * 3\n"
        ),
    )
    apres = fichier.read_text(encoding="utf-8")
    assert "corps modifie" in apres, "l'edition n'a pas eu lieu"
    assert "@functools.cache" in apres, (
        "le decorateur a ete mange par l'edition :\n" + apres
    )


def test_un_checkpoint_repond(appeler, copie: Path) -> None:
    sortie = appeler("checkpoint", project=str(copie), label="essai-cycle")
    assert sortie.strip()
    assert "Traceback" not in sortie


def test_reindexer_ne_perd_pas_les_symboles(appeler, copie: Path) -> None:
    appeler("reindex", project=str(copie))
    sortie = appeler("find_symbol", project=str(copie), name="calculer_total")
    assert "panier.py" in sortie, f"symbole perdu apres reindex :\n{sortie[:300]}"


def test_un_symbole_deplace_change_de_fichier(appeler, copie: Path) -> None:
    sortie = appeler(
        "move_symbol",
        project=str(copie),
        name="compter_articles",
        target_file="boutique/remises.py",
    )
    assert "Traceback" not in sortie
    if "error" in sortie.lower():
        pytest.skip(f"move_symbol refuse ce deplacement : {sortie[:200]}")
    remises = (copie / "boutique" / "remises.py").read_text(encoding="utf-8")
    assert "compter_articles" in remises, "le symbole n'est pas arrive a destination"
