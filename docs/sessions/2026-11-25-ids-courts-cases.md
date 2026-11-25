# 2026-11-25 — Identifiants courts des cases partout

## Objectif

Demande de Jules : en interne comme en public, on n'utilise que l'id court d'un case. Le
lien d'un case doit tenir dans un post X, ce qu'un UUID de 36 caractères empêche. Avant ce
lot, seul le bot acceptait l'id court ; l'API publique répondait `422` à
`/v1/cases/fbaa8363/details`.

## Réalisé

- Colonne `cases.short_id` (migration `0019`, expand) : les 8 premiers caractères
  hexadécimaux du `public_id`, un de plus par collision. Elle est unique, avec un `CHECK`
  hexadécimal de 8 à 32 caractères. Le déclencheur `cases_short_id` la pose à l'insertion,
  y compris pour le code déjà déployé, et ignore toute modification ultérieure. Les cases
  existants sont remplis dans l'ordre de création.
- `core/domain/ids.py` : `parse_ref` (id court ou UUID complet) et `SHORT_ID_LEN`.
- `core/usecases/cases.py` : `case_ref` (clause `WHERE`, correspondance **exacte** sur
  `short_id`, ou sur l'UUID). `get_case`, `link_discord_thread`, `transition`,
  `mark_published`, `history` et `get_case_details` acceptent l'un ou l'autre.
- Routes `/v1/cases/{case_id}…` (gate) : paramètre `CaseId` (8 à 36 caractères). Les
  réponses (`CaseOut`, `CasePublicOut`, `CaseSummaryOut`, `CaseDetailsOut`) ajoutent
  `short_id` ; `public_id` est gardé pendant la transition.
- `/v1/ids/case/{ref}` : un `short_id` exact l'emporte sur la recherche par préfixe.
- Événements : `short_id` dans l'état du case (`case.opened`, `case.transitioned`) ;
  `case_short_id` dans `draft.ready`, `draft.reviewed`, `verdict.published`. Bloc `case`
  du brouillon : `short_id`.
- Bot : `case_short` affiche le `short_id` servi par gate (cartes `/case`, titre et pied
  des posts du forum, notifications), avec repli sur les 8 premiers caractères.
- Docs : `api.md`, `database.md`, `events.md`, `intercom.md`, `decisions.md`.
- Test : `tests/test_short_ids.py` (`test_parse_ref`,
  `test_case_short_id_is_stable_and_used_by_every_route`).

## Décisions

Reportées dans `docs/decisions.md` (section du 2026-11-25, identifiants courts).

- Colonne figée plutôt qu'un simple préfixe : un lien publié ne doit jamais devenir
  ambigu.
- Champ `short_id` (pas `id`), UUID encore accepté et renvoyé (expand/contract).
- Cases seulement : les autres objets restent internes.

## Vérifications

- Conteneur : PostgreSQL 16 démarré sur le port 5433, pgvector 0.8.1 compilé depuis les
  sources (il a fallu installer `postgresql-server-dev-16`).
- `ruff format --check`, `ruff check` et `mypy` strict : OK.
- `alembic check` : aucune différence. Migration `0019` aller-retour
  (`downgrade -1`, `upgrade head`) : OK, déclencheur présent.
- Déclencheur testé à part sur une table minimale : remplissage des lignes existantes,
  collisions (`fbaa83632`, puis `fbaa83632f`), modification de `short_id` ignorée.
- Suite complète : 2 249 réussis, 9 échecs. Six venaient de tests qui comparaient des
  charges utiles exactes (nouveaux champs `short_id` et `case_short_id`) ; ils sont
  adaptés et repassent (325 tests ciblés réussis, dont ceux du bot). Les 3 autres
  (`test_source_standing.py::test_unrated_source_without_first_party_cannot_be_primary`,
  `test_source_standing_db.py::test_identity_refresh_errors` et
  `::test_subject_sites_failure_does_not_stop_the_investigation`) échouaient déjà avant
  ce lot (voir le résumé `2026-11-25-api-site-cases.md`).
- Smoke test du vrai processus `tower gate` sur une base locale :
  `GET /v1/cases/fbaa8363` → `200` avec `short_id`, `/details` → `200`,
  `GET /v1/cases/fbaa83` → `422` `TWR-GEN-001`.
- Pas de mesure de RAM : le lot n'ajoute aucun import ni module chargé au démarrage.
- Rien n'a été appelé en production ; la migration n'est pas encore déployée.

## Points ouverts / suite

- **Site et extension** (hors dépôt) : construire les liens et les appels avec
  `short_id`.
- **Contract** : retirer `public_id` des réponses des cases quand plus aucun client ne le
  lit.
- Collision simultanée de deux insertions qui partagent leurs 8 caractères : la seconde
  échoue sur la contrainte d'unicité et doit être relancée (un job l'est
  automatiquement). Probabilité de l'ordre de 1 sur 4 milliards par paire simultanée.

