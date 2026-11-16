# Badges de vérification Seat 12A

Sources : `core/domain/badges.py` (règles pures), `core/usecases/badges.py`,
`badge_affiliations.py`, `badge_submissions.py`, `badge_sources.py`,
`core/models/badges.py`, `gate/routes/badges.py`, `gate/routes/org.py`,
`services/badges/` (service hangar), `core/adapters/wikidata_badges.py`.

Gestion par l'équipe dans Discord : commande `/badge` ([intercom.md](intercom.md#commandes)).

Les badges Seat 12A remplacent les vérifications de X : l'extension masque les badges X
et affiche les nôtres, obtenus par le lookup en lot (`GET /v1/badges/lookup`).

## Principes

- **Un badge par compte**, identifié par la plateforme et l'**identifiant natif**
  (ID numérique X, DID Bluesky, etc.), **jamais par le pseudo**. Le pseudo stocké
  (`platform_accounts.handle`) n'est qu'informatif.
- **Multi-plateforme** : tout passe par `platform_accounts (platform, external_id)`. Les
  plateformes reconnues sont celles de `Platform` (`x`, `bluesky`, `threads`, `mastodon`,
  `youtube`, `tiktok`, `telegram`, `web`) ; les identifiants non-X sont des chaînes opaques
  imprimables (≤ 256 caractères). Les sources automatiques (Wikidata, X) ne concernent
  aujourd'hui que X ; l'attribution manuelle, les affiliations, les demandes, les
  signalements et le lookup fonctionnent déjà pour toute plateforme.
- **Une décision de l'équipe l'emporte toujours sur une source** (voir « Sources
  automatiques »).
- **Historique complet** dans `badge_events` : attribution, modification, retrait,
  blocage, déblocage, affiliations (création, validation, reconfirmation, rejet,
  retrait, expiration, principale), clés d'organisation ; chaque ligne porte l'acteur
  (`staff:<discord_id>@intercom`, `organization:<uuid>`, `system:badges`), la source, le
  motif et les changements.
- **Égalité de traitement** : aucune règle ne dépend du bord politique. La catégorie
  vient de la fonction (profession, type d'organisation), jamais d'une opinion.

## Catégories

| Valeur API | Nom | Couleur | Pour |
|---|---|---|---|
| `institutional` | institutionnel | `#FF7A00` | gouvernements, administrations, élus, partis, organisations internationales |
| `organization` | organisation | `#FFD60A` | entreprises, associations, ONG, clubs |
| `media` | média | `#D946EF` | médias, agences, journalistes |
| `expert` | expert | `#22D3EE` | chercheurs, médecins, universitaires |
| `personality` | personnalité | `#3B5BFF` | artistes, sportifs, créateurs |

Priorité si un compte peut prétendre à plusieurs badges :
**institutional > media > expert > organization > personality** (`pick_category`).
`GET /v1/badges/categories` renvoie ce tableau (couleurs et rang) pour l'extension.

## États d'un badge

Une ligne `badges` par compte, quel que soit l'état :

| `status` | Sens |
|---|---|
| `active` | badge affiché (`category` obligatoire) |
| `revoked` | pas de badge. Retiré par l'équipe : **définitif pour les sources** (Wikidata et X ne le redonnent jamais) ; retiré par le système (`ended_by` = `system:…`) : une source peut le redonner |
| `blocked` | pas de badge et **aucune source** (Wikidata, X, demande) ne peut en donner ; les affiliations du compte tombent ; il faut débloquer (le compte repasse `revoked`) puis attribuer |

`source` : `manual` (équipe), `request` (demande approuvée), `wikidata`, `x_government`.
Une modification de catégorie par l'équipe rend le badge `manual` : les sources ne le
touchent plus.

## Affiliations

- Lien entre un compte individuel et une **organisation badgée** `organization`,
  `media` ou `institutional` (sinon `TWR-BDG-006`). Un compte ne peut pas s'affilier à
  lui-même (`TWR-BDG-020`).
- Le compte affilié **hérite de la couleur** de l'organisation, sauf s'il a son propre
  badge ; l'extension affiche le **logo** de l'organisation (`logo_url`).
- Plusieurs affiliations possibles en base, **une seule principale affichée**
  (`is_primary`, index unique partiel). La première validée devient principale ; quand
  la principale se termine, la plus ancienne active restante prend le relais. L'équipe
  peut changer la principale.
- **Validation obligatoire** par l'organisation (clé d'organisation) ou par l'équipe :
  une affiliation `pending` n'est pas affichée.
