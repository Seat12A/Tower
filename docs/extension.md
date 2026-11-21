# Extension Chromium : guide d'implémentation

Guide pour construire l'extension de Seat 12A de A à Z, côté client. L'extension est un
client de gate comme les autres : elle ne parle qu'à l'API ([api.md](api.md)), jamais à
la base. Le nom public n'est pas encore choisi (pistes notées dans le résumé de session
du 2026-11-16).

Ce guide décrit le contrat **tel qu'il existe dans Tower aujourd'hui** (endpoints,
formats, limites, codes d'erreur) et recommande une architecture pour le client. Les
recommandations sont marquées comme telles ; les manques côté serveur sont listés dans
[Points ouverts](#points-ouverts).

## 1. Le projet en bref

### Seat 12A

**Seat 12A** est un service francophone de **fact-checking et de modération** centré
sur X. Son identité visuelle reprend le thème de l'aviation (le siège 12A est un siège
côté hublot). Le but : repérer les informations trompeuses qui circulent sur X,
les vérifier avec des sources solides, et montrer le résultat aux lecteurs **là où ils
lisent**, sans faux équilibre (un fait établi n'est jamais présenté comme débattu) et
avec la même règle pour tous les comptes, quel que soit leur bord politique.

L'écosystème compte trois clients, qui passent tous par la même API :

| Client | Pour qui | Rôle |
|---|---|---|
| **Site web** | public | Publie les vérifications (cases), formulaires de demande et de signalement de badges |
| **Extension Chromium** (ce guide) | lecteurs de X | Remonte les posts qui correspondent aux filtres, affiche les vérifications et les badges Seat 12A dans X |
| **Bot Discord interne** (`intercom`) | équipe | Relecture des brouillons de fact-check, décisions, gestion des badges, registres, liste de suivi |

### 12A Tower, le backend

**12A Tower** (ce dépôt) est le cœur : une base de code, une image Docker, plusieurs
processus sur Railway, avec PostgreSQL comme seule infrastructure. Les noms suivent
le thème aviation :

| Processus | Rôle |
|---|---|
| `tower gate` | L'**API** HTTP (FastAPI). C'est le seul interlocuteur de l'extension |
| `tower hangar` | Le **worker** : exécute les jobs des services activés (ingestion, fact-check, badges, `radar`…) |
| `tower intercom` | Le **bot Discord** de l'équipe, client de gate |
| `tower migrate` | Les migrations de la base |

Le **radar** (service du hangar) lit les comptes suivis par l'API officielle de X.
L'extension est l'autre source de surveillance : elle voit ce que les lecteurs voient,
à grande échelle et sans coût d'API, mais elle n'est pas digne de confiance (une
extension modifiée peut mentir), d'où les vérifications côté serveur.

### Le chemin d'un post

1. L'extension voit un post qui passe les filtres et l'envoie (`POST /v1/ingest/observations`).
2. Tower le normalise, le dédoublonne, relie les copies et vérifie le texte et l'auteur
   par l'intégration publique de X (oEmbed) ([ingestion.md](ingestion.md)).
3. Le fact-check classe le post par niveau, en extrait les affirmations vérifiables,
   cherche des sources (registres, statistiques, droit, presse…) et rédige un
   brouillon ([fact-check.md](fact-check.md)).
4. L'équipe relit et valide le brouillon dans Discord. **Rien n'est publié sans
   validation humaine**, et rien n'est jamais publié automatiquement sur X.
5. Le verdict validé devient visible : sur le site, et dans l'extension
   (`GET /v1/content/x/{id}/verdict`), à côté du post et de ses copies.

En parallèle, les **badges Seat 12A** ([badges.md](badges.md)) remplacent les coches de
X par des badges attribués selon la fonction du compte (institution, média, expert,
organisation, vérifié), vérifiés par l'équipe, Wikidata ou l'API X.

## 2. Rôle et limites (non négociables)

L'extension fait trois choses :

1. **Remonter des observations** : elle repère dans X les posts qui correspondent aux
   filtres publiés par le serveur et les envoie à Tower (`POST /v1/ingest/observations`).
   C'est l'une des deux sources de surveillance, avec le radar (API officielle de X sur
   les comptes suivis).
2. **Afficher les vérifications** : à côté d'un post qui a un verdict validé, elle
   affiche l'issue et la note (`GET /v1/content/{platform}/{external_id}/verdict`).
3. **Remplacer les badges de X** : elle masque les coches de X et affiche les badges
   Seat 12A des auteurs (`GET /v1/badges/lookup`).

Règles issues de [decisions.md](decisions.md) et de la règle d'architecture 9 :

- **N'envoyer que les posts filtrés**, avec un payload minimal et **rien sur
  l'utilisateur** (ni pseudo, ni compte connecté, ni historique, ni fil d'actualité
  complet). L'option « tout collecter » est écartée (responsabilité, RGPD, règles du
  Chrome Web Store).