## Suite : niveau des demandes de fact-check

- Demande de Jules : les demandes depuis le bot passaient en N4 (modèles les plus forts à
  chaque fois). Nouveau réglage `TOWER_FC_REQUEST_LEVEL` (`N3` par défaut, `N4`
  possible), lu par le job `factcheck.request` (`services/factcheck/jobs.py`).
- `claims.promote_for_request` : une demande N3 sur un contenu déjà extrait porte ses
  groupes en N3 et ouvre une enquête standard si le groupe n'a ni verdict actif ni
  enquête N3/N4 en cours.
- Docs : `configuration.md`, `.env.example`, `api.md`, `intercom.md`, `fact-check.md`,
  `decisions.md`.
- Vérifications : ruff, mypy strict OK ; tests du service factcheck, du bot, de gate et
  des niveaux et récits : 171 réussis. Tests adaptés (demande en N3) et deux tests
  courts ajoutés (niveau configurable, demande N3 sur contenu déjà extrait).
- À savoir : en N3, une affirmation non vérifiable ne produit pas de brouillon.

## Suite : commande `/factchecks`

- Demande de Jules : voir les fact-checks en préparation et leur statut.
- `core/usecases/factcheck_progress.py` (lecture seule) et
  `GET /v1/factcheck/in-progress` (`cases:read`) : demandes de l'équipe pas encore
  extraites (jobs `factcheck.request` et `factcheck.extract`, flux automatiques exclus)
  et enquêtes non assemblées, avec les échecs des dernières 24 h.
- Bot : `/factchecks` (carte avec étape, niveau, profondeur, coût, affirmation, lien,
  demandeur, ancienneté). Permission `cases:read`, déjà donnée aux rôles de relecture :
  rien à configurer, la commande est synchronisée au démarrage du bot.
- Docs : `api.md`, `intercom.md`.
- Vérifications : ruff, mypy strict OK ; un test de route et un test de carte ajoutés ;
  tests de gate (fact-check), du bot et de l'aide : 111 réussis.
- Redémarrage : les fact-checks en cours ne sont pas perdus. Les jobs sont en base ; un
  job interrompu reprend après expiration de son bail (`TOWER_JOB_LEASE_SECONDS`,
  5 min) et les étapes déjà payées d'une enquête ne sont pas refaites.

## Suite : enquête bloquée et raisons d'attente dans `/factchecks`

- Constat de Jules : une enquête affichée « Rédaction de la note » depuis un jour, et des
  demandes manuelles absentes de la liste. Les données de production n'ont pas pu être
  lues depuis cette session : le diagnostic vient du code.
- Cause possible d'une enquête figée : une étape qui se termine sans marquer l'enquête
  en échec (délai dépassé à la dernière tentative, `PermanentError` levée par une étape,
  job perdu). Nouvelle tâche périodique `investigate:stalled`
  (`services/investigate/stalled.py`) : échec après 30 min sans job actif, avec la
  dernière erreur du job.
- Autre cause possible, non bloquante : un job reporté (`RetryLater`, par exemple budget
  `level:N3` épuisé). Il ne consomme pas de tentative et peut attendre longtemps.
  `/factchecks` affiche maintenant la raison et le prochain essai (`run_at`,
  `job_error`), et signale une étape sans job.
- Demandes absentes : la liste ne montrait que les jobs en file ou en cours. Elle montre
  aussi les demandes échouées des dernières 24 h. Une demande réussie dont l'extraction
  n'a donné aucune enquête (aucune affirmation vérifiable, verdict réutilisé, non
  vérifiable en N3), ou dont le brouillon est assemblé mais retenu par le quota
  quotidien, n'apparaît toujours pas.
- Vérifications : ruff, mypy strict OK ; un test ajouté (enquête bloquée), test de carte
  adapté ; tests de gate (fact-check), du bot, des services et d'isolation des imports :
  201 réussis.