- **Expiration** un an après la dernière validation ou reconfirmation (`expires_at`).
  Le lookup ignore une affiliation échue même avant le passage de la tâche périodique,
  qui la passe `expired` (heure par heure). Une affiliation `expired` peut être
  revalidée.
- **Si l'organisation perd son badge** (retrait, blocage, ou catégorie non affiliable),
  toutes ses affiliations tombent : `active` → `revoked`, `pending` → `rejected`
  (motif `org_badge_revoked` ou `org_category_changed`).

| Transition | Qui | Endpoint |
|---|---|---|
| création `pending` | équipe (`validate_now: false`) | `POST /v1/badges/affiliations` |
| création `active` | équipe (défaut) ou organisation | `POST /v1/badges/affiliations`, `POST /v1/org/affiliations` |
| `pending`/`expired` → `active` | équipe ou organisation | `POST /v1/badges/affiliations/{id}/validate` |
| `active` → `active` (+1 an) | équipe ou organisation | `.../reconfirm` |
| `pending` → `rejected` | équipe ou organisation | `.../reject` |
| `active` → `revoked` | équipe ou organisation | `.../revoke` |
| principale | équipe | `.../primary` |

Une affiliation `rejected`, `revoked` ou `expired` peut être recréée par l'équipe ou
l'organisation (la ligne est rouverte) ; **l'import Wikidata ne rouvre jamais** une
affiliation terminée.

## Organisations : clés d'organisation

