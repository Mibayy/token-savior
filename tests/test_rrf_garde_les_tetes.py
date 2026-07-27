"""La fusion RRF ne doit pas enterrer ce qu'une jambe a juge meilleur.

Trouve le 27/07/2026 en instrumentant les deux jambes SEPAREMENT sur la vraie
base, au lieu de lire le resultat fusionne. La jambe lexicale placait 11 cibles
sur 12 au rang 1 ; apres fusion, trois tombaient au rang 4, au rang 6, ou
sortaient de la sortie.

Le mecanisme est arithmetique. Avec k = 60 :

    classe 1 dans UNE liste   -> 1/61 = 0,0164
    classe 3 dans DEUX listes -> 2/63 = 0,0317

Le mediocre partout bat l'excellent quelque part.

Mesure sur 45 observations reelles, jeu NEUTRE : chaque requete est batie sur
des mots du CONTENU absents du TITRE, pour qu'aucune jambe ne parte avec un
avantage offert.

    RRF nu                 39/45 trouvees, rang moyen 2,08
    garantie de presence   39/45 trouvees, rang moyen 2,08
    garantie de position   39/45 trouvees, rang moyen 1,26

Un premier jeu, dont les requetes venaient des titres, avait fait retenir la
garantie de PRESENCE par prudence. Refaite sans ce biais, la mesure montre que
la presence ne change strictement rien et que la position tient. La prudence
etait de la timidite.

Aucun test existant ne pouvait attraper le defaut : ils verifient que RRF
favorise les identifiants partages, ce qui est precisement le comportement qui
le causait.
"""
from __future__ import annotations

from token_savior.memory.search import RRF_K, rrf_merge


def _lignes(*ids: int) -> list[dict]:
    return [{"id": i, "title": f"obs-{i}"} for i in ids]


def test_la_tete_d_une_jambe_passe_devant_le_consensus() -> None:
    """Le cas exact mesure : excellent dans une liste, absent de l'autre.

    100 est classe 1 par la premiere jambe et ignore par la seconde : il
    marque 1/61. Les elements 7, 8 et 9 sont mediocres dans les deux et
    marquent chacun 2/63, presque le double. Sans garantie, 100 sortait
    d'une fenetre de 3.
    """
    ids = [r["id"] for r in rrf_merge(_lignes(100, 7, 8, 9), _lignes(7, 8, 9, 200), limit=3)]
    assert ids[0] == 100, (
        f"la tete de la premiere jambe doit passer devant, obtenu {ids}. "
        "Sans cela l'arithmetique de RRF laisse des resultats mediocres dans "
        "les deux listes evincer le meilleur d'une seule."
    )


def test_les_deux_tetes_sont_devant_dans_l_ordre_des_listes() -> None:
    """Garantie symetrique : aucune jambe n'est privilegiee.

    Rien ne dit que la jambe lexicale est toujours passee en premier ni
    qu'elle est toujours la plus fiable. Proteger seulement `listes[0]`
    marcherait aujourd'hui et casserait au premier appelant qui inverse.
    L'ordre entre les deux tetes suit l'ordre des listes, sinon le meme appel
    rendrait un classement different au gre du tri.
    """
    ids = [r["id"] for r in rrf_merge(_lignes(10, 2, 3), _lignes(20, 2, 3), limit=4)]
    assert ids[:2] == [10, 20], ids


def test_le_score_classe_tout_le_reste() -> None:
    """La garantie ne concerne que les tetes, pas le classement general.

    Une fois les tetes placees, un element partage par les deux jambes doit
    toujours devancer un element vu par une seule. C'est l'intention d'origine
    de la fusion et elle reste vraie.
    """
    ids = [r["id"] for r in rrf_merge(_lignes(1, 5, 9), _lignes(2, 5, 8), limit=5)]
    assert ids[:2] == [1, 2], ids
    assert ids.index(5) < ids.index(9), f"le partage 5 doit devancer le solitaire 9 : {ids}"


def test_une_seule_liste_reste_dans_son_ordre() -> None:
    """Avec une seule jambe, la garantie ne doit rien changer du tout."""
    assert [r["id"] for r in rrf_merge(_lignes(3, 1, 2), limit=3)] == [3, 1, 2]


def test_une_liste_vide_ne_casse_rien() -> None:
    """La jambe vectorielle vide, cas frequent quand sqlite-vec manque."""
    assert [r["id"] for r in rrf_merge(_lignes(1, 2), [], limit=2)] == [1, 2]
    assert rrf_merge([], [], limit=5) == []


def test_les_scores_restent_ceux_que_rrf_a_calcules() -> None:
    """La garantie deplace une ligne, elle ne doit pas falsifier son score.

    Sinon un futur lecteur croira que la tete a gagne au score alors qu'elle a
    ete protegee, et il retirera la garantie en pensant qu'elle ne sert a rien.
    """
    fusion = rrf_merge(_lignes(100, 7), _lignes(7, 8), limit=3)
    par_id = {r["id"]: r["_rrf_score"] for r in fusion}
    assert par_id[100] == round(1.0 / (RRF_K + 1), 6)
    assert par_id[7] == round(1.0 / (RRF_K + 2) + 1.0 / (RRF_K + 1), 6)
    assert par_id[7] > par_id[100], (
        "7 a bien le meilleur score et se retrouve pourtant derriere 100 : "
        "c'est exactement le comportement voulu. Si cette assertion tombe, le "
        "test ne prouve plus rien."
    )
