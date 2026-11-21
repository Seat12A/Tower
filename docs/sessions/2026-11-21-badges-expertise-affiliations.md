# 2026-11-21 — Badges : expertise, « vérifié », affiliations indépendantes

## Objectif

Demandes de Jules sur les badges de vérification :

1. préciser le domaine des experts ;
2. renommer le badge « personnalité » en « vérifié » (`verified`), plus large ;
3. affiliations : liées à un compte X parent, plusieurs organisations possibles, badge
   indépendant de l'affiliation (logo à côté du pseudo sans forcément de coche), logo
   configurable.

## Réalisé

- **Domaine** (`core/domain/badges.py`) : `BadgeCategory.VERIFIED` remplace
  `PERSONALITY` ; alias `personality` accepté partout (`_missing_`,
  `LEGACY_CATEGORIES`), `normalize_category`, `stored_values` pour les filtres SQL ;
  `check_expertise` (≤ 120 caractères, réservé à `expert`) ; table
  `WIKIDATA_EXPERTISE` et `wikidata_expertise` (domaine déduit des professions P106).
  Nouveau code `TWR-BDG-027` ; `docs/errors.md` régénéré.
- **Migration `0015`** (expand) : colonne `badges.expertise` ; contraintes de catégorie
  de `badges` et `badge_requests` élargies à `verified` (en plus de `personality`).
  Aucune réécriture de données (voir points ouverts).
- **Cas d'usage** : `grant_badge` et `approve_request` prennent `expertise` (effacée si
  la catégorie n'est plus `expert`) ; réécriture silencieuse de `personality` →
  `verified` quand l'équipe réattribue la même catégorie ; `apply_automatic` remplit
  l'expertise Wikidata seulement si elle est vide ; payloads et événements toujours en
  `verified`.
- **Lookup** : `category`/`color` viennent **du badge propre seulement** (plus
  d'héritage de la couleur de l'organisation) ; `affiliation` = principale (logo à côté
  du pseudo) ; nouveau `affiliations` (actives, principale en tête, 5 au plus) ;
  `badge.expertise`. Deux requêtes SQL au lieu d'une.
- **Affiliations** : le lien au compte parent existait déjà (affiliation → badge de
  l'organisation → `platform_accounts`, ID X natif), ainsi que plusieurs affiliations par
  compte et le choix de la principale par l'équipe. Le logo est le `logo_url` du badge de
  l'organisation, déjà modifiable par l'équipe (`PUT`, `/badge grant`/« Modifier »).
- **gate** : `expertise` dans `PUT /v1/badges/accounts/…` et l'approbation des
  demandes ; schéma du lookup mis à jour.
- **intercom** : catégorie « Vérifié » ; champ « Domaine d'expertise » dans les modals
  d'attribution et d'approbation (le rappel du compte passe dans l'aide du champ
  catégorie : Discord limite un modal à 5 composants) ; expertise affichée sur la carte.
- **Documentation** : `badges.md`, `extension-api.md`, `extension.md`, `api.md`,
  `database.md`, `events.md`, `intercom.md`, `decisions.md`, `errors.md`.

## Décisions

Voir `decisions.md`, « Badges : expertise, « vérifié », affiliations indépendantes ».
En bref : renommage en expand/contract avec alias ; expertise en texte libre français,
facultative ; l'organisation parente doit rester badgée (son logo engage Seat 12A) ; logo
affiché = affiliation principale.

## Vérifications

- `ruff format --check`, `ruff check`, `mypy` strict : verts.
- **PostgreSQL réel** : cette fois pgvector 0.8.7 a pu être installé dans le conteneur
  (paquet PGDG extrait à la main), la chaîne de migrations complète tourne.
  `tests/test_badges.py` + `tests/test_badges_domain.py` : 49 verts (nouveaux tests :
  expertise, lignes `personality` héritées lues comme `verified`, alias en entrée de
  l'API, affiliation sans coche, liste des affiliations, `wikidata_expertise`).
- Suite complète : 2182 verts, 18 échecs **préexistants** (`test_drafts_usecases`, `test_intercom_groups`, `test_intercom_narratives`, `test_source_standing_db`) : les mêmes 18 échouent sur le commit de départ sans ces modifications (vérifié dans un worktree), sans lien avec les badges. Non corrigés ici.
- `alembic check` : aucune différence ; migrations aller-retour `0015 → 0014 → 0015`
  OK ; contraintes vérifiées en base.
- RAM (gate lancé directement, mesure à 8 s, « avant » dans un worktree de la base) :
  ~104 Mo avant, ~103 Mo après (écart dans le bruit) ; lookup réel servi par le vrai
  processus. Hangar et intercom non mesurés (aucun import ajouté).
- Rien d'externe appelé (ni X, ni Wikidata, ni modèle).

## Points ouverts

- **Migration contract** à faire dans un lot ultérieur (après déploiement de ce code sur
  tous les services) : `UPDATE badges SET category='verified' WHERE
  category='personality'` (idem `badge_requests.requested_category`), puis retirer
  `personality` des contraintes et de `LEGACY_CATEGORIES`.
- **Extension et site** (hors dépôt) à adapter : plus de couleur héritée, `affiliations`,
  `expertise`, catégorie `verified`.
- La branche `claude/blissful-rubin-sldj13` (non fusionnée) a ses propres migrations
  `0015` et `0016` : selon l'ordre de fusion, renuméroter la migration badges (`0017`,
  parent `0016`).
- À confirmer par Jules : « configurer le logo » = choisir la principale + modifier le
  `logo_url` de l'organisation (fait) ; si un logo propre à chaque affiliation était
  voulu, il faudra une colonne `badge_affiliations.logo_url`.
