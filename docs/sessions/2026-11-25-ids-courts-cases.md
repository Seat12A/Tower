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

VERIFICATIONS

## Points ouverts / suite

- **Site et extension** (hors dépôt) : construire les liens et les appels avec
  `short_id`.
- **Contract** : retirer `public_id` des réponses des cases quand plus aucun client ne le
  lit.
- Collision simultanée de deux insertions qui partagent leurs 8 caractères : la seconde
  échoue sur la contrainte d'unicité et doit être relancée (un job l'est
  automatiquement). Probabilité de l'ordre de 1 sur 4 milliards par paire simultanée.
