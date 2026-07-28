"""Les annotations MCP doivent couvrir tous les outils, sans exception.

Le risque que ces tests ferment : un outil qui ecrit sur disque est ajoute a
TOOL_SCHEMAS, personne ne pense a le declarer mutateur, et il part au client
annonce `readOnlyHint=true`. Un juge en lecture seule l'appellerait alors en
toute confiance. Une liste maintenue a la main derive en silence ; ces tests
transforment la derive en echec de CI.
"""

from __future__ import annotations

import pytest

from token_savior.tool_annotations import (
    DESTRUCTIVE_TOOLS,
    IDEMPOTENT_MUTATORS,
    MUTATING_TOOLS,
    OPEN_WORLD_TOOLS,
    annotations_for,
    read_only_tool_names,
)
from token_savior.tool_schemas import TOOL_SCHEMAS


def test_every_classified_tool_exists():
    """Aucun nom fantome dans les ensembles de classification.

    Un outil renomme ou supprime laisse son ancien nom dans MUTATING_TOOLS ; la
    protection devient alors decorative et personne ne le voit.
    """
    known = set(TOOL_SCHEMAS)
    for label, names in (
        ("MUTATING_TOOLS", MUTATING_TOOLS),
        ("DESTRUCTIVE_TOOLS", DESTRUCTIVE_TOOLS),
        ("IDEMPOTENT_MUTATORS", IDEMPOTENT_MUTATORS),
        ("OPEN_WORLD_TOOLS", OPEN_WORLD_TOOLS),
    ):
        orphelins = sorted(names - known)
        assert not orphelins, f"{label} cite des outils inexistants : {orphelins}"


def test_destructive_and_idempotent_are_mutators():
    """Un outil ne peut pas etre destructeur ou mutateur-idempotent sans muter."""
    assert not (DESTRUCTIVE_TOOLS - MUTATING_TOOLS)
    assert not (IDEMPOTENT_MUTATORS - MUTATING_TOOLS)


def test_destructive_and_idempotent_are_exclusive():
    """Rien ne peut etre a la fois destructeur et sans effet supplementaire.

    Exception assumee : une suppression est les deux (supprimer deux fois
    laisse le meme etat). On l'autorise explicitement plutot que par accident.
    """
    suppressions = {"capture_purge", "memory_delete"}
    chevauchement = (DESTRUCTIVE_TOOLS & IDEMPOTENT_MUTATORS) - suppressions
    assert not chevauchement, f"classement contradictoire : {sorted(chevauchement)}"


@pytest.mark.parametrize("name", sorted(TOOL_SCHEMAS))
def test_every_tool_gets_complete_annotations(name):
    """Les quatre hints sont produits, et typés bool, pour chaque outil."""
    ann = annotations_for(name)
    assert set(ann) == {
        "readOnlyHint",
        "destructiveHint",
        "idempotentHint",
        "openWorldHint",
    }
    assert all(isinstance(v, bool) for v in ann.values())


def test_read_only_tools_are_never_destructive():
    """Le cas qui compte : rien d'annonce lisible ne peut detruire."""
    for name in TOOL_SCHEMAS:
        ann = annotations_for(name)
        if ann["readOnlyHint"]:
            assert not ann["destructiveHint"], name
            assert ann["idempotentHint"], name


def test_known_mutators_are_flagged():
    """Garde-fou en dur sur les outils dont la nature ne doit jamais glisser.

    Ecrits en litteral : si quelqu'un retire `replace_symbol_source` de
    MUTATING_TOOLS, le test qui derive tout de cet ensemble ne verrait rien.
    """
    for name in (
        "replace_symbol_source",
        "move_symbol",
        "edit_lines_in_symbol",
        "add_field_to_model",
        "insert_near_symbol",
        "memory_delete",
        "memory_save",
        "reindex",
        "switch_project",
        "ts_execute",
        "run_project_action",
        "run_impacted_tests",
    ):
        assert name in TOOL_SCHEMAS, f"outil disparu du manifeste : {name}"
        assert not annotations_for(name)["readOnlyHint"], f"{name} annonce lisible"


