# Token Savior — configuration recommandée, tous agents

Ce fichier suit la convention `AGENTS.md`, lue par une trentaine d'agents
(OpenAI Codex, Cursor, GitHub Copilot, Gemini CLI, Aider, Zed, Windsurf,
Jules, Factory, Devin…). Claude Code lit `CLAUDE.md`, dont le contenu est
équivalent. Déposez ce fichier à la racine de votre projet pour reproduire
le résultat de [tsbench](https://github.com/Mibayy/tsbench) : 97,9 % du score,
−80 % de tokens actifs.

## Ce qui est portable, et ce qui ne l'est pas

| Couche | Portabilité | Détail |
|---|---|---|
| Serveur MCP (69 outils) | **Universelle** | MCP est un protocole ouvert. Aucune adaptation. |
| `AGENTS.md` | **~30 agents** | Ce fichier. |
| Skills (`SKILL.md`) | **16 à 32 outils** | Standard ouvert [agentskills.io](https://agentskills.io). |
| Hooks | **Aucun standard** | Chaque agent a son format. C'est la seule couche à écrire. |

`ts init --agent {claude,cursor,gemini,codex}` écrit la couche hooks pour
vous, au bon endroit et au bon format.

### Pièges de portage des hooks

Deux erreurs ne provoquent **aucun message d'erreur** et rendent simplement
le hook inerte. Les deux sont couvertes par `tests/test_codex_hook_bundles.py`.

1. **Événements inexistants.** Les agents n'exposent pas les mêmes événements
   de cycle de vie. Codex 0.145.0 expose `SessionStart`, `SessionEnd`,
   `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `PreCompact`,
   `Notification`, `SubagentStop` — et **pas** `Stop`, `StopFailure` ni
   `ConfigChange`, qui existent côté Claude Code. Un hook déclaré sur un
   événement inconnu ne se déclenche jamais, en silence.
2. **Unité des timeouts.** Claude Code compte en **millisecondes**, Codex en
   **secondes**. Recopier `timeout: 120000` d'un fichier à l'autre ne plante
   pas : ça demande 33 heures d'attente.

Pour vérifier ce qu'un binaire expose réellement plutôt que de faire
confiance à sa documentation :

```bash
strings -n 6 "$(command -v codex)" | grep -xE "(SessionStart|SessionEnd|PreToolUse|PostToolUse|UserPromptSubmit|PreCompact|Notification|SubagentStop|Stop)"
```

## Réglages par défaut

```bash
TS_PROFILE=tiny_plus            # 15 outils, manifeste ~2,5 KT
TS_CAPTURE_DISABLED=1           # pas de sandboxing des lectures
```

`tiny_plus` est le profil Pareto-optimal : exactement les 15 outils que les
agents utilisent après le premier tour (localiser, lire, éditer, auditer,
graphe, git, config), le reste restant accessible via `ts_search`.
Manifeste : tiny_plus ~2,5 KT contre lean ~7 KT et full ~10 KT.

## Routage des outils

| But | Outil | Remplace |
|---|---|---|
| Localiser un symbole | `find_symbol(name)` | `grep -rn` |
| Lire une fonction / classe | `get_function_source` / `get_class_source` | `cat` + défilement |
| Contexte complet en un appel | `get_full_context(name)` | toute la chaîne |
| Chercher dans le projet | `search_codebase(pattern)` | `grep` / `rg` |
| Découvrir un autre outil | `ts_search(query)` | lire la doc |
| Éditer du code | `replace_symbol_source` / `insert_near_symbol` | édition texte |
| Ajouter un champ de modèle | `add_field_to_model` | édition manuelle |
| Déplacer un symbole | `move_symbol(name, target_file)` | copier-coller |
| Cycles d'import | `find_import_cycles` | raisonnement manuel |
| Doublons | `find_semantic_duplicates(max_groups=30)` | revue manuelle |
| Ruptures d'API | `detect_breaking_changes(ref="v1")` | lecture de `git diff` |
| Code mort | `find_dead_code` | chasse manuelle |
| Audit de config | `analyze_config(checks=["orphans"])` | vérifs manuelles |
| État git structuré | `get_git_status` | parsing de `git status` |

Les outils natifs de lecture et d'écriture restent légitimes pour `.env`,
`.yml`, `.json` et `.md`. Sur du code source (`.py`, `.ts`, `.tsx`, `.js`,
`.jsx`), ils font perdre des points au benchmark.

## Règles qui font la différence

- **Une seule requête de contexte.** Si vous vous apprêtez à enchaîner
  `find_symbol` → `get_function_source` → `get_dependents`, appelez
  `get_full_context(name, depth=2)` : un aller-retour au lieu de trois.
- **Avant toute édition**, `get_edit_context(name)` rend source, appelants,
  dépendances, voisins et tests en un appel.
- **Tâches de génération de code** : la réponse doit contenir les `import` en
  tête du bloc et le chemin de fichier cible explicite.
- **Renommages** : `replace_symbol_source` sur la définition, puis
  `search_codebase` pour les appelants du même module. Ne jamais toucher aux
  symboles homonymes d'autres modules.
- **Citations** : tout symbole mentionné porte un chemin `fichier::symbole`.

## Reproduire le benchmark

```bash
git clone https://github.com/Mibayy/tsbench && cd tsbench
python3 generate.py --seed 42
git tag v1
python3 breaking_changes.py
git tag v2
TS_PROFILE=tiny_plus TS_CAPTURE_DISABLED=1 python3 bench.py --tasks all --run B
```
