"""Un argument obligatoire absent doit se dire, pas se cracher.

Regression du 27/07/2026. `get_call_chain` attend `from_name` et `to_name`.
Appele sans eux, son handler faisait `a["from_name"]` en acces direct et
laissait remonter `KeyError: 'from_name'`.

C'est precisement ce que `_require_name` existe pour empecher -- sa docstring
le dit : *"Found by auditing all 69 tools one by one: three of them answered
`Error: 'name'` -- the repr of a Python KeyError, nothing else. For an LLM
client that is the worst possible message."* Mais cet audit ne couvrait que
les outils dont l'argument s'appelle `name`. `get_call_chain`, avec ses
`from_name`/`to_name`, passait au travers.

Le garde-fou pose ici est generique, au niveau du dispatch, donc il vaut pour
tout outil quels que soient ses noms d'arguments. Et il ne traduit que si la
cle absente est reellement declaree obligatoire dans le schema : une KeyError
interne continue de remonter telle quelle, sinon on maquillerait un vrai
defaut en probleme d'appel.
"""

from __future__ import annotations

import pytest


def test_un_argument_obligatoire_absent_est_nomme() -> None:
    from token_savior import server

    message = server._message_argument_obligatoire(
        "get_call_chain", KeyError("from_name"),
    )
    assert message is not None
    # Il nomme l'argument manquant...
    assert "from_name" in message
    # ...les autres obligatoires...
    assert "to_name" in message
    # ...et comment s'en sortir si on ne connait pas le nom exact.
    assert "search_codebase" in message
    assert "KeyError" not in message


def test_une_erreur_interne_n_est_pas_maquillee() -> None:
    """Une KeyError qui n'est pas un argument declare doit remonter intacte."""
    from token_savior import server

    assert server._message_argument_obligatoire(
        "get_call_chain", KeyError("cle_interne_quelconque"),
    ) is None


def test_un_outil_sans_argument_obligatoire_ne_declenche_rien() -> None:
    from token_savior import server

    assert server._message_argument_obligatoire(
        "get_git_status", KeyError("peu_importe"),
    ) is None


@pytest.mark.parametrize("mauvaise_cle", [None, 42, ()])
def test_une_keyerror_sans_nom_exploitable_est_relayee(mauvaise_cle) -> None:
    from token_savior import server

    assert server._message_argument_obligatoire(
        "get_call_chain", KeyError(mauvaise_cle),
    ) is None


def test_le_dispatch_rend_le_message_au_lieu_de_planter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La branche except du dispatch, visee directement.

    On ne passe pas par un vrai projet : sans index enregistre le dispatch
    repond "No projects registered" bien avant d'atteindre le handler, et le
    test ne prouverait rien. On substitue donc un handler qui leve la KeyError
    exacte observee le 27/07/2026.
    """
    from token_savior import server

    def handler_fautif(args):
        raise KeyError("from_name")

    monkeypatch.setitem(server._META_HANDLERS, "get_call_chain", handler_fautif)
    sortie = server._dispatch_tool("get_call_chain", {"name": "quelconque"}, "")
    texte = "".join(getattr(bloc, "text", "") for bloc in sortie)
    assert "from_name" in texte
    assert "requires" in texte


def test_le_dispatch_ne_masque_pas_une_erreur_interne(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le pendant indispensable : une KeyError qui n'est pas un argument
    declare doit continuer de remonter, sinon ce garde-fou transformerait
    chaque vrai defaut en `argument manquant` et on ne le verrait jamais."""
    from token_savior import server

    def handler_casse(args):
        raise KeyError("une_cle_interne")

    monkeypatch.setitem(server._META_HANDLERS, "get_call_chain", handler_casse)
    with pytest.raises(KeyError, match="une_cle_interne"):
        server._dispatch_tool("get_call_chain", {"name": "quelconque"}, "")