Une organisation badgée gère ses affiliés avec une **clé d'organisation** délivrée par
l'équipe (`POST /v1/badges/accounts/{platform}/{id}/org-keys`), affichée une seule fois
(`t12o_…`, seule l'empreinte SHA-256 est stockée). En-tête :
`X-Organization-Token: t12o_…`, jamais combiné à un autre identifiant (`TWR-BDG-017`).

- Acteur `organization:<public_id du badge>`, permissions publiques + `badges:org_manage`.
- La clé n'est valide que si elle n'est pas révoquée **et** que le badge de
  l'organisation est actif (`TWR-BDG-016` sinon), au plus tard après
  `TOWER_AUTH_CACHE_TTL`.
- L'organisation ne voit et ne modifie que ses propres affiliations (`TWR-BDG-018`).
- Ses ajouts sont actifs immédiatement (validés par l'organisation, `source`
  `organization`).

## Demandes et signalements

- **Demande** (`POST /v1/badges/requests`, public, limité par IP) : `kind` `badge`
  (catégorie demandée) ou `affiliation` (organisation badgée). Une seule demande en
  attente par (type, compte, organisation) (`TWR-BDG-013`). Le contact facultatif n'est
  lu que par l'équipe : jamais public, jamais dans les événements. L'équipe approuve
  (badge `request`, catégorie modifiable ; ou affiliation active validée par l'équipe)
  ou rejette.
- **Signalement** (`POST /v1/badges/reports`, public, limité par IP) : motif
  `wrong_category`, `not_eligible`, `impersonation`, `wrong_affiliation`, `other` et
  commentaire facultatif. **Rien n'est stocké sur l'auteur** (ni IP, ni token). Seul un
  compte qui affiche quelque chose peut être signalé (aucune ligne créée pour un ID
  inconnu). L'équipe clôt en `resolved` ou `dismissed`.
- Limite des soumissions publiques (demandes, signalements, indices X) : seau à jetons
  par IP et par réplica, 10 d'affilée puis 1 par minute (`TWR-BDG-019`, 429). L'IP n'est
  ni journalisée ni stockée.

## Sources automatiques

Toutes passent par `apply_automatic` :

| Situation | Effet |
|---|---|
| aucune ligne, ou retrait par le système | badge attribué |
| `blocked`, ou retrait par l'équipe | rien |
| actif, même source automatique | la catégorie suit la source ; libellé et logo remplis s'ils sont vides |
| actif `wikidata`, X dit « government » | devient `x_government` (`institutional`) |
| actif `x_government` | Wikidata ne fait que renseigner le QID |
| actif `manual` ou `request` | catégorie inchangée ; QID renseigné |
| QID différent de celui déjà lié au compte | ignoré (sûreté) |

### Wikidata (SPARQL)

Vérifié en direct le 2026-11-16 sur `https://query.wikidata.org/sparql` :
l'ID numérique X (**P6552**) est un **qualificatif** de la déclaration du pseudo X
(**P2002**) (~388 000 déclarations, contre ~100 en déclaration principale). Certaines
valeurs ne sont pas numériques : elles sont ignorées.

1. Tâche périodique `badges:schedule` (horaire) : chaque portée de
   `TOWER_BADGES_WIKIDATA_SCOPES` (QID de pays ; `intl` = organisations internationales
   et intergouvernementales) est importée toutes les `TOWER_BADGES_WIKIDATA_INTERVAL_S`
   (hebdomadaire). Lancement manuel : `POST /v1/badges/imports/wikidata`.
2. Job `badges.wikidata_scope` : liste (élément, ID X, pseudo) des personnes de
   nationalité P27 = pays et des organisations de pays P17 = pays, avec une déclaration
   P2002 **sans date de fin (P582)** et non dépréciée. Un ID X revendiqué par plusieurs
   éléments est ignoré. Un job `badges.wikidata_batch` par lot de 200.
3. Job `badges.wikidata_batch` : lit par `VALUES` (une requête) P31 (nature), P106
   (profession), libellé français, P154 (logo, organisations seulement) et les
   affiliations **P108** (employeur), **P102** (parti), **P54** (équipe), **P463**
   (membre de), **en ignorant les déclarations qui ont un P582**.
4. Catégorie (`classify_wikidata`) : une personne (P31 = Q5) est classée par ses
   professions P106 seulement, une organisation par ses natures P31 seulement, avec la
   priorité des catégories. Les tables `WIKIDATA_OCCUPATIONS` et `WIKIDATA_TYPES` sont
   **volontairement restreintes** (QID vérifiés un par un) : un élément non listé n'a pas
   de badge. Pas de remontée des sous-classes (P279) : trop coûteux en SPARQL.
5. Affiliations : les liens lus sont gardés dans `badge_wikidata_links`, si bien qu'une
   affiliation est proposée quel que soit l'ordre d'import du membre et de
   l'organisation. Elles sont créées **`pending`** (validation obligatoire), jamais pour
   un membre bloqué, et une affiliation déjà terminée n'est jamais rouverte.
6. Retrait : un badge `wikidata` qui n'est plus vu depuis
   `TOWER_BADGES_WIKIDATA_STALE_DAYS` est retiré par le système (motif
   `wikidata_no_longer_listed`), **seulement si l'import tourne** (un badge Wikidata vu
   dans la dernière période + 1 jour) : une panne de Wikidata ne retire rien.

Les éléments n'ayant qu'un pseudo X sans P6552 ne sont **pas** importés (jamais
d'identification par pseudo ; leur résolution coûterait des appels à l'API X).

### X : comptes vérifiés « government »

L'API X v2 expose `verified_type` (`blue`, `business`, `government`, `none`). Les comptes
`government` (coche grise : gouvernements et organisations multilatérales) reçoivent un
badge `institutional`, source `x_government`.

1. L'extension envoie les IDs des auteurs qu'elle voit avec la coche grise
   (`POST /v1/badges/x-government-hints`, ≤ 50 IDs, limité par IP). **Un indice n'est
   jamais cru** : il met seulement le compte en file de vérification (sauf s'il a déjà
   un badge actif ou est bloqué, ou s'il a été vérifié depuis moins de
   `TOWER_BADGES_X_RECHECK_DAYS`).
2. Job `badges.x_verify` : `GET /2/users?ids=…&user.fields=verified_type` par 100 IDs au
   plus, dans la limite de `TOWER_BADGES_X_DAILY_CHECKS` comptes par jour UTC et du
   budget `provider:x` (réservation puis règlement au nombre de comptes renvoyés, comme
   le radar). Sans `TOWER_X_BEARER_TOKEN` : rien n'est appelé (log
   `badges.x_not_configured`).
