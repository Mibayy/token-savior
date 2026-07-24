# Unité rules : enforcement dur des règles trigger→action

Design 2026-07-25. Auteur : Claude (délégation pleine). Suite de l'unité A. Curseur validé par Louis : **bloquer l'outil**.

## Problème

La panne `ignored` (présent mais pas appliqué) ne se répare pas par du rappel : la connaissance est déjà là, je ne l'applique pas. Il faut la rendre **déterministe** — que le harness force le comportement, indépendamment de mon attention. Cible : les guardrails déjà brûlés en vrai (preflight avant push, jamais de DELETE en masse, force-push protégé).

## Contrat vérifié

Un hook PreToolUse bloque un outil en émettant sur stdout :
```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}
```
(`allow` laisse passer.) Payload d'entrée : `{"tool_name","tool_input":{...},"session_id"}`. `tool_input.command` pour Bash, `tool_input.file_path` pour édits. Vu dans `approval-hook.js` et `bash_rewriter_hook.py`.

## Architecture

**Hook d'enforcement dédié** (`hooks/rules-enforce.sh` + entrypoint python), séparé de `memory-pretooluse.sh` (qui n'émet que du texte d'injection) pour ne pas mélanger texte de contexte et décision JSON. Enregistré comme hook PreToolUse supplémentaire.

Nouveau module `token_savior.memory.rules` :
- `load_rules(path=None) -> list[dict]` : lit le catalogue JSON (Louis-éditable).
- `match(tool_name, tool_input, rules) -> list[dict]` : règles dont le trigger matche.
- `evaluate(tool_name, tool_input, session_id, *, rules=None) -> dict` : retourne `{"decision": "allow"|"deny", "reason": str|None, "rule_id": str|None, "severity": str|None}`. Écrit l'event ledger correspondant (`hard_block` si deny/justifié, `false_positive` réservé au feedback ultérieur).

### Catalogue (JSON, éditable)

`hooks/ledger-rules.json` :
```json
[
  {
    "id": "no-force-push-protected",
    "trigger": {"tool": "Bash", "command_regex": "git\\s+push\\b.*(--force|-f)\\b.*\\b(main|master)\\b"},
    "action": {"type": "deny", "message": "Force-push sur une branche protégée bloqué. Retire --force ou vise une branche de feature."},
    "severity": "hard"
  },
  {
    "id": "no-blanket-delete",
    "trigger": {"tool": "Bash", "command_regex": "(?i)delete\\s+from\\s+\\w+\\s*(;|$)"},
    "action": {"type": "deny", "message": "DELETE sans WHERE bloqué (guardrail données prod). Cible des lignes précises."},
    "severity": "hard"
  },
  {
    "id": "preflight-before-push",
    "trigger": {"tool": "Bash", "command_regex": "git\\s+push\\b"},
    "action": {"type": "require_precondition", "precondition": "preflight",
               "message": "Lance preflight.sh avant de push (brûlé 2×)."},
    "severity": "hard"
  }
]
```

Triggers supportés v1 : `tool` (exact) + `command_regex` (Bash) ou `file_glob` (édits). Actions : `deny` (bloque toujours), `require_precondition` (bloque sauf si la précondition a été satisfaite cette session), `warn` (soft, laisse passer + rappelle).

### Préconditions (mécanisme léger, réutilise le ledger)

Pas de nouvelle table. `ledger_put(event_type="precondition", meta={"name": "preflight"}, session_id=...)` marque qu'une précondition a été satisfaite. `precondition_met(session_id, name)` interroge le ledger. `event_type="precondition"` est inerte pour `net_value` (ni bénéfice ni friction, coût 0).

- **PostToolUse** (nouvel entrypoint, greffé sur `memory-posttooluse.sh`) : quand une commande Bash matchant une précondition connue (ex. `preflight`) se termine en succès (exit 0, lu du tool_result), écrit l'event `precondition`.
- **PreToolUse rules** : `require_precondition` → si `precondition_met` est faux → deny avec le message.

### Cran de sécurité (non négociable)

- Kill-switch `TS_RULES_DISABLE=1` court-circuite l'enforcement (comme `TS_MEMORY_DISABLE`).
- Un deny n'est jamais un mur : le message dit exactement quoi faire ; Louis peut toujours contourner (retirer --force, lancer preflight, ou désactiver).
- Règles `hard` peu nombreuses et vérifiées à la main (les 3 ci-dessus au départ).
- Toute erreur du hook → `allow` par défaut (fail-open) : un bug d'enforcement ne doit JAMAIS bloquer le travail de Louis. C'est l'inverse d'un fail-closed : ici la sécurité de session prime sur la sécurité de la règle.

## Évaluation

- Chaque deny/warn écrit un event ledger → `net_value` mesurera plus tard si une règle est nette positive (vraies prises vs faux positifs).
- Un `false_positive` (Louis contourne un deny légitimement) sera capturable en v2 (feedback), desserrant la règle.
- Backtest (unité suivante) : rejouer les 493 tool captures contre le catalogue et vérifier que les règles auraient tiré au bon moment, sans faux positifs.

## Non-objectifs

- Pas de génération auto de règles (elles sont écrites à la main, vérifiées).
- Pas d'attribution de bénéfice fine (vient avec reflection).
- v1 : triggers Bash/édit simples. Pas de triggers sémantiques.

## Phasage

1. Module `rules` : `load_rules` + `match` + `evaluate` (deny + warn), TDD. **Livrable testable sans hook.**
2. Préconditions : `precondition_met` + recorder PostToolUse.
3. Hook `rules-enforce.sh` : émet le deny JSON, log l'event, fail-open. Smoke live.
4. Catalogue initial `ledger-rules.json` (les 3 règles).
