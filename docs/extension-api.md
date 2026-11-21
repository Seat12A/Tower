# API de l'extension Chromium

Référence complète et autonome de **tout ce que l'extension appelle** sur gate : ce
qu'elle envoie, ce qu'elle reçoit. Vérifiée contre le code (`gate/routes/ingest.py`,
`gate/routes/badges.py`, `gate/routes/factcheck.py`, `gate/schemas.py`). Détails de
fond : [ingestion.md](ingestion.md), [badges.md](badges.md), [auth.md](auth.md).

## Généralités

- **Base** : URL publique de gate (`https://<gate>`), toutes les routes sous `/v1`.
- **JSON** UTF-8 en entrée (`Content-Type: application/json`) et en sortie.
- **Identifiants** (posts, comptes) : toujours des **chaînes** (`"1856000000000000000"`),
  jamais des nombres. Sur X : chiffres uniquement, 19 au plus. Jamais de pseudo à la
  place d'un identifiant.
- **Dates** : ISO 8601 avec fuseau (`"2026-11-11T08:00:00Z"`).
- **CORS** : origines autorisées par `TOWER_CORS_ORIGINS` (y mettre
  `chrome-extension://<id>`). Méthodes `GET`, `POST`. En-têtes autorisés :
  `Authorization`, `Content-Type`, `X-Installation-Token`, `X-Organization-Token`,
  `If-None-Match`. En-tête exposé : `ETag`.
- **`X-Request-ID`** : facultatif en entrée, toujours renvoyé en sortie.

### Jeton d'installation

