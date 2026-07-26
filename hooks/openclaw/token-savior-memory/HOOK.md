---
name: token-savior-memory
description: "Moteur memoire Token Savior : recall a l'amorcage, capture sur prompt et compaction"
homepage: https://github.com/Mibayy/token-savior
metadata:
  {
    "openclaw":
      {
        "emoji": "🧠",
        "events":
          [
            "agent:bootstrap",
            "message:received",
            "session:compact:before",
            "command:new",
            "command:reset",
          ],
        "requires": { "config": ["workspace.dir"] },
        "install":
          [{ "id": "token-savior", "kind": "manual", "label": "Token Savior Recall" }],
      },
  }
---

# Moteur memoire Token Savior pour OpenClaw

Pont entre les scripts de hooks Token Savior (ecrits pour Claude Code) et le
modele d'evenements d'OpenClaw. Les scripts eux-memes ne sont pas dupliques :
le handler les execute et leur passe sur l'entree standard une charge utile de
forme Claude (`hook_event_name`, ...), ce qui evite d'entretenir deux moteurs.

## Correspondance des evenements

| Claude Code | OpenClaw | Script |
|---|---|---|
| `SessionStart` | `agent:bootstrap` | `memory-session-start.sh` |
| `UserPromptSubmit` | `message:received` | `memory-userprompt.sh` |
| `PreCompact` | `session:compact:before` | `memory-precompact.sh` |
| `SessionEnd` | `command:new`, `command:reset` | `memory-session-stop.sh end` |

`command:new` et `command:reset` sont ce qu'utilise le hook livre
`session-memory` pour detecter une fin de session : OpenClaw n'expose pas
d'evenement de fermeture par session.

## Ce qui ne peut pas etre porte

OpenClaw 2026.4.14 **n'expose aucun evenement d'outil**. Les predicats
exportes par son module de hooks sont `isAgentBootstrapEvent`,
`isGatewayStartupEvent`, `isMessageReceivedEvent`, `isMessagePreprocessedEvent`,
`isMessageSentEvent`, `isMessageTranscribedEvent` et `isSessionPatchEvent`.
Il n'y a pas d'equivalent a `PreToolUse` / `PostToolUse`, donc ni le sandbox
`tool_capture` ni le reecriveur de commandes Bash ne fonctionnent ici.

## Mecanisme d'injection

La ou Claude Code injecte le contexte par la sortie standard du hook, OpenClaw
injecte en **mutant `context.bootstrapFiles`** pendant `agent:bootstrap` --
c'est ce que fait le hook livre `bootstrap-extra-files`. Une entree a la forme
`{ name, path, content, missing }`. Le consommateur deduplique par nom de
fichier, d'ou le nom distinct `TOKEN-SAVIOR-MEMORY.md` : reutiliser `AGENTS.md`
ecraserait le vrai fichier de l'agent.
