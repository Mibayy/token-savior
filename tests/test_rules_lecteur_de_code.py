"""Les deux regles qui bornent le shell quand il sert de lecteur de code.

`grep` et `cat` avaient deja un compacteur PostToolUse. Un compacteur ne peut
rien economiser a ce moment-la : la sortie est deja partie vers le modele. Ces
regles-ci vivent en PreToolUse, donc elles comptent.

Chaque test dit aussi ce que la regle ne doit PAS toucher : une borne posee
par l'appelant, une composition shell, une concatenation voulue.
"""
from __future__ import annotations

from token_savior.bash_rewriter.rules import RULES, is_unsafe_to_rewrite


def _reecrire(cmd: str) -> str | None:
    """La commande reecrite, ou None si aucune regle ne s'applique."""
    if is_unsafe_to_rewrite(cmd):
        return None
    for regle in RULES:
        if regle.matches(cmd):
            return regle.apply(cmd)
    return None


class TestGrep:
    def test_un_grep_nu_est_borne(self) -> None:
        assert _reecrire("grep -rn motif src/") == "grep -rn motif src/ -m 20"

    def test_rg_aussi(self) -> None:
        assert _reecrire("rg motif") == "rg motif -m 20"

    def test_une_borne_deja_posee_est_respectee(self) -> None:
        assert _reecrire("grep -m 3 motif f.py") is None

    def test_un_comptage_n_est_pas_borne(self) -> None:
        """`grep -c` rend un nombre : le borner fausserait le nombre."""
        assert _reecrire("grep -c motif f.py") is None

    def test_la_liste_des_fichiers_n_est_pas_bornee(self) -> None:
        """`-l` rend un fichier par ligne, pas les correspondances."""
        assert _reecrire("grep -rl motif src/") is None

    def test_un_grep_silencieux_n_est_pas_touche(self) -> None:
        assert _reecrire("grep -q motif f.py") is None

    def test_les_drapeaux_groupes_comptent(self) -> None:
        """`-rl` porte `-l` sans l'ecrire seul. Le premier jet bornait
        pourtant cette commande, qui ne rend que des noms de fichiers."""
        for cmd in ("grep -rl motif src/", "grep -rc motif src/",
                    "grep -rq motif src/", "grep -ro motif src/"):
            assert _reecrire(cmd) is None, cmd

    def test_une_valeur_collee_au_drapeau_compte_aussi(self) -> None:
        assert _reecrire("grep -m20 motif f.py") is None

    def test_un_groupe_sans_drapeau_bornant_est_bien_borne(self) -> None:
        assert _reecrire("grep -rn motif src/") == "grep -rn motif src/ -m 20"

    def test_un_tube_interdit_toute_reecriture(self) -> None:
        assert _reecrire("grep -rn motif src/ | wc -l") is None


class TestCat:
    def test_un_cat_de_fichier_entier_devient_un_head(self) -> None:
        assert _reecrire("cat gros.py") == "head -n 400 gros.py"

    def test_une_concatenation_voulue_est_laissee_tranquille(self) -> None:
        """`cat a b` concatene exprès : le tronquer changerait le resultat."""
        assert _reecrire("cat a.py b.py") is None

    def test_un_cat_avec_drapeau_n_est_pas_touche(self) -> None:
        assert _reecrire("cat -n gros.py") is None

    def test_une_redirection_interdit_la_reecriture(self) -> None:
        assert _reecrire("cat gros.py > copie.py") is None


class TestRaisons:
    def test_les_deux_regles_nomment_l_outil_de_remplacement(self) -> None:
        """Borner sans dire ou aller ensuite ne fait que degrader la reponse."""
        par_nom = {r.name: r.reason for r in RULES}
        assert "search_codebase" in par_nom["grep-cap"]
        assert "read_lines" in par_nom["cat-cap"]
        assert "get_function_source" in par_nom["cat-cap"]