- **Aucune publication sur X**, aucune action au nom de l'utilisateur (pas de like, de
  réponse, de signalement automatique).
- **Aucun scraping** : l'extension lit seulement ce que l'utilisateur affiche déjà à
  l'écran ; elle ne charge pas de pages, ne fait pas défiler le fil, n'appelle pas les
  API internes de X.
- Le serveur **ne croit pas** l'extension : le texte et l'auteur déclarés sont vérifiés
  par l'intégration publique de X (oEmbed) avant de déclencher un fact-check ciblé
  ([ingestion.md](ingestion.md#vérification-des-contenus)). Inutile d'essayer de
  « prouver » quoi que ce soit côté client.
- Le verdict ne dépend jamais de l'auteur (règle 10) : l'affichage non plus. Même
  rendu pour tous les comptes.

## 3. Ce que Tower expose à l'extension

Authentification : en-tête `X-Installation-Token` (permission `content:ingest`), sinon
anonyme (`cases:read_public`). Voir [auth.md](auth.md).

| Endpoint | Auth | Usage |
|---|---|---|
| `GET /v1/ingest/config` | public | Filtres actifs. `ETag` / `If-None-Match` → `304`. `Cache-Control: public, max-age=300` |
| `POST /v1/ingest/observations` | `X-Installation-Token` | Envoi des posts filtrés, réponse `202` |
| `GET /v1/content/{platform}/{external_id}/verdict?lang=` | public | Verdicts validés d'un post. `404` `TWR-FCK-013` s'il n'y en a pas. `Cache-Control: public, max-age=300` |
| `GET /v1/cases`, `GET /v1/cases/{public_id}` | public | Cases publiés (vue réduite), pour un lien « voir sur le site » si besoin |
| `GET /v1/errors/catalog` | public | Catalogue des codes (titres, statut, relançable) |
| `GET /v1/badges/categories` | public | Catégories de badges, couleurs, rang de priorité. `Cache-Control: max-age=86400` |
| `GET /v1/badges/lookup?platform=x&ids=…` | public | Badges et affiliations de 100 comptes au plus. `ETag`, `304`, cache 300 s |
| `GET /v1/badges/accounts/{platform}/{id}` | public | Détail d'un compte (badge et toutes ses affiliations actives). `404` `TWR-BDG-001` s'il n'affiche rien |
| `POST /v1/badges/x-government-hints` | public, limité par IP | Indices de comptes à coche grise X, 50 IDs au plus. `202` |
| `POST /v1/badges/requests`, `POST /v1/badges/reports` | public, limité par IP | Demande de badge ou d'affiliation, signalement d'un badge. `201` |
| `GET /health` | public | Disponibilité de l'API |

Tout le reste de l'API est réservé à l'équipe et aux services : l'extension ne peut pas
l'obtenir (les permissions de l'extension sont non attribuables, et réciproquement).

Format d'erreur, commun à toute l'API :

```json
{"error": {"code": "TWR-ING-002", "message": "...", "details": {},
           "request_id": "...", "fingerprint": "..."}}
```

Toujours décider sur `error.code`, jamais sur `message`.

## 4. Internationalisation (obligatoire)

**L'extension est entièrement internationalisée dès la première version.** C'est une
exigence, pas une option : l'équipe est francophone et anglophone, les réponses du
fact-check suivent la langue du post, et la décision du projet est que tout doit être
traduisible ([decisions.md](decisions.md)).

Règles :

- **Aucun texte en dur** dans le code, le HTML ou le CSS (y compris `aria-label`,
  `title`, `alt`, textes de badge, messages d'état). Tout passe par
  `chrome.i18n.getMessage` et les fichiers `_locales/<langue>/messages.json`.
- **Langues livrées** : `fr` (référence) et `en` au minimum ; `default_locale` du
  manifeste : `en`. Le nom et la description du manifeste sont aussi traduits
  (`"name": "__MSG_extName__"`). Ajouter une langue = ajouter un dossier, sans toucher
  au code.
- **Valeurs de l'API → libellés traduits** : l'API renvoie des valeurs stables
  (`veracity: "false"`, `category: "media"`, `context_need`, codes `TWR-…`), jamais des
  libellés. Chaque valeur a sa clé de message (`veracity_false`, `badge_category_media`,
  `error_TWR_ING_002`…). Une valeur inconnue (ajoutée plus tard côté serveur) affiche un
  libellé générique, jamais la valeur brute.
- **Pluriels, dates et nombres** par `Intl` (`Intl.PluralRules`, `Intl.DateTimeFormat`,
  `Intl.NumberFormat`) dans la langue choisie ; jamais de concaténation de morceaux de
  phrase (utiliser les substitutions `$1` de `messages.json`).
- **Contenu dans sa langue** : chaque texte venu du serveur porte sa langue (`lang` des
  affirmations, `note.lang`) ; l'insérer avec l'attribut `lang` correspondant (et
  `dir="auto"`) pour les lecteurs d'écran et les langues de droite à gauche.
- **Langue choisie par l'utilisateur** : `chrome.i18n` suit la langue du navigateur et
  ne se change pas à l'exécution. Pour respecter le choix fait dans les options, charger
  soi-même le `messages.json` de la langue choisie (petit module de traduction qui
  retombe sur `chrome.i18n` puis sur `en`). La même langue est envoyée dans le
  paramètre `lang` des verdicts.
- **Test** : un test vérifie que toutes les langues ont exactement les mêmes clés, et
  qu'aucune chaîne visible n'est en dur (recherche dans le code source).

## 5. Architecture recommandée (Manifest V3)

Recommandation : TypeScript, Manifest V3, un bundler simple (Vite ou esbuild), dans un
**dépôt séparé** de Tower (autre cycle de publication, autre outillage).

```
extension/
  manifest.json
  src/
    background/   service worker : réseau, file d'envoi, cache, alarmes
    content/      script injecté dans x.com : lecture du DOM, filtrage, affichage
    popup/        fenêtre de l'icône : état, interrupteur, langue
    options/      page d'options : langue, explication vie privée
    shared/       types de l'API, constantes, normalisation
  test/fixtures/  HTML de posts X enregistré pour les tests du parseur
```

Répartition des rôles :

| Partie | Fait | Ne fait pas |
|---|---|---|
| Content script | Observe le DOM, extrait les posts visibles, applique les filtres, envoie les candidats au service worker par `chrome.runtime.sendMessage`, affiche les badges | Aucun appel réseau |
| Service worker | Jeton, config, file d'envoi, appels à gate, cache des verdicts, quotas | Aucun accès au DOM |
| Popup / options | Réglages utilisateur (`chrome.storage`) | Aucun appel réseau direct |

**Pourquoi tout le réseau passe par le service worker.** Depuis Chrome 85, un `fetch`
fait par un content script est soumis au CORS **de la page** (`https://x.com`). Le
service worker, lui, n'est pas soumis au CORS pour les hôtes déclarés dans
`host_permissions`. Il n'y a donc **rien à configurer côté Tower** pour l'extension :
`TOWER_CORS_ORIGINS` sert au site web (voir [configuration.md](configuration.md)).

Le service worker MV3 s'arrête au bout d'environ 30 s d'inactivité : **aucun état en
mémoire n'est durable**. File d'envoi, compteurs et caches vont dans
`chrome.storage.local` (ou `chrome.storage.session` pour ce qui peut se perdre), et les
tâches périodiques passent par `chrome.alarms` (pas de `setInterval`).

### Manifest minimal

```json
{
  "manifest_version": 3,
  "name": "Seat 12A",
  "version": "0.1.0",
  "description": "…",
  "permissions": ["storage", "alarms"],
  "host_permissions": ["https://x.com/*", "https://<domaine-gate>/*"],
  "background": {"service_worker": "background.js", "type": "module"},
  "content_scripts": [{
    "matches": ["https://x.com/*"],
    "js": ["content.js"],
    "css": ["content.css"],
    "run_at": "document_idle"
  }],
  "action": {"default_popup": "popup.html"},
  "options_page": "options.html"
}
```

Ne demander **aucune autre permission** (`tabs`, `history`, `cookies`, `webRequest`,
`<all_urls>`…) : chaque permission en trop alourdit la revue du Chrome Web Store et
contredit la promesse de vie privée. `twitter.com` redirige vers `x.com` : l'ajouter
seulement si un besoin réel apparaît.

## 6. Étapes d'implémentation

### Étape 1 : jeton d'installation

- À l'installation (`chrome.runtime.onInstalled`, raison `install`), générer 32 octets
  aléatoires avec `crypto.getRandomValues`, encoder en **base64url sans remplissage**
  (43 caractères ; le serveur accepte 22 à 64), stocker dans `chrome.storage.local`.
- L'envoyer dans `X-Installation-Token` sur `POST /v1/ingest/observations` seulement.
  Inutile sur les endpoints publics : ne pas l'y envoyer (moins de données liables).
- Le serveur ne stocke jamais le jeton : seulement un pseudonyme du jour
  (`HMAC-SHA256(secret du jour, jeton)`), dont le secret est détruit le lendemain. Le
  quota quotidien est compté sur ce pseudonyme.
- Ne jamais afficher, journaliser ni transmettre le jeton ailleurs.

### Étape 2 : récupérer les filtres

```http
GET /v1/ingest/config
If-None-Match: "<etag précédent>"
```

Réponse `200` :

```json
{"version": "2026-11-11.1",
 "filters": {"keywords": ["…"],
             "accounts": [{"platform": "x", "external_id": "44196397"}],
             "thresholds": {"…": 0}}}
```

- Sans version active : `version` nul et filtres vides → **ne rien envoyer**.
- Interroger au démarrage puis toutes les 15 minutes (`chrome.alarms`, période minimale
  de 30 s en MV3). `304` → garder la config en cache. Stocker `etag`, `version` et
  `filters` dans `chrome.storage.local`.
- En cas d'échec réseau, garder la dernière config connue ; si aucune n'existe, ne rien
  envoyer.
- La version (`version`) est renvoyée telle quelle dans `filter_version` à chaque envoi :
  le serveur s'en sert pour la détection de vagues (gel de 24 h après un changement de
  filtres, voir [factcheck/vagues.md](factcheck/vagues.md)).

Limites du format côté serveur : 500 mots-clés de 100 caractères au plus, 2 000 comptes,
`thresholds` est un objet `{nom: nombre}` libre. Voir les [points ouverts](#points-ouverts)
sur les comptes et les seuils.

### Étape 3 : lire les posts affichés dans X

- Un `MutationObserver` sur le conteneur principal détecte les posts ajoutés au fil
  (défilement, navigation SPA). Traiter les ajouts par paquets (`requestIdleCallback`
  ou un délai court), jamais à chaque mutation.
- Pour chaque post (aujourd'hui `article[data-testid="tweet"]`) :
  - `external_id` : chiffres du lien `/<pseudo>/status/<id>` du post (balise `time`
    entourée d'un lien) ; ignorer le post s'il n'y en a pas ;
  - `author.handle` : le pseudo de ce même lien ;
  - `text` : texte affiché (`[data-testid="tweetText"]`), **sans** le texte des
    traductions proposées par X ;
  - `lang_hint` : attribut `lang` du bloc de texte s'il existe ;
  - `created_at` : attribut `datetime` de la balise `time` ;
  - `metrics` : seulement ce qui est lisible (vues, j'aime, reposts, réponses), en
    entiers ; omettre plutôt que deviner (`1,2 k` → 1200 est acceptable, une valeur
    absente ne l'est pas) ;
  - `quoted_external_id` : identifiant du post cité s'il est affiché ;
  - `parent_external_id` : seulement sur la page d'un post, pour une réponse dont le
    parent est affiché ;
  - `media` : `{kind, url}` avec `kind` parmi `image`, `video`, `audio`, `document`.
- Marquer chaque post traité (attribut `data-seat12a-seen` ou `WeakSet`) pour ne pas le
  relire.
- Ignorer : publicités (« Sponsorisé »), posts protégés (cadenas), et **tout ce qui
  concerne l'utilisateur connecté** (son profil, ses brouillons, ses messages privés,
  ses notifications). Ne lire que `x.com/home`, les pages de profil public, les pages
  de post et la recherche.
- Les sélecteurs de X changent sans préavis : les centraliser dans un seul module, les
  couvrir par des tests sur des fixtures HTML, et **échouer silencieusement** (un post
  non reconnu est ignoré, jamais envoyé à moitié).

### Étape 4 : filtrer localement

Un post n'est envoyé que s'il passe les filtres. Recommandation pour la correspondance
des mots-clés, à aligner sur la normalisation E0 de Tower (`core/domain/text.py`) :
NFKC, minuscules, espaces compactés, puis recherche du mot-clé normalisé (en mot entier
pour un mot seul, en sous-chaîne pour une expression).

- Mots-clés : `filters.keywords`.
- Comptes : `filters.accounts` contient des **identifiants numériques**, que le DOM ne
  montre pas de façon fiable (voir [points ouverts](#points-ouverts)).
- Seuils : `filters.thresholds`, sémantique à définir (voir
  [points ouverts](#points-ouverts)).

### Étape 5 : envoyer par lots

```http
POST /v1/ingest/observations
X-Installation-Token: <jeton>
Content-Type: application/json
```

Corps : voir [ingestion.md](ingestion.md#post-v1ingestobservations). Tous les
identifiants sont des **chaînes** ; `platform` vaut `x` ; ne pas envoyer de `source`
(l'extension est toujours `extension`). Les champs inconnus sont refusés.

File d'envoi (dans `chrome.storage.local`) :

- Dédoublonner localement par `external_id` sur 24 h (ensemble borné, par exemple
  5 000 entrées, les plus anciennes évincées). Un même post revu plus tard peut être
  renvoyé une fois par jour pour mettre à jour les métriques, pas plus.
- Vider la file quand elle atteint **50 items** (`TOWER_INGEST_MAX_ITEMS`) ou toutes les
  60 s, et rester sous **256 Ko** par requête (couper le lot sinon). Texte de
  20 000 caractères au plus par item.
- `observed_at` : heure de l'observation. Le serveur la remplace si elle a plus de 24 h
  ou plus de 5 min d'avance : ne pas garder d'items plus d'une journée.
- Borner la file (par exemple 500 items) : au-delà, jeter les plus anciens. Une charge
  forte ralentit, elle ne déborde pas.

Réponse `202` : `{"accepted", "new", "duplicates", "rejected", "errors": [{index, code}]}`.
Un item refusé l'est seul : le retirer de la file, ne pas le renvoyer.

Erreurs et conduite à tenir :

| Code | HTTP | Conduite |
|---|---|---|
| `TWR-ING-002` | 429 | Quota quotidien (500 items par installation) atteint : suspendre les envois jusqu'à 00:00 UTC, vider la file |
| `TWR-ING-007` | 429 | Trop de requêtes depuis cette IP (rafale de 20, puis une toutes les 5 s) : réessayer après 30 s, puis backoff exponentiel |
| `TWR-ING-003` | 422 | Lot de plus de 50 items : bug client, couper le lot |
| `TWR-ING-004` | 413 | Corps de plus de 256 Ko : bug client, couper le lot |
| `TWR-ING-005`, `TWR-ING-006`, `TWR-GEN-001` | 422 (par item) | Item invalide : le jeter (bug du parseur à corriger) |
| `TWR-ING-010` | 503 | Panne côté serveur, relançable : backoff (1 min, puis jusqu'à 30 min) |
| Réseau, 5xx | — | Backoff exponentiel avec gigue, file conservée |

Ne jamais réessayer en boucle serrée. Ne jamais afficher d'erreur technique à
l'utilisateur pour l'envoi : c'est un travail de fond.

### Étape 6 : afficher les vérifications

```http
GET /v1/content/x/<external_id>/verdict?lang=fr
```

Réponse `200` (schéma `PublicVerdictOut`) :

```json
{"platform": "x", "external_id": "…", "url": "https://…", "lang": "fr",
 "claims": [{"text": "…", "lang": "fr", "veracity": "false",
             "context_need": "necessary", "context_types": ["…"],
             "validated_at": "2026-11-15T10:00:00Z",
             "note": {"lang": "fr", "text": "…", "fact_sheet": "…",
                      "machine_translated": false, "links": ["https://…"]},
             "inherited": null}]}
```

- `404` `TWR-FCK-013` : pas de verdict validé → **ne rien afficher**. C'est le cas
  normal pour la quasi-totalité des posts.
- `veracity` : `true`, `mostly_true`, `misleading`, `false`, `unverifiable`.
  `context_need` : `none`, `useful`, `necessary`. Les libellés affichés sont du ressort
  du client (traduits) ; ne jamais afficher la valeur brute.
- Un post peut porter plusieurs affirmations (`claims`) : afficher un badge résumé et
  le détail par affirmation au clic.
- `note.machine_translated: true` → l'indiquer (« traduction automatique »).
- `inherited` non nul : le verdict couvre ce post par un autre (copie, même chiffre) ;
  proposer le lien `origin_url`.
- `note` peut être nulle (son régime ne permet pas de la montrer) : afficher l'issue
  seule.
- Cache des réponses (y compris les `404`) dans le service worker, 5 minutes, borné.

**Vie privée : quels posts interroger ?** Interroger gate pour chaque post affiché
reviendrait à transmettre au serveur, avec l'adresse IP, la liste de tout ce que
l'utilisateur regarde, ce que la règle « rien sur l'utilisateur » interdit dans
l'esprit. Recommandation pour la V1 : **n'interroger que les posts qui passent les
filtres** (déjà envoyés de toute façon), au plus une fois par post et par période de
cache. L'affichage sur tous les posts demande un endpoint serveur dédié (voir
[points ouverts](#points-ouverts)).

L'affichage s'insère sous le post sans masquer ni modifier son contenu, avec un style
isolé (Shadow DOM ou classes préfixées) pour ne pas casser la page de X ni subir ses
styles.

### Étape 7 : badges Seat 12A

Règles complètes : [badges.md](badges.md). Ce qui concerne l'extension :

**Catégories.** Au démarrage puis une fois par jour, lire `GET /v1/badges/categories` :

```json
[{"category": "institutional", "color": "#F46600", "color_dark": "#FF6A00", "rank": 1},
 {"category": "media", "color": "#EA00FF", "color_dark": "#EE33FF", "rank": 2}, "…"]
```

Cinq catégories aujourd'hui (`institutional`, `media`, `expert`, `organization`,
`verified`, ex-`personality`). Une coche vient du badge propre du compte ; une
affiliation n'affiche que le logo de l'organisation à côté du pseudo (sans coche si le
compte n'a pas de badge). Les couleurs viennent de l'API (ne pas les coder en dur) : `color` pour le thème clair
de X, `color_dark` pour les thèmes sombre et Dim ; les
libellés viennent des fichiers de traduction (section 4).

**Lookup en lot.** Pour les auteurs affichés à l'écran :

```http
GET /v1/badges/lookup?platform=x&ids=12,345,6789
If-None-Match: "<etag>"
```

- **100 identifiants au plus**, **numériques** (jamais de pseudo : un badge est lié à
  l'ID natif). Trier les IDs dans la requête : un même lot donne la même URL et profite
  mieux des caches partagés.
- Regrouper les auteurs découverts pendant environ 300 ms avant d'envoyer un lot ; ne
  jamais faire une requête par auteur.
- Garder un cache local par ID (5 minutes, borné), y compris pour les IDs **absents** :
  un ID absent de `accounts` n'a pas de badge Seat 12A.
- Erreurs : `TWR-ING-006` (ID invalide), `TWR-BDG-003` (lot vide ou de plus de 100).

Réponse : pour chaque ID présent, `category` et `color` sont **ce qu'il faut
afficher** (badge propre du compte, sinon couleur de l'organisation de son affiliation
principale). `badge` décrit le badge propre (`category`, `color`, `label`),
`affiliation` l'affiliation principale (`org` avec `label` et `logo_url`, `title`).

**Affichage.**

- **Masquer la coche de X** de tous les auteurs, badgés ou non (décision du projet :
  les badges Seat 12A remplacent les vérifications de X).
- Afficher le badge Seat 12A à la couleur `color`, à côté du nom, avec une infobulle
  traduite : catégorie, libellé, et pour une affiliation « <titre> · <organisation> »
  avec le logo (`logo_url`, chargé en `https`, taille fixe, texte alternatif traduit).
- Au clic : détail par `GET /v1/badges/accounts/x/{id}`, liens « demander un badge » et
  « signaler ce badge ».
- Même rendu pour tous les comptes d'une catégorie : aucune mise en avant liée au bord
  politique.

**Indices « government ».** Quand l'extension voit la **coche grise** de X (comptes
gouvernementaux), elle envoie l'ID de l'auteur en indice :

```http
POST /v1/badges/x-government-hints
{"ids": ["12", "345"]}
```

- 50 IDs au plus par requête, dédoublonnés localement (un même ID au plus une fois par
  semaine). Réponse `202 {queued}`.
- Un indice **n'est jamais cru** : le serveur vérifie par l'API X avant d'attribuer un
  badge `institutional`. Ne jamais afficher de badge sur la seule foi de la coche grise.
- La couleur de la coche se lit dans le DOM de X (sélecteur à isoler dans le module des
  sélecteurs, comme pour les posts).

**Demandes et signalements.** Formulaires dans l'extension ou renvoi vers le site :

- `POST /v1/badges/requests` : `kind` (`badge` ou `affiliation`), `account`, `category`
  ou `org`, `title`, `justification`, `contact` facultatif (lu par l'équipe seulement),
  `evidence_urls` (https, 10 au plus) ; `account` et `org` valent
  `{"platform": "x", "external_id": "…"}`. Une seule demande en attente par type,
  compte et organisation (`TWR-BDG-013`).
- `POST /v1/badges/reports` : `account`, `reason` (`wrong_category`, `not_eligible`,
  `impersonation`, `wrong_affiliation`, `other`), `comment` facultatif. Rien n'est
  stocké sur l'auteur du signalement ; seul un compte qui affiche un badge ou une
  affiliation peut être signalé.
- Limite par IP pour ces soumissions et les indices : 10 d'affilée puis 1 par minute,
  `429` `TWR-BDG-019` → message traduit « réessayez dans une minute », pas de nouvel
  essai automatique.

**Vie privée.** Le lookup transmet les IDs des **auteurs** affichés (pas les posts ni
l'utilisateur), en `GET` public et cachable. C'est le choix retenu pour les badges ;
le mentionner dans la politique de confidentialité.

### Étape 8 : langue des vérifications

Décision : la langue d'affichage est **au choix de l'utilisateur** dans l'extension
(section 4). Par défaut, `chrome.i18n.getUILanguage()` ramené à sa sous-étiquette
(`fr`, `en`…). L'envoyer dans `lang` ; le serveur choisit la note dans cette langue si
elle existe, sinon dans la langue du post, puis `en`, puis `fr`, et le signale par
`note.lang` (afficher la langue réelle si elle diffère du choix).

### Étape 9 : popup et options

- Popup : état (actif, quota atteint, API injoignable), interrupteur « envoyer les
  observations » (l'affichage des vérifications et des badges peut rester actif
  seul), choix de
  langue, lien vers la page vie privée du site.
- Options : explication claire de ce qui est envoyé (exemple de payload), de ce qui ne
  l'est jamais, et du jeton d'installation, avec un bouton « réinitialiser le jeton »
  (en génère un nouveau).
- Charte : celle de Seat 12A (thème aviation). Aucune charte graphique de l'extension
  n'est documentée dans ce dépôt à ce jour.

### Étape 10 : vie privée et Chrome Web Store

À préparer avant la première soumission (à faire relire par une personne compétente :
ce guide n'est pas un avis juridique) :

- Politique de confidentialité publique (page du site), cohérente avec
  [ingestion.md](ingestion.md#vie-privée) : contenu des posts publics filtrés, jeton
  aléatoire non stocké, aucune IP stockée, aucun profil.
- Formulaire « Pratiques de confidentialité » du tableau de bord : objectif unique,
  justification de chaque permission, déclaration des données transmises.
- Pas de code distant : MV3 interdit d'exécuter du code téléchargé. Les filtres sont
  des données, pas du code.
- Captures d'écran, description en français et en anglais.

### Étape 11 : tests

- **Parseur** : tests unitaires sur des fixtures HTML de posts réels enregistrées
  (post simple, citation, réponse, médias, post long, publicité, post protégé). Les
  réenregistrer quand X change son DOM.
- **Filtres et file** : tests unitaires (normalisation, dédoublonnage, coupe à 50 items
  et 256 Ko, suspension après `TWR-ING-002`).
- **Contrat avec Tower** : contre un gate local :

  ```bash
  uv run tower migrate
  ```

  ```bash
  uv run tower ingest publish-filters filters.json --version dev.1
  ```

  ```bash
  uv run tower gate
  ```

  puis charger l'extension non empaquetée (`chrome://extensions`, mode développeur)
  avec `http://localhost:8000` comme domaine de gate dans une configuration de
  développement. Les types TypeScript peuvent être générés depuis `/openapi.json`
  (disponible hors production).
- **Bout en bout** : Playwright avec un Chromium lancé en contexte persistant et
  `--load-extension`, sur une page de test qui reproduit le DOM de X (ne pas tester
  contre x.com en automatique).

### Étape 12 : publication et versions

- Chrome Web Store, puis Microsoft Edge Add-ons (même paquet Chromium).
- Numéro de version semver dans le manifeste ; publier d'abord en « non répertorié »
  pour l'équipe, puis en public.
- Le serveur reste compatible avec les anciennes versions de l'extension (règle
  expand/contract) : un changement d'API qui casse le contrat garde l'ancien chemin le
  temps que les installations se mettent à jour.

## 7. Liste de contrôle avant publication

- [ ] Seules les permissions `storage`, `alarms` et les deux hôtes sont demandées.
- [ ] Aucun appel réseau depuis le content script.
- [ ] Rien n'est envoyé sans version de filtres active ; seuls les posts filtrés partent.
- [ ] Aucune donnée sur l'utilisateur dans les payloads (vérifié sur des captures réseau).
- [ ] Jeton envoyé seulement sur `POST /v1/ingest/observations`.
- [ ] Lots ≤ 50 items et ≤ 256 Ko ; file et caches bornés.
- [ ] `TWR-ING-002` suspend jusqu'à minuit UTC ; `429` et `5xx` en backoff.
- [ ] `404` sur un verdict n'affiche rien ; traduction automatique signalée.
- [ ] Aucune chaîne visible en dur ; `fr` et `en` complets (mêmes clés) ; langue au
      choix appliquée à l'interface et aux notes ; `lang` posé sur le contenu serveur.
- [ ] Coches X masquées ; badges en lots triés de 100 IDs au plus, `ETag` respecté ;
      indices « government » dédoublonnés ; `TWR-BDG-019` sans nouvel essai automatique.
- [ ] Politique de confidentialité en ligne et formulaire du Store rempli.

## Points ouverts

Manques ou décisions côté Tower relevés en rédigeant ce guide, à trancher avant ou
pendant le développement de l'extension :

1. **Identifiant numérique des auteurs (bloquant pour les badges).** Le lookup des
   badges, les indices « government » et `filters.accounts` exigent l'ID numérique X
   de l'auteur, alors que le DOM de X montre le pseudo et n'expose pas l'ID de façon
   stable (aucun attribut documenté). Il faut trouver une source fiable côté client
   (à étudier sur le DOM réel, sans appeler les API internes de X, interdites par la
   règle « aucun scraping ») ou ajouter côté serveur une correspondance pseudo → ID pour
   les seuls comptes connus (badgés ou suivis), en gardant l'ID comme référence. La
   règle des badges (« jamais par pseudo ») impose que cette correspondance vienne du
   serveur, pas du client.
2. **Sémantique de `thresholds`.** Le format accepte `{nom: nombre}` sans nom défini.
   Il faut fixer la liste des seuils (par exemple un nombre minimal de vues) et leur
   règle d'application, dans `ingestion.md` et dans le validateur.
3. **Affichage sur tous les posts sans fuite de navigation.** Aujourd'hui, seul
   l'endpoint par post existe. Pour afficher les vérifications sur des posts non
   filtrés sans révéler ce que l'utilisateur regarde, il faudrait un endpoint de type
   index (liste compacte des posts vérifiés, ou recherche par préfixe de hachage),
   téléchargé périodiquement.
4. **Limitation de débit des lectures publiques.** Des seaux à jetons par IP protègent
   l'ingestion et les soumissions de badges ; `GET /v1/content/.../verdict` et
   `GET /v1/badges/lookup` n'ont que le cache HTTP de 5 minutes.
   À surveiller quand l'extension aura des utilisateurs.
5. **Nom de l'extension** et domaine de production de gate, à fixer.
