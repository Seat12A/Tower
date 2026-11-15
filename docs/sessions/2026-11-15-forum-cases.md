# 2026-11-15 — Forum des cases, messages publics en anglais, renommage « dossier » → « case »

## Objectif

Jules : (1) messages publics (salons, forum) automatiquement en anglais ; (2) le salon
des dossiers, illisible à des milliers de fact-checks (3 à 4 messages par dossier,
boutons et infos éparpillés), devient un **forum** : un post par dossier, infos de base
dans le message d'origine, le reste bien rangé dans le fil, tags pour le type et le
statut, icônes avec parcimonie (tout ou rien par type d'élément) ; (3) remplacer
« dossier » par « case » **partout**, technique compris, par un moyen qui ne consomme
pas d'usage.

## Réalisé

- **Forum** (`intercom/forum.py`, rendu pur `handlers/forum.py`, Discord
  `delivery.ForumDelivery`) : message d'origine = seul panneau de contrôle (statut et
  niveau, affirmation, verdict proposé, note avec longueur, comptes d'avertissements,
  une barre d'actions, un menu « More actions » persistant `t12a:fc:more:<id>`, bouton
  « Mark as published » une fois validé) ; fil en sections fixes (Evidence avec sources
  dédupliquées, Gaps & checks, Quality warnings, Context) puis journal d'activité ;
  18 tags modérés (statut, issue, niveau, signaux) ; post mis à jour par les événements
  (`case.opened`, `draft.ready`, `draft.reviewed`, `case.transitioned`), archivé une
  fois final ; post épinglé « How to review ». Repli salon texte sans forum.
- Gate : `cases.discord_thread_id` (migration 0011), `PUT /v1/cases/{id}/discord-thread`,
  `TWR-DOS-007`, `draft_id` et `discord_thread_id` dans le détail du case.
- Messages publics toujours en anglais (`TOWER_DISCORD_LOCALE` dépréciée) ; alertes de
  vague dans `TOWER_DISCORD_CHANNEL_NARRATIVES`.
- Icônes : règle tout ou rien codée (`all_or_none`) ; 10 icônes à fournir déclarées
  vides dans `ui/emojis.py`.
- **Renommage** par script Python (aucun appel de modèle) : 104 fichiers, 7 fichiers
  renommés ; puis compatibilité expand écrite à la main : migration 0012 (renommage des
  tables, colonnes, index, contraintes, séquences + vues aux anciens noms), alias
  `/v1/dossiers`, `dossiers:*`, topics `dossier.*`, `TOWER_DISCORD_CHANNEL_DOSSIERS`,
  clé `dossier_investigation_id`, raison `dossier_opened`.
- Docs : `intercom.md`, `api.md`, `auth.md`, `database.md`, `events.md`,
  `configuration.md`, `.env.example`, `errors.md`, `decisions.md`, `roadmap.md`, et tout
  le vocabulaire des docs (hors résumés de session).
- Aucun sous-agent.

## Décisions

Voir `decisions.md`, sections « Forum des dossiers et messages publics en anglais » et
« Renommage « dossier » → « case » ».

## Vérifications

- `ruff format --check`, `ruff check`, `mypy` strict : verts.
- Tests sans base : **1 261 verts** (dont 22 nouveaux pour le forum, 2 pour les alias).
- **Non vérifié** : tout ce qui demande PostgreSQL. pgvector n'est pas installé dans ce
  conteneur et sa compilation (`apt-get install postgresql-server-dev-16 …`) a été
  refusée par le mode automatique : ~800 tests avec base, `alembic check`, aller-retour
  des migrations 0011 et 0012 (vues de compatibilité comprises), smoke test gate +
  `tower intercom --dry-run`, mesure de RAM d'intercom. Rien n'a tourné contre un vrai
  Discord (forum, tags, menu, archivage, limite de fils actifs). Aucun appel de modèle
  ni d'API externe.

## Points ouverts / suite

- Lancer les vérifications avec base (accord pour compiler pgvector, ou en local).
- Sur Railway : créer le forum, renseigner `TOWER_DISCORD_FORUM_CASES` et
  `TOWER_DISCORD_CHANNEL_NARRATIVES`, retirer `TOWER_DISCORD_LOCALE` ; droits du bot sur
  le forum (Gérer le salon, Gérer les fils, Envoyer des messages dans les fils).
- Fournir les 10 icônes manquantes (liste dans `ui/emojis.py` et `intercom.md`).
- Passer le site et l'extension à `/v1/cases` et `cases:*`, puis faire le **contract**
  du renommage (voir `roadmap.md`).
- Texte des modèles toujours en français : traduction à décider (coût).
