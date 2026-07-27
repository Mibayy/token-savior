"""Token Savior — structural code indexer with MCP server for AI-assisted development."""

# Single source of truth is pyproject.toml; read it from installed metadata so
# __version__ never drifts (it sat stale at "3.4.0" through the whole v4.x line,
# which masked that a stale build was installed -- audit 2026-07-04).
#
# Lu paresseusement : `importlib.metadata` coute ~47 ms a l'import, et ce cout
# etait paye par *tout* ce qui touche au paquet, dont les hooks, qui demarrent
# un interpreteur neuf a chaque outil appele. Mesure sur ce VPS : 169 ms pour
# `from token_savior import memory_db`, dont 47 ms ici, pour une chaine qu'un
# seul appelant lit (cache_ops.build_cache_key). Le module __getattr__ (PEP 562)
# garde la meme valeur et la meme source de verite, calculee au premier acces.

__all__ = ["__version__"]

_version_cache: str | None = None


def _lire_version() -> str:
    global _version_cache
    if _version_cache is None:
        # Le pyproject.toml qui accompagne ces sources l'emporte sur les
        # metadonnees de la distribution, et pas l'inverse.
        #
        # Mesure du 26/07/2026 sur ce VPS : le venv est installe en editable,
        # donc le code servi venait de /root/token-savior/src (4.19.0) pendant
        # que le dist-info gelait a 4.16.0, fige au dernier `pip install -e`.
        # `importlib.metadata` rendait donc 4.16.0, et compute_config_key()
        # sortait {"pkg": "4.16.0"} : quatre livraisons (4.17 a 4.19) ont
        # change l'index et la memoire sans jamais invalider le cache, ce qui
        # est exactement le defaut #61 que cette cle existe pour empecher.
        #
        # Un pyproject.toml n'est trouve qu'a cote d'un arbre source : sur une
        # installation normale en site-packages la recherche echoue et on
        # retombe sur les metadonnees, qui sont alors la bonne source.
        _version_cache = _version_depuis_pyproject()
        if _version_cache is None:
            try:
                from importlib.metadata import version as _pkg_version

                _version_cache = _pkg_version("token-savior-recall")
            except Exception:
                # Ni arbre source ni distribution installee. On garde un
                # marqueur explicite plutot qu'une version plausible.
                _version_cache = "0.0.0+unknown"
    return _version_cache


def _version_depuis_pyproject() -> str | None:
    """Version declaree dans le pyproject.toml qui accompagne ces sources.

    Sans dependance : `tomllib` est dans la bibliotheque standard depuis 3.11,
    et le paquet exige deja 3.10+ donc on retombe sur une lecture textuelle si
    absent. Toute erreur rend None, l'appelant garde son marqueur explicite.
    """
    from pathlib import Path

    racine = Path(__file__).resolve().parent
    for candidat in (racine.parent.parent, racine.parent, racine):
        fichier = candidat / "pyproject.toml"
        if not fichier.is_file():
            continue
        try:
            try:
                import tomllib

                donnees = tomllib.loads(fichier.read_text(encoding="utf-8"))
                version = (donnees.get("project") or {}).get("version")
                if version:
                    return str(version)
            except ModuleNotFoundError:
                import re

                trouve = re.search(
                    r'^\s*version\s*=\s*"([^"]+)"',
                    fichier.read_text(encoding="utf-8"),
                    re.MULTILINE,
                )
                if trouve:
                    return trouve.group(1)
        except Exception:
            return None
    return None


def __getattr__(nom: str) -> str:
    if nom == "__version__":
        return _lire_version()
    raise AttributeError(f"module {__name__!r} has no attribute {nom!r}")


def __dir__() -> list[str]:
    # Sans ca `dir(token_savior)` n'annoncerait plus __version__ alors qu'il
    # reste accessible : un attribut qui existe doit rester listable.
    return sorted({*globals(), "__version__"})
