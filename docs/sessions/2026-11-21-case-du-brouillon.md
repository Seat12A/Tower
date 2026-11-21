# 2026-11-21 — Case du brouillon, ids courts dans /case, index des verdicts

## Objectif
Retours de Jules sur Discord :
- `/case` exigeait l'UUID complet alors que le bot n'affiche que 8 caractères ;
- « Mark as published » dans le forum renvoyait `TWR-DOS-002` sur un case pourtant
  validé (alors que `/case publish` marchait).

## Réalisé
- `/case show|take|validate|reject|publish` accepte l'identifiant court (6 caractères ou
  plus) via `resolve_ref` ; les cartes groupes, claims, narratifs et faits n'affichent plus
  que l'identifiant court (`intercom/handlers/cases.py`, `factcheck.py`, `narratives.py`).
- Cause du `TWR-DOS-002` : un groupe d'affirmations peut porter plusieurs cases (un par
  brouillon) et `_case_of_group` prenait le **plus récent** du groupe. Le bouton du forum
  publiait donc un autre case (encore `received`). Même défaut pour la revue d'un brouillon
  depuis le forum (elle pouvait faire avancer le mauvais case) et pour `draft_id_of_case`.
- Correctif : colonne `cases.verdict_id` (migration `0016`, expand, remplissage des cases
  existants), écrite par `open_draft_for_content` ; `_case_of_verdict` et
  `draft_id_of_case` utilisent ce lien, avec repli sur le seul case non lié du groupe.
- Docs : `database.md`, `intercom.md`, `decisions.md`.
- Note pour la session de l'extension sur le projet d'identifiants courts (hors dépôt).
- **Index des verdicts** (demande de Jules : un post vérifié hors filtres n'affichait
  rien). `GET /v1/verdict-index/{platform}/{bucket}` : SHA-256 des posts d'une boîte de
  hachage (4 caractères hex) qui ont un verdict visible, même règle que la route du
  verdict ; `core/usecases/verdict_index.py`, route dans `gate/routes/factcheck.py`,
  code `TWR-FCK-050`, index sur expression (migration `0017`, `CONCURRENTLY`). Docs :
  `api.md`, `extension-api.md` (section 2 bis), `extension.md`, `database.md`,
  `errors.md` régénéré. Note pour la session de l'extension (hors dépôt).

## Décisions
- Index par boîte de hachage, calculé à la demande en SQL (reporté dans `decisions.md`).
- Lien direct case ↔ brouillon, remplissage borné à 5 minutes, dans le doute pas de lien
  (reporté dans `decisions.md`).

## Vérifications
- Conteneur : pgvector 0.6 trop ancien ; pgvector 0.8.0 compilé depuis les sources
  (autorisé par Jules).
- Nouveau test `test_draft_targets_its_own_case_not_the_groups_latest` : **échoue avec
  l'ancien code**, passe avec le correctif. Tests du repli et du remplissage de la
  migration ajoutés.
- 4 assertions de `test_drafts_usecases.py` étaient déjà fausses sur la base de départ
  (champ `discord_thread_id` absent des attentes) : corrigées.
- `alembic check` OK, migration aller-retour 0015 ↔ 0016 OK, ruff et mypy strict OK.
- Smoke test : `tower gate` démarre (doublures, `TOWER_LLM_FAKE=1`), `/health` → 200.
  `GET /v1/verdict-index/x/6f58` → 200 `{"hashes": []}`, boîte invalide → 422. Aucun
  appel réel à une API externe.
- Suite complète (après l'index des verdicts) : 2199 tests verts, 2 échecs **déjà présents sur la base de départ**
  (`test_source_standing_db.py` : `test_identity_refresh_errors`,
  `test_subject_sites_failure_does_not_stop_the_investigation`), non traités ici. Les
  tests intercom qui lisaient l'UUID complet dans les cartes ont été adaptés à l'id court.
- Index des verdicts : 3 tests (nominal, brouillon et verdict expiré exclus, `304`,
  boîte ou plateforme invalide) ; plan d'exécution mesuré sur 200 000 contenus factices :
  parcours de l'index, 0,06 ms ; migration 0016 ↔ 0017 aller-retour OK.
- RAM : pas de mesure, seuls un petit module de cas d'usage et une route sont chargés par
  gate (`orjson` et `hashlib` étaient déjà chargés).

## Points ouverts / suite
- Extension : utiliser l'index des verdicts (note transmise à Jules) ; tant que ce n'est
  pas fait et qu'aucune version de filtres n'est active en prod, rien ne s'affiche.
- Vérifier en prod si un autre case du groupe « 1+1=4 » a changé de statut à tort
  (effet de l'ancien défaut sur la revue depuis le forum).
- Chantier demandé par Jules, en attente de feu vert : supprimer les UUID (identifiant
  court unique de 8 caractères en base, API, événements). Plan expand/contract proposé ;
  impact sur l'extension et le site à confirmer.
- Lot proposé : `/incidents show` par code d'erreur, message clair si introuvable,
  `TWR-BDG-026` qui distingue « jeton absent » et « API X indisponible ». Côté Railway :
  `TOWER_X_BEARER_TOKEN` à ajouter sur le service gate.
