# Unité A : instrumentation des injections + classification des ratés

Design validé le 2026-07-25. Auteur : Claude (délégation pleine de Louis). Fait suite à la Phase 1 (ledger) shippée le 2026-07-24.

## Problème

Le ledger enregistre les `miss` (corrections de Louis) mais il est **à moitié aveugle** :
- il ne journalise aucune action du système (les injections que le hook fait déjà, leur coût tokens) ;
- le champ qui classe la panne (`was_visible`) n'est jamais rempli.

Sans ça, on ne sait pas quelle panne domine, et on construirait `rules` ou `retrieval` à l'aveugle. A rend le ledger réellement mesurant, et sa sortie (la distribution des ratés) **décide par la donnée** quelle unité construire ensuite.

## But

Pour chaque `miss`, classer automatiquement en **4 catégories** :
1. **`unrecorded`** — le savoir n'était nulle part → problème de *capture* (pas récup, pas activation).
2. **`invisible`** — stocké mais pas remonté → problème de *récupération* (→ `retrieval`).
3. **`ignored`** — remonté mais pas appliqué → problème d'*activation* (→ `rules`).
4. **`uncertain`** — confiance insuffisante → on ne devine pas (soupape d'honnêteté, esprit sceptique adversarial).

## Ajustement de design clé (exploration du code réel, 2026-07-25)

`observation_search` (`memory/observations.py:309`) ne retourne **pas** de score de pertinence exploitable : ordre par `rank` FTS puis fusion RRF, et `_rrf_score` n'existe qu'en mode vectoriel (`memory/search.py:52`), absent en FTS pur. **On n'utilise donc pas de score absolu.** À la place, signal de confiance = **recouvrement de tokens** entre les tokens de contenu de la correction et l'obs top (titre + `excerpt`), plus le signal fiable « FTS vide ». Calculable dans tous les modes, inspectable, sans boîte noire.

## Architecture

Deux moitiés, greffées sur `hooks/memory-userprompt.sh` (aucune migration DB : tout passe dans `meta_json`).

### Moitié 1 — Instrumenter les injections

Le bloc d'injection synchrone (`memory-userprompt.sh` ~L39-97) sélectionne déjà `top3` obs et les affiche. On l'étend d'un appel à une nouvelle fonction :

`ledger.record_injection(session_id, project_root, obs_ids, injected_text) -> dict`
- écrit un event `injection` avec `meta={"obs_ids":[...]}` et `cost_tokens` = estimation (`len(injected_text)//4`, approximation tokens).
- une seule ligne INSERT sur un bloc qui fait déjà une recherche FTS : coût négligeable.
- journalise **exactement** ce qui a été montré (les ids en main), pas une ré-estimation.

### Moitié 2 — Classifier le raté

Nouvelle fonction pure-ish :
`ledger.classify_miss(correction_text, injected_obs_ids, project_root) -> dict`
retournant `{"miss_class": str, "expected_obs": int | None, "overlap": float, "content_tokens": int}`.

Algorithme :
1. Extraire les tokens de contenu de `correction_text` (retirer la phrase-trigger et les stopwords ; réutiliser la logique de tokenisation déjà présente dans le hook : `[A-Za-zÀ-ÿ0-9_]{3,}` moins stopwords).
2. Si `< 2` tokens de contenu → **`uncertain`** (requête trop maigre pour être fiable).
3. Sinon `observation_search(project_root, query, limit=5)` :
   - résultats vides → **`unrecorded`**.
   - sinon `expected = top result` ; `overlap` = fraction des tokens de contenu présents dans `title + excerpt` de `expected`.
     - `overlap >= OVERLAP_HIGH` (défaut 0.5) :
       - `expected.id ∈ injected_obs_ids` → **`ignored`**
       - sinon → **`invisible`**
     - `overlap < OVERLAP_HIGH` → **`uncertain`** (résultat trouvé mais match trop faible pour trancher).

### Câblage

`record_from_userprompt` (déjà appelé par `ledger_hook` dans le bloc background) est étendu :
- quand un `miss` est détecté, il récupère l'ensemble injecté récent = union des `meta.obs_ids` des events `injection` de ce `session_id` (via `ledger_query`), appelle `classify_miss`, et écrit le `miss` enrichi :
  - `meta = {"phrase","text","miss_class","expected_obs","overlap"}`
  - colonne existante `was_visible` remplie en cohérence : `1` si `ignored`, `0` si `invisible`, `NULL` si `unrecorded`/`uncertain`.

## Modèle de données

Aucune migration. `injection` et `miss` enrichissent `meta_json`. `was_visible` (colonne existante) porte le booléen dérivé.

## Où ça tourne

Tout dans le bloc **background** du hook (déjà en `&`, non-bloquant). La classification fait une recherche FTS, plus lourde — l'async est le bon endroit, zéro impact sur la latence de prompt. La moitié 1 (record_injection) est dans le bloc synchrone (elle a les ids en main), une ligne INSERT.

## Évaluation

- **Le livrable EST la distribution** des 4 buckets. Après accumulation, on lit p.ex. « 60% invisible / 30% ignored / 10% unrecorded » → ça décide `retrieval` vs `rules` en premier.
- **`uncertain` = soupape d'honnêteté.** S'il est énorme, les seuils sont mal calés (signal de retune), pas une fausse certitude.
- **Spot-check** : étiqueter à la main un petit échantillon de ratés réels, vérifier l'accord du classifieur.

## Non-objectifs (limites nettes)

- A **ne répare rien** : instrumente et classe. Ni règles ni meilleure récup.
- A ne remplit **que** `was_visible` / `miss_class`. Les autres champs d'outcome (`acted_on`, `prevented_error`, `block_justified`) viendront avec `rules`.
- A **ne touche pas** la logique d'injection ; il journalise ce qu'elle montre déjà.
- Pas de boucle `reflection`.

## Sortie stratégique

La réponse chiffrée à « les deux, ça dépend » — et donc l'ordre de construction de `rules` vs `retrieval`, piloté par la donnée.

## Point à verrouiller au plan

Les clés exactes retournées par `observation_search` (`id`, `title`, `excerpt` confirmés à `observations.py:337-340`) et le format de l'`excerpt` (marqueurs `»«` autour des termes FTS matchés) — pour calculer `overlap` sur du réel.
