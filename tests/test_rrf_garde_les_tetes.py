"""La fusion RRF ne doit pas faire DISPARAITRE ce qu'une jambe a juge meilleur.

Trouve le 27/07/2026 en instrumentant les deux jambes SEPAREMENT sur la vraie
base, au lieu de regarder le resultat fusionne. La jambe lexicale placait 11
cibles sur 12 au rang 1 ; apres fusion, trois tombaient au rang 4, au rang 6,
ou sortaient completement.

Le mecanisme est arithmetique. Avec k = 60 :

    classe 1 dans UNE liste   -> 1/61 = 0,0164
    classe 3 dans DEUX listes -> 2/63 = 0,0317

Le mediocre partout bat l'excellent quelque part. C'est le mode de defaillance
connu de RRF quand une jambe est nettement plus fiable que l'autre, et avec une
fenetre etroite il ne le devance pas seulement, il l'evince.

Ce que ces tests exigent, et ce qu'ils n'exigent PAS. Ils exigent la PRESENCE
de chaque tete dans la sortie. Ils n'exigent pas sa position : le score garde
la main sur l'ordre, et un element sur lequel les deux jambes s'accordent a le
droit de rester premier. Cette limite est deliberee, voir la docstring de
`rrf_merge` : la garantie de position mesurait mieux (rang moyen 1,08 contre
2,71) mais sur un jeu d'evaluation dont les requetes sont fabriquees a partir
des titres cibles, ce qui avantage mecaniquement le lexical.

Aucun test existant ne pouvait attraper le defaut : ils verifient que RRF
favorise les identifiants partages, ce qui est precisement le comportement qui
le causait.
"""
from __future__ import annotations

from token_savior.memory.search import RRF_K, rrf_merge


def _lignes(*ids: int) -> list[dict]:
    return [{"id": i, "title": f"obs-{i}"} for i in ids]


def test_la_tete_d_une_jambe_ne_disparait_pas_de_la_fusion() -> None:
    """Le cas exact mesure : excellent dans une liste, absent de l'autre.

    L'element 100 est classe 1 par la premiere jambe et ignore par la
    seconde : il marque 1/61. Les elements 7, 8 et 9 sont mediocres dans les
    deux et marquent chacun 2/63, presque le double. Avec une fenetre de 3,
    100 sortait de la sortie.
    """
    fusion = rrf_merge(_lignes(100, 7, 8, 9), _lignes(7, 8, 9, 200), limit=3)
    ids = [r["id"] for r in fusion]
    assert 100 in ids, (
        f"la tete de la premiere jambe a disparu : {ids}. L'arithmetique de "
        "RRF a laisse des resultats mediocres dans les deux listes l'evincer."
    )


def test_la_tete_de_la_seconde_jambe_ne_disparait_pas_non_plus() -> None:
    """Garantie symetrique : aucune jambe n'est privilegiee.

    Rien ne dit que la jambe lexicale est toujours passee en premier ni
    qu'elle est toujours la plus fiable. Proteger seulement `listes[0]`
    marcherait aujourd'hui et casserait au premier appelant qui inverse.
    """
    ids = [r["id"] for r in rrf_merge(_lignes(1, 2, 3, 4), _lignes(500, 2, 3, 4), limit=3)]
    assert 500 in ids, f"la tete de la seconde jambe a disparu : {ids}"


def test_le_consensus_garde_la_premiere_place() -> None:
    """La garantie n'est PAS une promotion : elle interdit la disparition.

    Un element sur lequel les deux jambes s'accordent reste devant. C'est
    l'intention d'origine de la fusion et elle n'est pas remise en cause :
    on ne dispose pas d'une mesure non biaisee qui dirait le contraire.
    """
    fusion = rrf_merge(_lignes(1, 2, 7, 3), _lignes(9, 10, 7, 11), limit=5)
    ids = [r["id"] for r in fusion]
    assert ids[0] == 7, f"le consensus doit rester premier, obtenu {ids}"
    assert 1 in ids and 9 in ids, f"les deux tetes doivent figurer : {ids}"


def test_la_garantie_cede_la_derniere_place_pas_la_premiere() -> None:
    """Ou la tete atterrit quand il faut lui faire de la place.

    Elle prend la derniere place de la fenetre. Le reste du classement par
    score est preserve dans son ordre.
    """
    fusion = rrf_merge(_lignes(100, 7, 8, 9), _lignes(7, 8, 9, 200), limit=3)
    ids = [r["id"] for r in fusion]
    assert ids[-1] in (100, 200), ids
    assert ids[0] == 7, f"le mieux score reste premier : {ids}"


def test_une_seule_liste_reste_dans_son_ordre() -> None:
    """Avec une seule jambe, la garantie ne doit rien changer du tout."""
    assert [r["id"] for r in rrf_merge(_lignes(3, 1, 2), limit=3)] == [3, 1, 2]


def test_les_scores_restent_ceux_que_rrf_a_calcules() -> None:
    """La garantie deplace une ligne, elle ne doit pas falsifier son score.

    Sinon un futur lecteur croira que la tete a gagne au score alors qu'elle
    a ete protegee.
    """
    fusion = rrf_merge(_lignes(100, 7), _lignes(7, 8), limit=3)
    par_id = {r["id"]: r["_rrf_score"] for r in fusion}
    assert par_id[100] == round(1.0 / (RRF_K + 1), 6)
    assert par_id[7] == round(1.0 / (RRF_K + 2) + 1.0 / (RRF_K + 1), 6)
    assert par_id[7] > par_id[100], (
        "7 a bien le meilleur score. Si cette assertion tombe, le test ne "
        "prouve plus rien sur la garantie."
    )


def test_une_liste_vide_ne_casse_rien() -> None:
    """Le cas ou la jambe vectorielle n'a rien rendu, frequent en pratique."""
    assert [r["id"] for r in rrf_merge(_lignes(1, 2), [], limit=2)] == [1, 2]
    assert rrf_merge([], [], limit=5) == []
