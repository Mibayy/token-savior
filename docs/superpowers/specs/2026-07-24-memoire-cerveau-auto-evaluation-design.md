# Mémoire cerveau : rappel actif + auto-évaluation autonome

Design validé le 2026-07-24. Auteur : Louis + Claude.

## Problème

Malgré un moteur de mémoire persistant riche (Token Savior : `decay`, `dedup`,
`distillation`, `links`, `consistency`, `embeddings`, `reasoning`, `roi`,
`linucb_injector`) et une injection mémoire à chaque événement du cycle de vie,
en pratique le rappel échoue : Louis doit régulièrement rappeler à Claude une
chose qu'il « sait » déjà (ex : aller regarder les logs). Deux pannes coexistent :

1. **Info invisible** : la connaissance existe mais n'est pas remontée dans le
   contexte au bon moment (panne de récupération).
2. **Présent mais ignoré** : la connaissance est bien injectée mais Claude ne
   fait pas le lien « situation actuelle = déclencheur » et n'agit pas (panne
   d'activation).

Preuve vivante de la panne 1, observée pendant le brainstorm : le bloc
« Relevant memory » injecté à chaque message ressortait des conventions
hors-sujet (gold underline, bulles de chat) sur des questions sans rapport.

Le problème n'est **pas** un manque de stockage. Toute la machinerie existe.
Ce qui manque : l'**activation** fiable et une **boucle qui se corrige seule**.

## But et critère de succès

Le nombre de fois où Louis doit rappeler à Claude une chose déjà connue tend
vers zéro. Mesuré, pas supposé.

Contrainte cardinale (leçon `bench_compactors_real`) : on mesure les **erreurs
réellement évitées**, jamais l'activité (« nombre de règles déclenchées » est
une métrique de vanité). Le système doit pouvoir conclure qu'un de ses propres
mécanismes est contre-productif et le désactiver.

## Approche retenue

Hybride en tiers (approche C), construit sur les hooks Token Savior existants,
avec une boucle de réflexion autonome **à autonomie bornée**. Louis est *sur* la
boucle (inspection + veto), pas *dans* la boucle.

## Architecture

Rien de nouveau côté infra. Chaque unité se greffe sur un hook déjà en place.

```
Événement          Hook existant              Rôle ajouté
──────────────────────────────────────────────────────────────────────
UserPromptSubmit   memory-userprompt.sh   →   retrieval (tier souple)
PreToolUse         memory-pretooluse.sh   →   rules (tier dur, enforcement)
PostToolUse        memory-posttooluse.sh  →   ledger (registre actions + capture)
cron nocturne      (nouveau)              →   reflection (auto-évaluation)
```

Frontière des deux mémoires :
- **Moteur TS** (SQLite, embeddings, obs scorées) = source de vérité, sait *quoi*
  remonter.
- **Hooks** = organes d'action, décident *quand* et *comment* (souple = injecté,
  dur = forcé/bloquant).
- **Mémoire native Claude Code** (`MEMORY.md`) = catalogue lisible/éditable des
  règles trigger→action.

Quatre unités isolées, chacune un seul rôle, testable seule :
`rules`, `retrieval`, `ledger`, `reflection`.

## Unité 1 — rules (tier dur, enforcement)

Catalogue de règles trigger→action, lisible et éditable par Louis, appliqué par
`memory-pretooluse.sh`.

```yaml
règle:
  id: preflight-avant-push
  trigger:
    type: tool_pattern        # ou file_pattern (globs) / context_keyword
    match: "git push"
  action:
    type: require_precondition # ou "remind" (non bloquant)
    precondition: "preflight.sh a tourné cette session"
  severity: hard              # hard = bloque | soft = rappelle
  source_obs: ts://obs/…      # la mémoire d'origine
```

Mécanique : le hook reçoit l'appel d'outil imminent, le matche contre le
catalogue (mots-clés/patterns, pas d'appel LLM, zéro token tant que rien ne
matche).
- Règle *soft* → injecte un rappel fort, l'outil passe.
- Règle *hard* dont la précondition n'est pas satisfaite → refuse l'outil, avec
  un message disant exactement quoi faire d'abord.

Détection de précondition : `PostToolUse` tient un registre « actions récentes de
la session » (fait partie du ledger). La règle hard vérifie que l'action requise
y figure.

Cran de sécurité anti-paralysie :
- Règles *hard* peu nombreuses, vérifiées à la main. Amorçage sur les guardrails
  déjà brûlés : preflight avant push, jamais de DELETE en masse en prod,
  `replace_symbol` mange les décorateurs.
- Un blocage n'est jamais un mur : précondition remplie → ça passe ; Louis peut
  toujours forcer.
