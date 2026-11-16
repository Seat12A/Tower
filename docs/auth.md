# Authentification et autorisations

Sources : `core/domain/security.py`, `core/auth/`, `gate/deps.py`.

## Acteurs

| Type | Comment | Droits |
|---|---|---|
| `anonymous` | aucune en-tête | `cases:read_public`, `badges:read_public`, `badges:submit` (site public : lecture, demandes et signalements de badges) |
| `service` | `Authorization: Bearer <token>` | scopes du client de service |
| `staff` | token de service + `X-Acting-Discord-Id: <id>` | **(permissions du membre, ou toutes pour un administrateur) ∩ scopes du service** |
| `system` | processus internes (hangar, CLI) | tout |
| `installation` | extension Chromium : en-tête `X-Installation-Token` (aléatoire, jamais stocké) | `content:ingest`, `badges:read_public`, `badges:submit` ; quota quotidien et limite par IP (voir [ingestion.md](ingestion.md)) |
| `organization` | organisation badgée : en-tête `X-Organization-Token: t12o_…` (empreinte stockée dans `badge_org_keys`), jamais combiné à un autre identifiant | permissions publiques + `badges:org_manage` (ses affiliations), tant que la clé n'est pas révoquée et que son badge est actif (voir [badges.md](badges.md)) |

Le bot (intercom) s'authentifie avec son token et transmet l'ID Discord de la
personne qui agit. L'ID est contrôlé contre `staff_members` (actif, rôles et permissions
individuelles), sauf pour un administrateur codé en dur (voir ci-dessous).
L'intersection garantit qu'un bot compromis ne peut pas dépasser ses scopes, et qu'un
membre ne peut pas dépasser ses rôles.

## Rôles

