# Authentification et autorisations

Sources : `core/domain/security.py`, `core/auth/`, `gate/deps.py`.

## Acteurs

| Type | Comment | Droits |
|---|---|---|
| `anonymous` | aucune en-tête | `dossiers:read_public` (site public, lecture seule) |
| `service` | `Authorization: Bearer <token>` | scopes du client de service |
| `staff` | token de service + `X-Acting-Discord-Id: <id>` | **rôles du membre ∩ scopes du service** |
| `system` | processus internes (hangar, CLI) | tout |
| `installation` | extension Chromium : en-tête `X-Installation-Token` (aléatoire, jamais stocké) | `content:ingest` seulement ; quota quotidien et limite par IP (voir [ingestion.md](ingestion.md)) |

Le bot (intercom) s'authentifie avec son token et transmet l'ID Discord de la
personne qui agit. L'ID est contrôlé contre `staff_members` (actif et rôles).
L'intersection garantit qu'un bot compromis ne peut pas dépasser ses scopes, et qu'un
membre ne peut pas dépasser ses rôles.

## Rôles

| Rôle | Permissions |
|---|---|
| `reviewer` | lire, créer, prendre en revue ; lire la liste de suivi |
| `editor` | + valider, rejeter, marquer comme publié ; modifier la liste de suivi ; consulter l'usage et les budgets ; demander un fact-check manuel |
| `admin` | tout (gestion de l'équipe, erreurs) |

## Permissions

`dossiers:read_public`, `dossiers:read`, `dossiers:create`, `dossiers:review`,
`dossiers:decide`, `dossiers:publish`, `events:read`, `staff:manage`,
`errors:read`, `errors:manage`, `content:ingest` (envoi de contenus : extension,
radar), `watchlist:read`, `watchlist:write` (liste des comptes suivis), `usage:read`
(allocation gratuite et budgets du mois : `GET /v1/llm/usage`, commande `/usage`),
`factcheck:request` (demande de fact-check manuelle N4 : `POST /v1/factcheck/requests`,
commande `/factcheck`). Lot 4 : `claim_groups:manage` (fusion et scission, éditeur),
`reference_facts:write` (création d'une fiche, relecteur et éditeur),
`reference_facts:validate` (validation des fiches et des bandes, éditeur),
`profiles:read` (relecteur et éditeur), `profiles:manage` (activation et retour arrière
des profils : **admin** seulement), `levels:override` (forçage de niveau, éditeur).

Brouillons de fact-check (voir [fact-check.md](fact-check.md#revue-e11-et-publication)) :
lecture avec `dossiers:read` ; `accept` et `edit` avec `dossiers:review` enregistrent un
avis favorable (`endorsed`), avec `dossiers:decide` ils valident le verdict ; `reject`
exige `dossiers:decide`. La lecture publique d'un verdict validé
(`GET /v1/content/{platform}/{external_id}/verdict`) relève de `dossiers:read_public`
(accordée aux anonymes).

Elles sont vérifiées **dans les cas d'usage**, pas seulement dans l'API.

## Tokens de service

```bash
railway run tower create-service-token intercom --scopes dossiers:read dossiers:create dossiers:review dossiers:decide dossiers:publish events:read
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
inactif (403), `TWR-AUTH-005` permission manquante (403).
