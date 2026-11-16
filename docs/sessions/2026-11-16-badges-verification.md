# 2026-11-16 — Badges de vérification Seat 12A

## Objectif

Jules : ajouter un système de badges de vérification Seat 12A qui remplace les
vérifications de X dans l'extension. Cinq catégories colorées avec priorité, un badge
par compte identifié par l'ID natif (X aujourd'hui, autres plateformes demain), jamais
par le pseudo. Affiliations (couleur et logo de l'organisation, une principale,
validation obligatoire, expiration à un an, chute si l'organisation perd son badge).
Sources : import Wikidata (SPARQL : P2002, P6552, P106, P108/P102/P54/P463 sans P582),
reprise des comptes X vérifiés « government », blocage possible par l'équipe, ajout et
affiliation manuels. Endpoints de demandes, de gestion des affiliés par les
organisations, de signalement, lookup en lot pour l'extension pensé pour le cache.
Historique (attribution, retrait, qui a validé).

## Réalisé

- Domaine pur `core/domain/badges.py` : catégories, couleurs, priorité, statuts, tables
  de classification Wikidata (QID vérifiés en direct), validations ; codes
  `TWR-BDG-001` à `023`.
- Migration `0013` (expand) et modèles `core/models/badges.py` : `badges`,
  `badge_affiliations`, `badge_events`, `badge_requests`, `badge_reports`,
  `badge_org_keys`, `badge_wikidata_links`, `badge_import_runs`, `badge_x_checks`.
- Cas d'usage : `badges.py` (attribution, retrait, blocage, lookup en une requête,
  détail public, historique, clés d'organisation, règles des sources automatiques),
  `badge_affiliations.py`, `badge_submissions.py` (demandes, signalements),
  `badge_sources.py` (lots Wikidata, liens d'affiliation, retrait des badges périmés,
  indices et résultats X).
- Auth : acteur `organization` (`X-Organization-Token: t12o_…`), permissions
  `badges:read_public`, `badges:submit` (public), `badges:read` (relecteur, éditeur),
  `badges:manage` (éditeur), `badges:org_manage` (organisations).
- gate : `routes/badges.py` (public, équipe, affiliations, demandes, signalements,
  imports) et `routes/org.py` ; limiteur par IP des soumissions ; CORS
  (`X-Organization-Token`, `If-None-Match`, `ETag` exposé).
- Service hangar `badges` : jobs `badges.wikidata_scope`, `badges.wikidata_batch`,
  `badges.x_verify` ; tâches `badges:schedule` et `badges:expire`. Adaptateur
  `core/adapters/wikidata_badges.py` ; `XPlatformClient.get_accounts` (lot de 100,
  `verified_type`) ; ports `core/ports/badges.py`.
- intercom : catégorie « Badges » dans `/permissions` (fr, en).
- **Deuxième phase (même jour, demande de Jules)** : commande Discord `/badge`
  (`intercom/commands/badges.py`, `handlers/badges.py`, catalogues `i18n/badge_en.py` et
  `badge_fr.py`, méthodes de `gate_client.py`) : `show`, `grant`, `revoke`, `block`,
  `unblock`, `history`, `affiliate`, `affiliation`, `queue`, `request`, `report`,
  `org-keys`, `import`. Cartes V2 persistantes (7 `DynamicItem`), modals pour tout texte,
  file de travail à onglets. gate : `GET /v1/badges/requests/{id}` et
  `/reports/{id}` ; identifiants courts pour `affiliation`, `badge_request`,
  `badge_report`, `org_key`. Table `/help` et messages d'erreur `TWR-BDG-*` du bot.
- Documentation : nouvelle page `badges.md` ; `api.md`, `database.md`, `events.md`,
  `configuration.md`, `.env.example`, `auth.md`, `services.md`, `intercom.md`,
  `deployment-railway.md`, `README.md`, `decisions.md`, `errors.md` (régénéré).

## Décisions

Voir `decisions.md`, section « Badges de vérification Seat 12A ». En bref : valeurs
d'API en anglais ; une ligne par compte avec statut `blocked` ; décision de l'équipe
toujours prioritaire sur les sources ; clés d'organisation délivrées par l'équipe ;
affiliations Wikidata toujours `pending` ; P6552 lu en qualificatif de P2002 (constaté
en direct) ; indices X jamais crus sans l'API X ; lookup en `GET` avec `ETag`.

## Vérifications

- `ruff format --check`, `ruff check`, `mypy` strict : verts.
- **Tests sans base** : 1009 verts (dont `test_badges_domain.py`, isolation des imports
  avec 3 nouveaux tests, catalogue `errors.md`). 4 tests existants adaptés aux nouvelles
  permissions publiques et au catalogue de `/permissions`.
- **Tests avec base : suite complète NON exécutée.** pgvector n'est pas installé dans ce
  conteneur et son installation (compilation + `make install`) a été refusée par le
  garde-fou de permissions ; la chaîne de migrations (0007 et suivantes) ne peut donc
  pas tourner. À la place, sur le PostgreSQL 16 du conteneur :
  - `tests/test_badges.py` (24 tests : cas d'usage, jobs du service avec doublures,
    endpoints de gate) et `tests/test_gate_staff.py` exécutés sur un **schéma partiel**
    (tables du socle nécessaires + tables des badges, créées depuis les modèles) :
    24/24 badges verts ; staff 31/32, le seul échec vient de `watched_accounts` absente
    du schéma partiel (artefact du banc, pas une régression).
  - Migration `0013` appliquée sur une base contenant `entities` et
    `platform_accounts` : **aucune différence** entre migration et modèles
    (`compare_metadata`), puis descente (tables supprimées) et remontée OK.
  - `alembic check` sur la chaîne complète : non exécuté (même cause).
- **Smoke test des vrais processus** (même base partielle) : `tower gate` (attribution
  par token de service créé avec la CLI, lookup avec `Cache-Control` et `ETag`, clé
  d'organisation) ; `tower hangar` avec `TOWER_SERVICES=badges` contre un **faux serveur
  SPARQL** local : import de la portée, lot appliqué, badges créés, affiliation
  Wikidata proposée en `pending`.
- **Appels réels** : seulement des requêtes SPARQL de lecture à `query.wikidata.org`
  pendant la conception (comptages, vérification des QID, essai de l'adaptateur). Aucun
  appel à l'API X (doublure `FakeX` dans les tests ; pas de jeton), aucun appel de modèle.
- Phase `/badge` : `tests/test_intercom_badges.py` (10 tests fr/en contre un vrai gate
  ASGI, schéma partiel) et `test_badges.py` : 34/34 verts ; tests statiques du bot
  (`test_intercom_ui`, `i18n`, `team`, isolation des imports) : 80 verts ; `tower
  intercom --dry-run` contre un vrai gate : **17 commandes** enregistrées (16 avant). RAM
  intercom (dry-run, 8 s, « avant » = `1c26be4`) : 79 à 90 Mo avant, 80 Mo après.
  **Non vérifié** : rien n'a tourné contre un vrai Discord (rendu des cartes, modals,
  clics après redémarrage).
- RAM (processus lancé directement, mesure à 8 s, « avant » = worktree de `ce7f1dd`) :
  gate 94 à 99 Mo avant, 98 à 99 Mo après (bruit, cible +5 Mo) ; hangar `ping` 69 Mo
  avant et après ; hangar `ping,badges` au repos 81 Mo (+12 Mo, cible +20 Mo) ; hangar
  `badges` après traitement d'un import 90 Mo (httpx chargé). intercom non mesuré
  (seulement des chaînes i18n et une catégorie ajoutées).

## Points ouverts / suite

- **Lancer la suite complète avec pgvector** (`alembic check`, migrations aller-retour,
  tous les tests) avant la fusion.
- Ajouter `badges:read badges:manage` au token du bot, `badges` à `TOWER_SERVICES` du
  hangar sur Railway.
- Extension : appeler `/v1/badges/lookup` par lots triés, respecter `ETag`, envoyer les
  indices `x-government-hints` ; site : formulaires de demande et de signalement.
- Tester `/badge` sur le serveur (rendu des cartes, modals, menu de révocation des clés).
- Enrichir au besoin les tables de QID (`WIKIDATA_OCCUPATIONS`, `WIKIDATA_TYPES`) ; les
  sous-classes ne sont pas suivies.
- Volume : le premier import Wikidata proposera beaucoup d'affiliations `pending`
  (partis, employeurs) ; prévoir une file de validation dans le bot.
- Le `User-Agent` SPARQL est générique (pas d'adresse de contact) : la politique
  Wikimedia recommande un contact ; à compléter si Jules le souhaite.
