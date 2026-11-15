# 2026-11-15 — Refonte d'intercom (Components V2) et permissions de l'équipe

## Objectif

Jules : refaire complètement les commandes et l'interface d'intercom avec le kit de
design de son bot Moddy (Components V2, émojis perso, vues persistantes, modals V2),
modifier les commandes au besoin, et ajouter un système de permissions : admins codés
en dur, permissions détaillées, commande de modification, permission « modifier les
autres » et « modifier les siennes », hiérarchie des rôles Discord, rang obligatoire
(`/rank`, `/unrank`, liste).

## Réalisé

- Kit copié dans `docs/design/` (règles, Components V2, modals V2, vues persistantes,
  correspondance des émojis).
- Socle UI : `handlers/__init__.py` (carte `Reply` en données), `ui/emojis.py`,
  `ui/render.py`, `ui/base.py`, `commands/__init__.py` (registre des modules et des
  `DynamicItem`), i18n par domaine (`ops`, `fc`, `team`, vues `ChainMap`), `app.py`
  reconstruit, `delivery.py`.
- Commandes portées en cartes V2 avec boutons, menus et modals persistants : liste de
  suivi, dossiers, incidents, usage, profils, niveaux, registre, fact-check, groupes,
  fiches, récits ; notifications et brouillons en cartes (anciens `fc:` acceptés).
- Équipe : `/team`, `/rank`, `/unrank`, `/permissions`, `/help` ; gate : `GET /v1/staff/me`,
  `GET /v1/staff`, `GET|PUT|DELETE /v1/staff/{id}`, `PUT /v1/staff/{id}/permissions` ;
  use cases `core/usecases/staff.py` ; migration 0010 (`staff_members.permissions`) ;
  permissions `staff:read`, `permissions:manage_others`, `permissions:manage_self` ;
  `TOWER_ADMIN_DISCORD_IDS` ; codes `TWR-STF-001` à `007` ; topics `staff.ranked`,
  `staff.unranked`, `staff.permissions_changed`.
- Documentation : `intercom.md`, `auth.md`, `api.md`, `database.md`, `events.md`,
  `configuration.md`, `.env.example`, `errors.md`, `decisions.md`.
- Travail réparti : socle, relecture, intégration par l'agent principal ; trois
  sous-agents Sonnet (permissions de bout en bout ; commandes opérationnelles ;
  fact-check, notifications, brouillons). Corrections à l'intégration : cache des
  acteurs vidé aussi après le commit (tâche de fond), règle d'édition sur place commune
  (`stays_in_place`), catalogues i18n vivants.

## Décisions

Voir `decisions.md`, section « Refonte d'intercom et permissions de l'équipe ».

## Vérifications

- pgvector 0.8 compilé dans le conteneur (accord de Jules) : **suite complète 2030
  tests verts** sur PostgreSQL réel ; `ruff format --check`, `ruff check`, `mypy` strict
  verts ; `alembic check` OK ; migration 0010 aller-retour OK.
- Smoke test : gate réel lancé avec `TOWER_ADMIN_DISCORD_IDS` ; `/v1/staff/me` d'un admin
  codé en dur (toutes les permissions) ; rang d'un membre par `PUT /v1/staff/{id}` ;
  `tower intercom --dry-run` contre ce gate : 16 commandes, notificateur connecté
  (réconciliation refusée faute de `dossiers:read` sur le token de test, attendu).
- RAM intercom (dry-run, 8 s) : avant 79 à 90 Mo, après 80 à 87 Mo (bruit de mesure,
  écart sous la cible de +10 Mo).
- **Non vérifié** : rien n'a tourné contre un vrai Discord (rendu réel des cartes,
  soumission des modals, clics après redémarrage, `guild.fetch_member`, mention du rôle
  d'alerte). Aucun appel de modèle ni d'API externe.

## Points ouverts / suite

- Ajouter au token du bot : `staff:read staff:manage permissions:manage_others
  permissions:manage_self` ; renseigner `TOWER_ADMIN_DISCORD_IDS` sur Railway (service
  gate).
- Tester en vrai sur le serveur : `/help`, `/rank`, `/permissions`, un brouillon.
- Thème clair (jumeaux `_dark` des icônes) : non repris, facultatif.
- `/help` utilise une table commande → permission dans `handlers/team.py` : la tenir à
  jour à chaque nouvelle commande.