- Kill-switch instantané (variable d'env, style `TS_*`).
- Un faux positif écrit une ligne au ledger → signal négatif qui desserre la règle.

## Unité 2 — retrieval (tier souple, récup contextuelle)

Améliore la pertinence de l'injection existante, sans nouvel ML.
- Construit un **vecteur de situation** : dernier message + fichiers touchés dans
  la session + mots-clés de la tâche + dernières erreurs d'outil.
- Interroge `embeddings.py` sur la proximité sémantique à ce vecteur (pas juste le
  recouvrement de mots-clés).
- Re-rank via `linucb_injector.py` / `roi.py` (une obs qui a déjà payé remonte).
- **Seuil de pertinence + silence** : injecte seulement au-dessus d'un seuil. Si
  rien ne passe la barre, n'injecte **rien**. Le silence vaut mieux que le bruit.
  Plafond : 3 obs.

Le « gold underline » sur une question de coûts tomberait sous le seuil et serait
écarté.

## Unité 3 — ledger (observabilité + contre-productivité)

Absorbe le miss-log. **Tout** ce que le système fait écrit une ligne, pas
seulement les échecs.

```yaml
event:
  ts: …
  type: injection | rappel_soft | blocage_hard | silence | raté | faux_positif
  quoi: la règle ou l'obs impliquée
  coût: tokens injectés + latence ajoutée
  résultat:                 # rempli après, parfois async
    agi_dessus: bool
    a_évité_une_vraie_erreur: bool
    ignoré: bool
    blocage_justifié: bool
    était_visible: bool     # ← classe la panne automatiquement
```

Valeur nette par règle et par mécanisme :

```
bénéfice = vraies erreurs évitées + rappels ayant changé le comportement
coût     = tokens d'injection + faux positifs + frottement (blocages injustifiés)
net      = bénéfice − coût
```

Si `net < 0` pour une règle ou un mécanisme → contre-productif → signalé pour
suppression. Le système doit pouvoir se recommander son propre rollback.

Classement automatique de la panne via `était_visible` :
- visible mais raté → « présent mais ignoré » → candidat promotion en règle *hard*.
- absent → « invisible » → ajuster seuil/vecteur du tier souple.

Le ledger **mesure donc lui-même quelle panne domine**, au lieu de le supposer.

Capture des ratés (heuristique quasi gratuite, sur `UserPromptSubmit`) : phrases
de correction (« je t'ai déjà dit », « tu devais », « encore », « je te
rappelle »).

Honnêteté sur l'attribution du `résultat` : automatique quand possible (blocage
forcé = faux positif vs plié = probablement justifié ; rappel suivi = l'appel
d'outil suivant l'a respecté ; injection utile = obs référencée dans la session).
Le reste passe par une **passe d'échantillon** de la boucle de réflexion. On
mesure honnêtement sur échantillon, pas parfaitement.

## Unité 4 — reflection (auto-évaluation autonome, bornée)

Un agent qui tourne seul (cron nocturne, contexte frais à chaque fois, pattern
`orchestrator`). Il :
1. lit son propre ledger,
2. juge sa propre performance (règles qui paient, bruit, ratés récurrents),
3. écrit un **journal métacognitif** (pour lui-même, pas pour Louis) : ce qu'il a
   observé de son propre comportement, ce qu'il change et pourquoi,
4. agit dans son enveloppe d'autonomie,
5. ce journal redevient de la mémoire, qui nourrit la réflexion suivante.

C'est de la **métacognition planifiée** : un processus qui observe et corrige ses
propres processus. Pas une conscience. La valeur (auto-correction autonome,
constante, infatigable) ne dépend pas de ce label.

### Autonomie bornée (curseur validé)

- **Faible enjeu → applique seul** : desserrer/couper une règle *soft* bruyante,
  couper un flux d'injection jamais suivi, ajuster un seuil de récupération.
- **Fort enjeu → propose + seuil de preuves + veto** : créer/supprimer une règle
  *hard* (qui peut bloquer les outils de Claude). Appliqué seulement après un
  seuil de preuves au ledger, toujours réversible, toujours tracé là où Louis
  peut voir.

Louis sur la boucle : inspecte et oppose un veto, sans avoir à conduire.

### Deux risques traités (non optionnels)

1. **Dérive** : un système qui réécrit ses règles d'enforcement peut se dégrader
   sans témoin. Mitigation = l'autonomie bornée ci-dessus + réversibilité +
   traçabilité intégrales.
2. **Auto-illusion** : un système qui se note lui-même dérive vers les métriques
   de vanité. Mitigation = **sceptique adversarial** : avant toute conclusion
   positive, une passe qui tente de *prouver que le système n'aide pas*. Interdit
   de conclure « je vais bien » sans avoir cherché la preuve du contraire.
   (Cohérent avec les patterns de vérification adversariale et
   `bench_compactors_real`.)

## Évaluation

- **Taux de ratés** : corrections détectées par session, doit tendre vers zéro
  (critère de succès de Louis).
- **Taux de faux positifs** : blocages hard injustifiés, doit rester ~0.
- **Précision du rappel** : fraction des mémoires injectées réellement
  pertinentes.
- **Net global** : le système est-il positif en tokens ET en erreurs évitées ?
- **Backtest** : rejouer des ratés connus (incident décorateurs, brûlures
  preflight) sur les ~493 tool captures existantes et vérifier que la règle
  *aurait* tiré, avant tout déploiement.

## Non-objectifs (YAGNI)

- Pas de refonte du moteur mémoire TS : on réutilise `embeddings`,
  `linucb_injector`, `roi`, `links`, `decay`.
- Pas de « cerveau » biologiquement fidèle : on vise les propriétés utiles
  (activation fiable, oubli, auto-correction), pas la ressemblance.
- Les deux autres désirs initiaux (réflexion autonome généralisée « selon ce que
  Louis dit », suggérer les erreurs de Louis) sont **hors périmètre de ce spec**.
  Ils viendront après, si le rappel actif est prouvé net positif.

## Phasage suggéré

1. `ledger` d'abord (mesure avant tout, cohérent avec « vérifier au lieu
   d'avancer ») + capture des ratés.
2. `rules` tier dur sur 3-5 guardrails connus + backtest.
3. `retrieval` seuil + silence + vecteur de situation.
4. `reflection` bornée + sceptique adversarial, une fois qu'il y a de la donnée
   au ledger à réfléchir.