| Rôle | Permissions |
|---|---|
| `reviewer` | lire, créer, prendre en revue ; lire la liste de suivi ; lire les badges |
| `editor` | + valider, rejeter, marquer comme publié ; modifier la liste de suivi ; consulter l'usage et les budgets ; demander un fact-check manuel ; gérer les badges |
| `admin` | tout (gestion de l'équipe, erreurs) |

## Permissions

Les anciens noms `dossiers:*` stockés sur un jeton ou un membre restent acceptés (transition du renommage, `to_permission`).
`cases:read_public`, `cases:read`, `cases:create`, `cases:review`,
`cases:decide`, `cases:publish`, `events:read`, `staff:read`, `staff:manage`,
`permissions:manage_others`, `permissions:manage_self`,
`errors:read`, `errors:manage`, `content:ingest` (envoi de contenus : extension,
radar), `watchlist:read`, `watchlist:write` (liste des comptes suivis), `usage:read`
(allocation gratuite et budgets du mois : `GET /v1/llm/usage`, commande `/usage`),
`factcheck:request` (demande de fact-check manuelle N4 : `POST /v1/factcheck/requests`,
commande `/factcheck`). Lot 4 : `claim_groups:manage` (fusion et scission, éditeur),
`reference_facts:write` (création d'une fiche, relecteur et éditeur),
`reference_facts:validate` (validation des fiches et des bandes, éditeur),
`profiles:read` (relecteur et éditeur), `profiles:manage` (activation et retour arrière
des profils : **admin** seulement), `levels:override` (forçage de niveau, éditeur).
Badges ([badges.md](badges.md)) : `badges:read_public` et `badges:submit` (public,
non attribuables), `badges:read` (relecteur et éditeur), `badges:manage` (éditeur),
`badges:org_manage` (acteur `organization` seulement, non attribuable).

Brouillons de fact-check (voir [fact-check.md](fact-check.md#revue-e11-et-publication)) :
lecture avec `cases:read` ; `accept` et `edit` avec `cases:review` enregistrent un
avis favorable (`endorsed`), avec `cases:decide` ils valident le verdict ; `reject`
exige `cases:decide`. La lecture publique d'un verdict validé
(`GET /v1/content/{platform}/{external_id}/verdict`) relève de `cases:read_public`
(accordée aux anonymes).

Elles sont vérifiées **dans les cas d'usage**, pas seulement dans l'API.

## Équipe : rangs, administrateurs et permissions individuelles

Sources : `core/usecases/staff.py`, `core/domain/security.py`, `gate/routes/staff.py`.

- **Administrateurs codés en dur** : `TOWER_ADMIN_DISCORD_IDS` (IDs Discord séparés par
  des virgules). Ils ont **toutes** les permissions (∩ scopes du service), sans ligne dans
  `staff_members`. Ils ne peuvent être ni classés, ni déclassés, ni modifiés (`TWR-STF-003`).
- **Membre classé** : ligne `staff_members` active. Seuls les membres classés peuvent
  recevoir des permissions (`TWR-STF-005`). `rank` crée ou réactive ; `unrank` désactive
  et vide `roles` et `permissions`.
- **Permissions individuelles** : colonne `staff_members.permissions`. Droits du membre =
  permissions des `roles` (préréglages de la CLI `tower staff`) ∪ `permissions`. À la
  première modification, les permissions des rôles sont **matérialisées** dans
  `permissions` et `roles` est vidé : la liste explicite devient la seule source.
- **Permissions attribuables** : toutes sauf `cases:read_public`, `content:ingest`,
  `events:read`, `badges:read_public`, `badges:submit` et `badges:org_manage` (propres
  au public, aux services et aux organisations).
- **Qui peut quoi** : `staff:read` (lister, lire un membre ; soi-même sans permission),
  `staff:manage` (classer, déclasser ; jamais soi-même, `TWR-STF-004`),
  `permissions:manage_others` (modifier les permissions d'un autre membre),
  `permissions:manage_self` (modifier les siennes). Chaque action est vérifiée par gate.
- **Anti-escalade** : un non-administrateur ne peut ajouter ou retirer que des permissions
  qu'il détient lui-même (`TWR-STF-006`). Les administrateurs détiennent tout.
- **Hiérarchie Discord** (côté bot, pour les non-administrateurs) : on n'agit que sur un
  membre dont le rôle le plus élevé est strictement inférieur au sien ; personne sauf un
  administrateur n'agit sur un administrateur.
- Le cache du résolveur est invalidé pour le membre visé après chaque écriture
  (`resolver.forget`), sinon au plus tard après `TOWER_AUTH_CACHE_TTL`.

Erreurs : `TWR-STF-001` membre non classé (404), `002` déjà classé (409), `003`
administrateur protégé (409), `004` auto-classement (403), `005` cible non classée (409),
`006` escalade (403), `007` permission inconnue ou non attribuable (422).
Événements : `staff.ranked`, `staff.unranked`, `staff.permissions_changed`
(voir [events.md](events.md)). Endpoints : [api.md](api.md#équipe).

## Tokens de service

```bash
railway run tower create-service-token intercom --scopes cases:read cases:create cases:review cases:decide cases:publish events:read
```

```bash
railway run tower staff 123456789012345678 --roles editor --name "Prénom"
```

- Format `t12a_<43 caractères>` ; seule l'empreinte SHA-256 est stockée ; le token
  n'est affiché qu'une fois.
- Révocation : `UPDATE service_clients SET revoked_at = now() WHERE name = '…'`.
  Effective au plus tard après `TOWER_AUTH_CACHE_TTL` (15 s).

## Erreurs

`TWR-AUTH-001` token invalide ou révoqué (401), `TWR-AUTH-002` ID Discord sans token
(401), `TWR-AUTH-003` ID Discord invalide (401), `TWR-AUTH-004` membre inconnu ou
inactif (403), `TWR-AUTH-005` permission manquante (403). Clés d'organisation :
`TWR-BDG-016` clé invalide ou révoquée (401), `TWR-BDG-017` combinée à un autre
identifiant (401).
