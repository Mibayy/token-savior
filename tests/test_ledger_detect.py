import pytest

from token_savior.memory import ledger


@pytest.mark.parametrize("text", [
    "je t'ai déjà dit de regarder les logs",
    "Tu devais vérifier avant de push",
    "je te rappelle qu'on utilise Token Savior",
    "combien de fois je dois te le dire",
    "encore une fois tu as oublié",
])
def test_detects_corrections(text):
    assert ledger.detect_correction(text) is not None


@pytest.mark.parametrize("text", [
    "peux-tu ajouter une fonction ici",
    "installe hermes sur le vps",
    "",
    "merci c'est parfait",
    "relance le build encore une fois",
    "combien de fois faut-il relancer le service",
])
def test_ignores_non_corrections(text):
    assert ledger.detect_correction(text) is None