def test_known_readers_are_flagged():
    """Symetrique : les outils de lecture pure ne doivent pas devenir opaques."""
    for name in (
        "get_function_source",
        "get_full_context",
        "search_codebase",
        "find_symbol",
        "list_files",
        "get_git_status",
        "memory_search",
    ):
        assert name in TOOL_SCHEMAS, f"outil disparu du manifeste : {name}"
        ann = annotations_for(name)
        assert ann["readOnlyHint"], f"{name} devrait etre lisible"
        assert not ann["openWorldHint"], f"{name} ne sort pas de la machine"


# Verbes qui, dans un nom d'outil, annoncent une ecriture. La liste est
# volontairement large : un faux positif se resout en une ligne ci-dessous,
# un faux negatif expose un mutateur annonce comme lisible.
_VERBES_ECRITURE = (
    "add_", "apply_", "build", "checkpoint", "delete", "edit_", "execute",
    "insert_", "move_", "purge", "put", "reindex", "replace_", "run_",
    "save", "set_", "switch_", "write",
)

# Outils dont le nom contient un verbe d'ecriture mais qui ne touchent a rien.
# Toute entree ici est une derogation assumee, pas un oubli.
_LECTEURS_MALGRE_LE_NOM: frozenset[str] = frozenset({
    "build_commit_summary",  # produit un texte, n'ecrit pas
    "discover_project_actions",  # liste les actions, ne les lance pas
    "get_edit_context",  # rassemble le contexte AVANT une edition, sans editer
})


def test_no_unclassified_writer_slips_through():
    """Cherche l'ABSENCE de classification, pas sa presence.

    `annotations_for` traite un nom inconnu comme lisible. C'est le bon defaut
    a l'execution, mais ca veut dire qu'un mutateur ajoute sans etre declare
    partirait au client annonce `readOnlyHint=true` sans qu'aucun autre test
    ne bronche. Ce test-ci est le seul qui attrape ce cas.
    """
    suspects = sorted(
        name
        for name in TOOL_SCHEMAS
        if any(v in name for v in _VERBES_ECRITURE)
        and name not in MUTATING_TOOLS
        and name not in _LECTEURS_MALGRE_LE_NOM
    )
    assert not suspects, (
        "outils au nom d'ecriture mais absents de MUTATING_TOOLS : "
        f"{suspects}. Classe-les, ou ajoute-les a _LECTEURS_MALGRE_LE_NOM "
        "en disant pourquoi ils n'ecrivent rien."
    )


def test_read_only_subset_matches_annotations():
    """`read_only_tool_names` et `annotations_for` ne peuvent pas diverger."""
    derive = read_only_tool_names(TOOL_SCHEMAS)
    attendu = {n for n in TOOL_SCHEMAS if annotations_for(n)["readOnlyHint"]}
    assert derive == attendu


def test_list_tools_advertises_annotations():
    """Le handler protocole transmet bien les hints, pas seulement le module."""
    import asyncio

    from token_savior.server import list_tools

    tools = asyncio.run(list_tools())
    assert tools, "aucun outil annonce"
    for t in tools:
        assert t.annotations is not None, f"{t.name} sans annotations"
        assert isinstance(t.annotations.readOnlyHint, bool), t.name

    par_nom = {t.name: t for t in tools}
    if "get_function_source" in par_nom:
        assert par_nom["get_function_source"].annotations.readOnlyHint is True
        assert par_nom["get_function_source"].annotations.destructiveHint is False
    if "replace_symbol_source" in par_nom:
        assert par_nom["replace_symbol_source"].annotations.readOnlyHint is False
        assert par_nom["replace_symbol_source"].annotations.destructiveHint is True
