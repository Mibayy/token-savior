"""Les deux formes de script qui rendent un vide sans rien dire.

Mesure le 09/08/2026 en usage reel. Le worker enveloppe le corps du script
dans `(async () => { <corps> })()`. Deux ecritures spontanees echouent :

1. `export default async function () {...}` : SyntaxError, le contexte `vm`
   n'est pas un module ES. Bruyant, donc benin.
2. une IIFE `(async () => {...})()` sans `return` : elle rend une promesse que
   personne n'attend. Resultat mesure : `value: null`, `error: null`,
   `tool_calls: 1` alors que le script en ecrivait quatre. **Aucun message.**

Le second cas est celui qui coute cher : un agent lit un resultat vide sans
erreur, conclut que `ts_execute` est casse, et repart en appels unitaires. Le
gain du Code Mode est perdu par une faute de forme silencieuse.

Sens de l'erreur retenu : refuser tot et bruyamment. Un script refuse coute un
aller-retour ; un resultat vide qui se lit comme une panne coute l'abandon de
l'outil.
"""

from __future__ import annotations

import asyncio
import json

import pytest


def _lancer(script: str) -> str:
    from token_savior.server import _handle_ts_execute

    return asyncio.run(_handle_ts_execute({"script": script}))[0].text


class TestFormesRefusees:
    def test_export_default_est_refuse_avec_la_marche_a_suivre(self) -> None:
        texte = _lancer("export default async function () { return 1; }")
        assert "Error:" in texte
        assert "module ES" in texte, "le message doit dire pourquoi, pas seulement que"
        assert "return" in texte, "le message doit donner la forme attendue"

    def test_iife_sans_return_est_refusee(self) -> None:
        """Le cas dangereux : sans ce garde-fou, il rendait null en silence."""
        texte = _lancer("(async () => { const x = 1 + 1; })()")
        assert "Error:" in texte
        assert "IIFE" in texte
        assert "null" in texte, "le message doit nommer le symptome qu'il evite"

    def test_iife_prefixee_par_return_reste_acceptee(self) -> None:
        """Elle est correcte : la promesse est bien rendue, donc attendue."""
        texte = _lancer("return (async () => 7)();")
        charge = json.loads(texte)
        assert charge["error"] is None
        assert charge["value"] == 7


class TestFormesAcceptees:
    def test_un_corps_normal_passe(self) -> None:
        charge = json.loads(_lancer("const a = 2; return a * 21;"))
        assert charge["error"] is None
        assert charge["value"] == 42

    def test_le_mot_export_dans_une_chaine_ne_declenche_rien(self) -> None:
        """Le garde-fou ne regarde que la tete du script, pas son contenu.

        Un script qui manipule le mot « export » (frequent : exports WhatsApp,
        export PDF) ne doit pas etre pris pour un module ES.
        """
        charge = json.loads(_lancer('const t = "export default"; return t.length;'))
        assert charge["error"] is None
        assert charge["value"] == len("export default")

    def test_une_iife_avec_return_interne_passe(self) -> None:
        """`return` present : on ne refuse pas, quitte a laisser passer un cas
        tordu. Refuser un script valide serait pire que le laisser courir."""
        charge = json.loads(_lancer("const f = async () => { return 5; }; return await f();"))
        assert charge["error"] is None
        assert charge["value"] == 5


class TestIndiceQuandLeRetourManque:
    def test_un_script_sans_return_qui_appelle_un_outil_recoit_un_indice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La forme est valide mais le `return` manque : on le dit.

        C'est la moitie du probleme que le refus ne couvre pas : un corps nu,
        parfaitement legal, qui appelle des outils et ne rend rien.
        """
        from token_savior import server

        monkeypatch.setattr(server, "_track_call", lambda *a, **k: None)
        monkeypatch.setattr(
            server, "_dispatch_tool",
            lambda name, args, rec: [server.TextContent(type="text", text='{"ok": true}')],
        )
        charge = json.loads(_lancer('await tools.get_git_status({});'))
        assert charge["value"] is None
        assert charge["error"] is None
        assert "hint" in charge, "un vide sans explication se lit comme une panne"
        assert "return" in charge["hint"]

    def test_pas_d_indice_quand_le_script_rend_bien_une_valeur(self) -> None:
        charge = json.loads(_lancer("return 1;"))
        assert "hint" not in charge