En-tête `X-Installation-Token: <jeton>`, **obligatoire seulement pour l'envoi de
posts** (`POST /v1/ingest/observations`). Les autres routes sont publiques (sans
en-tête d'authentification).

- Généré **une fois** par l'extension, aléatoire, stocké localement.
- Format : alphabet base64url `A-Z a-z 0-9 - _`, sans `=`, **22 à 64 caractères**
  (regex `^[A-Za-z0-9_-]{22,64}$`). Recommandé : 32 octets aléatoires en base64url
  (43 caractères).
- Jamais stocké ni journalisé par Tower (seul un pseudonyme du jour, non reliable d'un
  jour à l'autre). Invalide → `401 TWR-ING-001`.

### Format des erreurs (toutes les routes)

```json
{"error": {"code": "TWR-ING-003", "message": "Too many items in the batch",
           "details": {}, "request_id": "9f2c…"}}
```

- Se fier à `code` (stable), pas à `message`. Catalogue : [errors.md](errors.md) ou
  `GET /v1/errors/catalog`.
- Validation de schéma : `422 TWR-GEN-001`, `details.errors` liste les champs fautifs.
- Champ inconnu dans un corps JSON : refusé (`422 TWR-GEN-001`) sur toutes les routes
  POST ci-dessous.

### Récapitulatif

| Méthode | Chemin | Auth | Usage |
|---|---|---|---|
| GET | `/v1/ingest/config` | aucune | Filtres pilotés par le serveur (quels posts envoyer) |
| POST | `/v1/ingest/observations` | `X-Installation-Token` | Envoi des posts filtrés |
| GET | `/v1/content/{platform}/{external_id}/verdict` | aucune | Verdict publié d'un post |
| GET | `/v1/badges/categories` | aucune | Catégories, couleurs, priorité |
| GET | `/v1/badges/lookup` | aucune | Badges de jusqu'à 100 comptes visibles |
| GET | `/v1/badges/accounts/{platform}/{external_id}` | aucune | Détail d'un compte (au clic) |
| POST | `/v1/badges/reports` | aucune | Signaler un badge erroné |
| POST | `/v1/badges/x-government-hints` | aucune | Comptes vus avec la coche gouvernementale X |

---

## 1. `GET /v1/ingest/config` — filtres

**Envoi** : aucun corps. En-tête facultatif `If-None-Match: <ETag reçu précédemment>`.

**Réception `200`** :

```json
{
  "version": "2026-11-11.1",
  "filters": {
    "keywords": ["vaccin", "élection"],
    "accounts": [{"platform": "x", "external_id": "44196397"}],
    "thresholds": {"min_views": 1000}
  }
}
```

- `version` : chaîne ou `null` (aucune version active ; `filters` vaut alors `{}`).
- `filters.keywords` : ≤ 500 chaînes de ≤ 100 caractères ; `filters.accounts` : ≤ 2 000
  comptes ; `filters.thresholds` : objet `nom → nombre` (noms libres, définis par
  l'équipe). Chaque clé peut être absente : la traiter comme vide.
- En-têtes : `ETag: W/"<16 hex>"`, `Cache-Control: public, max-age=300`.

**`304`** (corps vide) si `If-None-Match` correspond : garder la config en cache.

Conseil : interroger au plus toutes les 5 minutes, et renvoyer `version` dans
`filter_version` à chaque envoi.

---

## 2. `POST /v1/ingest/observations` — envoi de posts

**En-têtes** : `X-Installation-Token` (obligatoire), `Content-Type: application/json`.
**Corps ≤ 256 Ko** (sinon `413 TWR-ING-004`).

```json
{
  "filter_version": "2026-11-11.1",
  "items": [
    {
      "platform": "x",
      "external_id": "1856000000000000000",
      "text": "Texte du post tel qu'affiché",
      "author": {"external_id": "44196397", "handle": "example"},
      "lang_hint": "fr",
      "created_at": "2026-11-11T08:00:00Z",
      "metrics": {"views": 1200, "likes": 40, "reposts": 3, "replies": 5},
      "parent_external_id": null,
      "quoted_external_id": null,
      "media": [{"kind": "image", "url": "https://pbs.twimg.com/media/…"}],
      "observed_at": "2026-11-11T08:05:00Z"
    }
  ]
}
```

| Champ | Type | Obligatoire | Règle |
|---|---|---|---|
| `filter_version` | chaîne ≤ 64 / `null` | non | `version` de la config utilisée |
| `source` | — | non | **Ne pas envoyer** : ignoré pour l'extension (toujours `extension`) |
| `items` | tableau | **oui** | 1 à `TOWER_INGEST_MAX_ITEMS` (50) éléments, sinon `422 TWR-ING-003` |
| `items[].platform` | chaîne ≤ 32 | **oui** | `x` (seule plateforme alimentée ; aussi acceptées : `bluesky`, `threads`, `mastodon`, `youtube`, `tiktok`, `telegram`, `web`) |
| `items[].external_id` | chaîne ≤ 512 | **oui** | Id natif du post (X : chiffres, ≤ 19) |
| `items[].text` | chaîne | **oui** | ≤ 20 000 caractères, texte brut tel qu'affiché |
| `items[].author` | objet / `null` | non | `{external_id?, handle?}` : id natif (chaîne) et pseudo sans `@` |
| `items[].lang_hint` | chaîne ≤ 32 / `null` | non | Attribut `lang` du post (ex. `fr`) |
| `items[].created_at` | date / `null` | non | Date de publication du post |
| `items[].metrics` | objet | non | ≤ 32 clés (≤ 32 car.) → entiers 0 à 2⁶² ; ex. `views`, `likes`, `reposts`, `replies`, `quotes`, `bookmarks` |
| `items[].parent_external_id` | chaîne / `null` | non | Post auquel il répond |
| `items[].quoted_external_id` | chaîne / `null` | non | Post cité |
| `items[].media` | tableau | non | `{kind, url}`, `kind` ∈ `image`, `video`, `audio`, `document` |
| `items[].observed_at` | date / `null` | non | Moment où l'extension l'a vu ; hors des 24 dernières heures (+5 min d'avance) → remplacé par l'heure serveur |

Ne rien envoyer sur l'utilisateur (ni compte connecté, ni historique, ni URL de page).

**Réception `202`** :

```json
{"accepted": 49, "new": 12, "duplicates": 37, "rejected": 1,
 "errors": [{"index": 7, "code": "TWR-ING-006"}]}
```

- `accepted` = `new` + `duplicates` ; `rejected` = nombre d'éléments refusés.
- `errors[].index` : position (base 0) dans `items` ; `code` : `TWR-ING-005`
  (plateforme inconnue), `TWR-ING-006` (id invalide : post, auteur, parent ou citation),
  `TWR-GEN-001` (texte trop long, média invalide). **Un élément refusé n'invalide pas le
  lot.** Ne pas renvoyer un élément refusé tel quel.

**Erreurs du lot entier** :

| Statut | Code | Que faire |
|---|---|---|
| 401 | `TWR-ING-001` | Jeton mal formé : régénérer un jeton valide |
| 413 | `TWR-ING-004` | Corps > 256 Ko : découper le lot |
| 422 | `TWR-ING-003` | Plus de 50 éléments : découper |
| 422 | `TWR-GEN-001` | Corps non conforme (champ manquant, inconnu, mauvais type) |
| 429 | `TWR-ING-002` | Quota quotidien atteint (500 éléments acceptés / installation / jour UTC) : arrêter jusqu'à 00:00 UTC |
| 429 | `TWR-ING-007` | Trop de requêtes depuis cette IP (rafale de 20, puis 1 toutes les 5 s) : attendre et regrouper |
| 503 | `TWR-ING-010` | Incident serveur : réessayer plus tard (backoff) |

Conseil : regrouper les posts (lots de 50 au plus, toutes les 10 à 30 s), ne pas
renvoyer un même post à chaque défilement.

---

## 3. `GET /v1/content/{platform}/{external_id}/verdict` — verdict d'un post

**Envoi** : chemin, ex. `/v1/content/x/1856000000000000000/verdict`. Paramètre
facultatif `?lang=fr` (≤ 16 car.) : langue souhaitée des notes (repli : langue du post,
puis anglais, puis français).

**Réception `200`** (seuls les verdicts **validés et publiés**, jamais les brouillons) :

```json
{
  "platform": "x",
  "external_id": "1856000000000000000",
  "url": "https://x.com/i/status/1856000000000000000",
  "lang": "fr",
  "claims": [
    {
      "text": "Affirmation extraite du post",
      "lang": "fr",
      "veracity": "misleading",
      "context_need": "necessary",
      "context_types": ["cherry_picking", "missing_baseline"],
      "validated_at": "2026-11-12T10:00:00Z",
      "note": {
        "lang": "fr",
        "text": "Note de contexte…",
        "fact_sheet": "Fiche détaillée… ou null",
        "machine_translated": false,
        "links": ["https://source.example/…"]
      },
      "inherited": {
        "rule": "identical_signature",
        "origin_verdict_id": "…",
        "origin_url": "https://x.com/i/status/…"
      }
    }
  ]
}
```

| Champ | Valeurs |
|---|---|
| `url` | chaîne ou `null` |
| `claims[].veracity` | `true`, `mostly_true`, `misleading`, `false`, `unverifiable` |
| `claims[].context_need` | `none`, `useful`, `necessary` |
| `claims[].context_types` | parmi `outdated`, `recycled_media`, `manipulated_media`, `cherry_picking`, `missing_baseline`, `quote_integrity`, `misrepresented_source`, `causation`, `definition`, `scope`, `procedure`, `scientific_uncertainty`, `satire`, `missing_event_context`, `developing` |
| `claims[].validated_at` | date ou `null` |
| `claims[].note` | objet ou `null` (pas de note) ; `fact_sheet` peut être `null` ; `machine_translated: true` → afficher la mention « traduction automatique » |
| `claims[].inherited` | absent/`null` si le verdict porte sur ce post ; sinon le verdict vient d'un autre post (`rule` : `identical_signature` ou `band`) |

En-tête : `Cache-Control: public, max-age=300`.

**Erreur** : `404 TWR-FCK-013` = aucun verdict publié pour ce post (cas normal : ne rien
afficher). Id invalide : `422`.

---

## 4. Badges

### 4.1 `GET /v1/badges/categories`

**Envoi** : rien. **Réception `200`** (statique, `Cache-Control: public, max-age=86400`) :

```json
[
  {"category": "institutional", "color": "#F46600", "color_dark": "#FF6A00", "rank": 1},
  {"category": "media",         "color": "#EA00FF", "color_dark": "#EE33FF", "rank": 2},
  {"category": "expert",        "color": "#16A34A", "color_dark": "#16A34A", "rank": 3},
  {"category": "organization",  "color": "#E30B5C", "color_dark": "#E30B5C", "rank": 4},
  {"category": "verified",      "color": "#335CFF", "color_dark": "#6D8AFF", "rank": 5}
]
```

`rank` 1 = priorité la plus haute. `color` = thème clair de X, `color_dark` = thèmes
sombre et Dim (choisir selon le thème de la page) ; les deux accompagnent `color`
partout dans l'API (lookup, détail). `verified` remplace l'ancienne catégorie
`personality` (2026-11-21) : gate ne renvoie plus que `verified` et accepte encore
`personality` en entrée.

### 4.2 `GET /v1/badges/lookup` — badges des comptes affichés

**Envoi** : `?ids=44196397,783214,…&platform=x`

- `ids` : **1 à 100** ids natifs séparés par des virgules (jamais de pseudos). Les
  **trier** améliore le cache partagé.
- `platform` : défaut `x`.
- `If-None-Match` facultatif.

**Réception `200`** :

```json
{
  "platform": "x",
  "accounts": {
    "44196397": {
      "category": "media",
      "color": "#EA00FF",
      "color_dark": "#EE33FF",
      "badge": {"category": "media", "color": "#EA00FF", "color_dark": "#EE33FF", "label": "Média",
                "expertise": null},
      "affiliation": null,
      "affiliations": []
    },
    "783214": {
      "category": null,
      "color": null,
      "color_dark": null,
      "badge": null,
      "affiliation": {
        "org": {"platform": "x", "external_id": "111", "category": "institutional",
                "color": "#F46600", "color_dark": "#FF6A00", "label": "Ministère …",
                "logo_url": "https://…"},
        "title": "Porte-parole"
      },
      "affiliations": [{"org": {"…": "…"}, "title": "Porte-parole"}]
    }
  }
}
```

- **Un id absent de `accounts` n'a pas de badge.**
- **Badge et affiliation sont indépendants.** `category`/`color` = la coche à
  afficher : badge propre du compte seulement ; `null` = pas de coche (un compte
  seulement affilié n'hérite plus de la couleur de son organisation).
- `affiliation` = affiliation principale : afficher le **logo de son organisation**
  (`org.logo_url`) à côté du pseudo, avec ou sans coche. L'équipe choisit la principale.
- `affiliations` : affiliations actives, principale en tête (5 au plus), pour le détail
  au survol ; `[]` si aucune.
- `badge.expertise` : domaine d'un badge `expert` (texte français, ex. « épidémiologie »),
  sinon `null`.
- `badge.label`, `affiliation.title`, `org.label`, `org.logo_url` : chaîne ou `null`.
- En-têtes : `ETag: "<32 hex>"`, `Cache-Control: public, max-age=300`
  (`TOWER_BADGES_LOOKUP_MAX_AGE_S`). `304` sans corps si `If-None-Match` correspond.

**Erreurs** : `422 TWR-BDG-003` (0 ou plus de 100 ids), `422 TWR-ING-005` (plateforme
inconnue), `422 TWR-ING-006` (id invalide, ex. un pseudo).

### 4.3 `GET /v1/badges/accounts/{platform}/{external_id}` — détail (au clic)

**Réception `200`** (`Cache-Control: public, max-age=300`) :

```json
{
  "account": {"platform": "x", "external_id": "783214"},
  "badge": {
    "public_id": "…", "account": {"platform": "x", "external_id": "783214"},
    "status": "active", "category": "expert", "color": "#16A34A", "color_dark": "#16A34A",
    "label": "…", "logo_url": null, "expertise": "épidémiologie"
  },
  "affiliations": [
    {
      "primary": true,
      "title": "Porte-parole",
      "org": {"public_id": "…", "account": {"platform": "x", "external_id": "111"},
              "status": "active", "category": "institutional", "color": "#F46600", "color_dark": "#FF6A00",
              "label": "Ministère …", "logo_url": "https://…", "expertise": null},
      "expires_at": "2027-05-01T00:00:00+00:00"
    }
  ]
}
```

`badge` peut être `null` (compte seulement affilié). **Erreur** : `404 TWR-BDG-001` si ni
badge ni affiliation.

### 4.4 `POST /v1/badges/reports` — signaler un badge erroné

**Envoi** :

```json
{
  "account": {"platform": "x", "external_id": "783214", "handle": "exemple"},
  "reason": "wrong_category",
  "comment": "Facultatif, ≤ 2000 caractères"
}
```

- `account.platform` : défaut `x` ; `external_id` obligatoire ; `handle` (≤ 100)
  informatif seulement.
- `reason` : `wrong_category`, `not_eligible`, `impersonation`, `wrong_affiliation`,
  `other`.

**Réception `201`** : `{"public_id": "…", "status": "open"}`. Rien n'est stocké sur la
personne qui signale.

### 4.5 `POST /v1/badges/x-government-hints` — coches gouvernementales vues

**Envoi** : `{"ids": ["783214", "111"]}` — 1 à 50 ids X (chaînes ≤ 19 chiffres) des
comptes affichés avec la coche grise gouvernementale. Ce ne sont que des **indices** :
Tower vérifie chaque compte auprès de l'API X avant tout badge.

**Réception `202`** : `{"queued": 2}` (nombre de comptes mis en file de vérification ;
`0` si déjà vérifiés récemment).

### Limite des soumissions (4.4 et 4.5, et `POST /v1/badges/requests`)

Par IP : rafale de 10, puis 1 par minute → `429 TWR-BDG-019`. Regrouper les indices et
ne pas les renvoyer pour des comptes déjà envoyés dans la session.

**Erreurs communes des badges** : `422 TWR-GEN-001` (corps invalide), `422 TWR-BDG-010`
(texte ou URL invalide), `422 TWR-ING-005`/`006` (plateforme ou id invalide).
