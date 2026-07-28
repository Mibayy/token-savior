"""Rendre visible une reponse tronquee par sa borne.

Le probleme, tel qu'il se paie
------------------------------
Vingt-neuf outils acceptent une borne (`limit`, `max_results`, `top_k`,
`max_groups`, `max_files`) et la respectent. Aucun ne dit s'il a coupe. Une
reponse de exactement `max_results` elements est donc indiscernable d'une
reponse complete.

L'appelant qui compte ces elements ne mesure pas ce qu'il croit mesurer : deux
comptages qui tombent tous les deux sur la borne ne veulent pas dire
« deux valeurs egales », ils veulent dire « deux troncatures ». L'erreur est
silencieuse par construction et ne se voit qu'en relisant la borne.

Ce module ne fait pas de la pagination. Il ne sait ni combien d'elements
existaient au total, ni ou reprendre : le handler a deja coupe quand on arrive
ici. Il ferme la classe d'erreur — « je ne sais pas que je n'ai pas tout » — et
laisse `has_more`/`next_offset`/`total_count` a une passe par handler.
"""

from __future__ import annotations

from typing import Any

# Bornes qui gouvernent le NOMBRE d'elements d'une collection. Ce sont les
# seules comparables a la longueur d'une liste. Tenu a jour par
# test_truncation::test_bornes_couvrent_les_schemas, qui echoue si un outil
# introduit une borne sous un nom inconnu d'ici.
NOMS_DE_BORNE = (
    "limit",
    "max_results",
    "top_k",
    "max_groups",
    "max_files",
    "max_tests",
    "max_issues",
    "max_cycles",
    "max_deps",
    "max_callers",
    "max_direct",
    "max_transitive",
)

# Bornes qui gouvernent une TAILLE (octets, lignes, caracteres), pas un nombre
# d'elements. Les comparer a la longueur d'une liste n'a aucun sens et
# produirait des alertes au hasard. Elles sont listees pour que le test de
# couverture ne les reclame pas, et pour que la distinction reste ecrite.
#
# `max_symbols_per_file` est ici pour une autre raison : il borne les symboles
# DANS chaque fichier, pas la collection rendue. Le comparer au total serait
# faux dans les deux sens.
BORNES_DE_TAILLE = (
    "max_bytes",
    "max_lines",
    "max_output_chars",
    "max_total_chars",
    "max_symbols_per_file",
)


def _entier_positif(valeur: object) -> int | None:
    if isinstance(valeur, bool):  # bool est un int en Python
        return None
    if isinstance(valeur, int) and valeur > 0:
        return valeur
    if isinstance(valeur, str) and valeur.isdigit() and int(valeur) > 0:
        return int(valeur)
    return None


def _bornes_demandees(arguments: dict[str, Any]) -> set[int]:
    """Toutes les bornes de collection exploitables passees par l'appelant.

    Un meme appel peut en porter plusieurs (`get_change_impact` a `max_direct`
    ET `max_transitive`). En retenir une seule, arbitrairement la premiere,
    ferait manquer la troncature gouvernee par l'autre.
    """
    valeurs = set()
    for cle in NOMS_DE_BORNE:
        n = _entier_positif(arguments.get(cle))
        if n is not None:
            valeurs.add(n)
    return valeurs


def _plus_longue_liste(result: object, profondeur: int = 0) -> tuple[str, int] | None:
    """(chemin, longueur) de la plus longue liste du resultat.

    On ne descend que de deux niveaux : au-dela, une liste longue est un detail
    interne (des lignes, des tokens) et pas la collection que la borne
    gouverne. Signaler celle-la produirait un faux positif.
    """
    if isinstance(result, list):
        return ("", len(result))
    if isinstance(result, dict) and profondeur < 2:
        meilleur: tuple[str, int] | None = None
        for cle, valeur in result.items():
            trouve = _plus_longue_liste(valeur, profondeur + 1)
            if trouve is None:
                continue
            chemin = cle if not trouve[0] else f"{cle}.{trouve[0]}"
            candidat = (chemin, trouve[1])
            if meilleur is None or candidat[1] > meilleur[1]:
                meilleur = candidat
        return meilleur
    return None


def notice_de_troncature(arguments: dict[str, Any], result: object) -> str | None:
    """Message a ajouter si le resultat est probablement coupe, sinon None.

    Le critere est l'egalite stricte entre la longueur rendue et la borne
    demandee. C'est volontairement conservateur : une collection qui compte
    pile `limit` elements sans avoir ete coupee produit un faux positif, mais
    un faux positif se lit et se corrige, alors qu'un faux negatif est
    exactement le silence qu'on cherche a supprimer.
    """
    bornes = _bornes_demandees(arguments)
    if not bornes:
        return None
    trouve = _plus_longue_liste(result)
    if trouve is None:
        return None
    chemin, longueur = trouve
    if longueur not in bornes:
        return None
    ou = f" ({chemin})" if chemin else ""
    return (
        f"[tronque] {longueur} element(s){ou} rendus, soit exactement la borne "
        f"demandee — il en reste probablement. Relance avec une borne plus "
        f"haute avant de conclure sur ce compte."
    )
