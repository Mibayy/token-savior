"""Garde-fous sur le hook pack OpenClaw.

OpenClaw ne cable pas des commandes shell par evenement : il charge des *hook
packs*, dossiers contenant un `HOOK.md` (frontmatter YAML declarant les
evenements) et un `handler.js` (module ESM a export par defaut).

Deux regressions sont possibles et silencieuses, ce fichier les surveille.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_DIR = REPO_ROOT / "hooks" / "openclaw" / "token-savior-memory"

# Evenements deduits des predicats exportes par le module de hooks d'OpenClaw
# 2026.4.14 (isAgentBootstrapEvent, isGatewayStartupEvent, ...) et des HOOK.md
# livres avec le produit. Un evenement hors de cette liste ne leve pas
# d'erreur : le hook ne se declenche jamais.
OPENCLAW_EVENTS = frozenset({
    "agent:bootstrap",
    "gateway:startup",
    "command",
    "command:new",
    "command:reset",
    "session:patch",
    "session:compact:before",
    "session:compact:after",
    "message:received",
    "message:preprocessed",
    "message:transcribed",
    "message:sent",
})


def _hook_metadata() -> dict:
    """Extrait le bloc metadata.openclaw du frontmatter de HOOK.md."""
    text = (PACK_DIR / "HOOK.md").read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    start = front.index("{")
    blob = front[start:]
    # Le frontmatter est du YAML dont metadata est un objet JSON-like tolerant
    # aux virgules finales.
    blob = re.sub(r",(\s*[}\]])", r"\1", blob)
    return json.loads(blob)["openclaw"]


def test_pack_has_both_required_files() -> None:
    assert (PACK_DIR / "HOOK.md").is_file()
    assert (PACK_DIR / "handler.js").is_file()


def test_pack_declares_only_events_openclaw_exposes() -> None:
    events = set(_hook_metadata()["events"])
    inconnus = sorted(events - OPENCLAW_EVENTS)
    assert not inconnus, (
        f"HOOK.md declare des evenements absents d'OpenClaw: {inconnus}. "
        "Un evenement inconnu ne leve pas d'erreur, le hook ne part jamais."
    )


def test_handler_imports_no_openclaw_internals() -> None:
    """Les modules internes d'OpenClaw ont un nom hache par version.

    `import ... from "../../internal-hooks-D52pUqod.js"` casse a la mise a
    jour suivante. Le handler ne doit dependre que de modules natifs Node.
    """
    src = (PACK_DIR / "handler.js").read_text(encoding="utf-8")
    imports = re.findall(r'from\s+"([^"]+)"', src)
    non_natifs = [i for i in imports if not i.startswith("node:")]
    assert not non_natifs, (
        f"handler.js importe des modules non natifs: {non_natifs}. "
        "Les modules internes d'OpenClaw sont haches par version."
    )


def test_handler_exports_a_default() -> None:
    src = (PACK_DIR / "handler.js").read_text(encoding="utf-8")
    assert "as default" in src or "export default" in src


def test_handler_never_injects_under_a_reserved_bootstrap_name() -> None:
    """Le consommateur OpenClaw deduplique par nom de fichier.

    Injecter sous `AGENTS.md` ou `SOUL.md` ecraserait le vrai fichier de
    l'agent au lieu de s'y ajouter.
    """
    src = (PACK_DIR / "handler.js").read_text(encoding="utf-8")
    for reserve in ("AGENTS.md", "SOUL.md", "TOOLS.md", "IDENTITY.md", "USER.md"):
        assert f'"{reserve}"' not in src, f"handler.js injecte sous le nom reserve {reserve}"


def test_openclaw_bundle_points_at_the_pack_parent_directory() -> None:
    bundle = json.loads((REPO_ROOT / "hooks" / "openclaw-config.json").read_text(encoding="utf-8"))
    dirs = bundle["hooks"]["internal"]["load"]["extraDirs"]
    assert dirs == ["{{TS_HOOKS_DIR}}/openclaw"], dirs
    # Le dossier resolu doit contenir le pack.
    assert (REPO_ROOT / "hooks" / "openclaw" / "token-savior-memory").is_dir()


def test_openclaw_is_a_supported_agent_and_hermes_is_mcp_only() -> None:
    from token_savior.cli_init.agent_paths import (
        MCP_ONLY_AGENTS,
        SUPPORTED_AGENTS,
        hook_config_paths,
    )

    assert "openclaw" in SUPPORTED_AGENTS
    assert hook_config_paths("openclaw", REPO_ROOT)
    # Hermes n'expose aucun systeme de hooks connu : le declarer comme agent
    # complet ferait echouer `ts init` faute de bundle.
    assert "hermes" in MCP_ONLY_AGENTS
    assert "hermes" not in SUPPORTED_AGENTS
