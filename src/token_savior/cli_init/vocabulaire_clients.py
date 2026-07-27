"""Source unique du vocabulaire de chaque client agent.

Pourquoi ce module existe. Les bundles de hooks etaient trois JSON edites a la
main, et ils avaient diverge en silence :

* le bundle Claude ne cablait que `SessionStart` et `Stop`. Les cinq autres
  scripts du moteur memoire etaient livres dans la roue PyPI et branches nulle
  part : un nouvel installeur recevait un tiers du moteur.
* le bundle Gemini filtrait `run_shell_command|mcp__plugin_token-savior`, donc
  aucun outil d'edition ni de lecture de Gemini. Le rappel des observations
  `ruled_out` avant une mutation -- le garde-fou le plus utile du lot -- ne
  pouvait pas se declencher.
* le bundle Codex filtrait `^(Bash|mcp__token-savior...)$`, meme angle mort.

Tant que les trois fichiers sont ecrits a la main, ils rederiveront. Les
bundles sont donc **generes** depuis ce module par
`scripts/generer_bundles_hooks.py`, et `tests/test_bundles_hooks_parite.py`
verifie que les fichiers livres correspondent bien a ce qui est declare ici.

Provenance des noms. Ce qui suit distingue ce qui a ete mesure de ce qui est
defensif, parce que la difference compte pour qui relit :

* Gemini : les huit noms d'outils ont ete releves dans le paquet installe
  `@google/gemini-cli` (occurrences litterales), et les evenements dans la
  version 0.38.1.
* Claude Code : noms observes directement dans les charges de hook de cette
  session.
* Codex : les evenements ont ete extraits du binaire `codex` 0.145.0. Le
  binaire est compresse et n'expose aucun nom d'outil litteral, donc seul
  `Bash` est verifie ; les autres sont declares par prudence. Un nom en trop
  ne coute rien -- le matcher ne le rencontre jamais. Un nom manquant rend le
  hook muet, ce qui est precisement le defaut corrige ici.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Outils Token Savior surveilles, cote MCP                                     #
# --------------------------------------------------------------------------- #
# Doit rester aligne sur CODE_TOOLS_RE / EDIT_TOOLS_RE dans
# hooks/memory-pretooluse.sh ; le test de parite le verifie.
TS_OUTILS_LECTURE = (
    "get_function_source",
    "get_class_source",
    "get_edit_context",
    "find_symbol",
    "get_file_dependencies",
)
TS_OUTILS_EDITION = (
    "replace_symbol_source",
    "insert_near_symbol",
    "apply_symbol_change_and_validate",
    "apply_symbol_change_validate_with_rollback",
)

# Delais en millisecondes, unite de reference. Codex les exprime en secondes :
# la conversion est faite a la generation, jamais recopiee a la main. Recopier
# `timeout: 120000` dans une config Codex demanderait 33 heures d'attente.
DELAIS_MS = {
    "session_start": 3_000,
    "user_prompt": 1_000,
    "pre_tool": 2_000,
    "post_tool": 3_000,
    "pre_compact": 5_000,
    "stop": 120_000,
    "session_end": 120_000,
}

# Script invoque pour chaque evenement canonique, et son argument eventuel.
SCRIPTS = {
    "session_start": "memory-session-start.sh",
    "user_prompt": "memory-userprompt.sh",
    "pre_tool": "memory-pretooluse.sh",
    "post_tool": "memory-posttooluse.sh",
    "pre_compact": "memory-precompact.sh",
    "stop": "memory-session-stop.sh stop",
    "session_end": "memory-session-stop.sh end",
}


@dataclass(frozen=True)
class Client:
    """Un client agent : ses evenements, son unite de temps, ses outils."""

    nom: str
    # evenement canonique -> nom chez ce client. Absent = non porte.
    evenements: dict[str, str]
    unite_delai: str  # "ms" ou "s"
    prefixe_mcp: str
    shell: tuple[str, ...]
    edition: tuple[str, ...]
    lecture: tuple[str, ...]
    recuperation: tuple[str, ...]  # web fetch, pour le hook post-outil
    ancre_matcher: bool = False  # entoure le motif de ^( )$
    note: str = ""
    # Modes du hook pre-outil actives par defaut chez ce client.
    # `lecture` est volontairement absent : voir MODES_PRE_TOOL_DEFAUT.
    modes_pre_tool: tuple[str, ...] = field(
        default=("ts_lecture", "ts_edition", "shell", "edition"))


# Le mode `read` du hook pre-outil existe et fonctionne, mais il n'est cable
# par defaut nulle part, et c'est deliberé : mesure sur ce VPS, un appel de
# hook coute ~197 ms, et une session de codage lit des fichiers en continu.
# Payer 200 ms sur chaque lecture pour un entete de contexte est un mauvais
# echange par defaut. Il s'active en ajoutant les noms d'outils de lecture au
# matcher pre-outil, ce que documente chaque bundle genere.
MODES_PRE_TOOL_DEFAUT = ("ts_lecture", "ts_edition", "shell", "edition")


CLIENTS: dict[str, Client] = {
    "claude": Client(
        nom="claude",
        evenements={
            "session_start": "SessionStart",
            "user_prompt": "UserPromptSubmit",
            "pre_tool": "PreToolUse",
            "post_tool": "PostToolUse",
            "pre_compact": "PreCompact",
            "stop": "Stop",
            "session_end": "SessionEnd",
        },
        unite_delai="ms",
        prefixe_mcp=r"mcp__token-savior[\w-]*__",
        shell=("Bash",),
        edition=("Edit", "Write", "MultiEdit", "NotebookEdit"),
        lecture=("Read", "NotebookRead"),
        recuperation=("WebFetch",),
        note=("Moteur memoire Token Savior pour Claude Code. Fusionner sous la "
              "cle 'hooks' de ~/.claude/settings.json. Delais en millisecondes. "
              "Ce bundle ne cablait que SessionStart et Stop jusqu'a la v4.19.0 : "
              "les cinq autres scripts etaient livres et branches nulle part."),
    ),
    "codex": Client(
        nom="codex",
        evenements={
            "session_start": "SessionStart",
            "user_prompt": "UserPromptSubmit",
            "pre_tool": "PreToolUse",
            "post_tool": "PostToolUse",
            "pre_compact": "PreCompact",
            "session_end": "SessionEnd",
            # Pas de Stop cote Codex : SubagentStop et Notification existent,
            # mais aucun equivalent du Stop de Claude.
        },
        unite_delai="s",
        prefixe_mcp=r"mcp__token-savior[\w-]*__",
        shell=("Bash", "shell", "local_shell"),
        edition=("Edit", "Write", "apply_patch"),
        lecture=("Read", "read_file"),
        recuperation=("WebFetch", "web_fetch"),
        ancre_matcher=True,
        note=("Moteur memoire Token Savior pour OpenAI Codex CLI. Fusionner dans "
              "~/.codex/hooks.json (ou <projet>/.codex/hooks.json). ATTENTION "
              "unites : les delais Codex sont en SECONDES, ceux de Claude Code en "
              "millisecondes -- ne pas recopier memory-hooks-config.json tel quel. "
              "Evenements extraits du binaire codex 0.145.0, qui expose exactement "
              "SessionStart, SessionEnd, PreToolUse, PostToolUse, UserPromptSubmit, "
              "PreCompact, Notification, SubagentStop. Sans equivalent donc non "
              "portes : Stop, StopFailure, ConfigChange. Parmi les noms d'outils, "
              "seul Bash est verifie ; les autres sont defensifs, le binaire etant "
              "compresse."),
    ),
    "gemini": Client(
        nom="gemini",
        evenements={
            "session_start": "SessionStart",
            "pre_tool": "BeforeTool",
            "post_tool": "AfterTool",
            "stop": "Stop",
            "session_end": "SessionEnd",
            # Gemini n'expose ni UserPromptSubmit ni PreCompact.
        },
        unite_delai="ms",
        prefixe_mcp="mcp__plugin_token-savior",
        shell=("run_shell_command",),
        edition=("replace", "write_file"),
        lecture=("read_file", "read_many_files"),
        recuperation=("web_fetch", "google_web_search"),
        note=("Moteur memoire Token Savior pour Gemini CLI. Fusionner sous la cle "
              "'hooks' de ~/.gemini/settings.json. Evenements du paquet "
              "@google/gemini-cli 0.38.1 : SessionStart, SessionEnd, Stop, "
              "BeforeTool, AfterTool, Notification. Absents cote Gemini donc non "
              "portes : UserPromptSubmit et PreCompact. Delais en MILLISECONDES "
              "(DEFAULT_HOOK_TIMEOUT = 6e4), comme Claude Code et contrairement a "
              "Codex. Noms d'outils releves dans le paquet installe : "
              "run_shell_command, read_file, read_many_files, write_file, replace, "
              "glob, search_file_content, web_fetch."),
    ),
}


def outils_pre_tool(client: Client, *, inclure_lecture: bool = False) -> list[str]:
    """Noms d'outils qui doivent declencher le hook pre-outil chez ce client.

    Le hook decide ensuite lui-meme du mode. Le matcher n'est la que pour eviter
    de demarrer un processus sur des outils sans rapport.
    """
    noms: list[str] = []
    noms += [client.prefixe_mcp + n for n in TS_OUTILS_LECTURE]
    noms += [client.prefixe_mcp + n for n in TS_OUTILS_EDITION]
    noms += list(client.shell)
    noms += list(client.edition)
    if inclure_lecture:
        noms += list(client.lecture)
    return noms


def outils_post_tool(client: Client) -> list[str]:
    """Le hook post-outil observe les commandes et les recuperations web."""
    return list(client.shell) + list(client.recuperation)


def motif(client: Client, noms: list[str]) -> str:
    """Assemble un motif d'alternance, ancre si le client l'exige."""
    # Les prefixes MCP contiennent deja `[\w-]*__`, donc regrouper les suffixes
    # produirait un motif illisible pour un gain nul : on garde l'alternance
    # explicite, qu'un humain peut relire et corriger.
    corps = "|".join(noms)
    return f"^({corps})$" if client.ancre_matcher else corps


def delai(client: Client, evenement: str) -> int:
    ms = DELAIS_MS[evenement]
    if client.unite_delai == "s":
        # Arrondi au superieur : un delai de 0 seconde tuerait le hook avant
        # qu'il ne demarre.
        return max(1, round(ms / 1000))
    return ms