3. Revérification des badges `x_government` tous les `TOWER_BADGES_X_RECHECK_DAYS` :
   si X ne confirme plus (autre type, compte absent de la réponse), retrait par le
   système (motif `x_verification_lost`).

## Lookup pour l'extension

`GET /v1/badges/lookup?platform=x&ids=1,2,3` (public, ≤ 100 IDs natifs, une requête SQL) :

```json
{
  "platform": "x",
  "accounts": {
    "2": {
      "category": "media",
      "color": "#D946EF",
      "badge": null,
      "affiliation": {
        "org": {"platform": "x", "external_id": "100", "category": "media",
                "color": "#D946EF", "label": "Le Journal", "logo_url": "https://…"},
        "title": "Reporter"
      }
    }
  }
}
```

- Un ID **absent** de `accounts` n'a pas de badge Seat 12A (l'extension masque quand
  même le badge X).
- `category` et `color` : ce qu'il faut afficher (badge propre, sinon couleur de
  l'organisation de l'affiliation principale). `badge` : badge propre ;
  `affiliation` : affiliation principale active et non échue.
- **Cache** : `Cache-Control: public, max-age=TOWER_BADGES_LOOKUP_MAX_AGE_S` (300 s) et
  `ETag` fort ; `If-None-Match` → `304`. Trier les IDs améliore les succès d'un cache
  partagé. Les changements publient `badge.changed` et `badge.affiliation_changed`.
- ID invalide : `TWR-ING-006` ; lot vide ou > 100 : `TWR-BDG-003`.

`GET /v1/badges/accounts/{platform}/{id}` : détail public d'un compte (badge et toutes
ses affiliations actives, principale en tête), `404 TWR-BDG-001` s'il n'affiche rien.

## Permissions

| Permission | Qui | Pour |
|---|---|---|
| `badges:read_public` | public, extension, organisations | lookup, détail public |
| `badges:submit` | public, extension, organisations | demandes, signalements, indices X |
| `badges:read` | relecteur, éditeur | liste, fiche équipe, historique, demandes, signalements, affiliations, clés, imports |
| `badges:manage` | éditeur | attribuer, retirer, bloquer, affilier, trancher, clés d'organisation, import |
| `badges:org_manage` | acteur `organization` seulement (non attribuable) | ses affiliations |

## Service hangar `badges`

File `badges`. Activation : `TOWER_SERVICES=…,badges` (léger : peut cohabiter avec
`ingest`/`radar`). httpx et les adaptateurs ne sont chargés qu'au premier job.

| Job / tâche | Rôle |
|---|---|
| `badges.wikidata_scope` | liste d'une portée, un job de lot par 200 comptes (`badge_import_runs`) |
| `badges.wikidata_batch` | détails et application d'un lot |
| `badges.x_verify` | vérification X d'au plus 100 comptes |
| `badges:schedule` (horaire) | imports dus, vérification X si des comptes attendent |
| `badges:expire` (horaire) | affiliations échues → `expired` ; badges Wikidata périmés |

Erreurs techniques : `TWR-BDG-021` (Wikidata indisponible, relance dans 10 min),
`TWR-BDG-022` (X indisponible). Endpoints : [api.md](api.md#badges-de-vérification).
Tables : [database.md](database.md#badges-de-vérification-migration-0013).
