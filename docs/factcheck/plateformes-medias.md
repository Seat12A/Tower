# Plateformes multiples et médias (images, vidéos)

D'autres plateformes que X seront intégrées, puis les images, vidéos et autres médias
entreront dans la détection. Le plan les prévoit **dès le modèle de données et les
ports**, même si leur livraison vient plus tard ([feuille-de-route.md](feuille-de-route.md)).
Règle : le pipeline de fact-check ne connaît ni X ni un format de média ; il travaille
sur des **contenus** et des **affirmations**. Tout ce qui est propre à une plateforme
ou à un média vit dans un adaptateur.

## Modèle générique de contenu

`content_items` remplace l'idée de `observed_posts` limitée à X (voir
[donnees.md](donnees.md)) :

| Champ | Rôle |
|---|---|
| `platform` | `x`, `bluesky`, `threads`, `mastodon`, `youtube`, `tiktok`, `telegram`, `web` (CHECK) |
| `external_id` | Identifiant natif en `TEXT` (les formats diffèrent : numérique pour X, `at://` pour Bluesky, chaîne pour YouTube) |
| (index) | Index sur expression `(external_id::bigint) WHERE platform = 'x'` et contrainte de format (chiffres, borne `BIGINT`) pour X : respecte la règle « IDs X en BIGINT » sans colonne générée (Fable, Q7 : une colonne générée ne sert que si l'on trie par identifiant numérique, et changer son expression réécrit la table) |
| `author_ref` | Référence vers `platform_accounts` (compte, plateforme, rôle public éventuel) |
| `text`, `lang`, `created_at` | Contenu normalisé |
| `parent_ref`, `quoted_ref` | Contexte du fil (réponse, citation, republication) |
| `media` | Liens vers `media_assets` |
| `metrics` | Dernières métriques connues et date de relevé |

Clé primaire : identifiant interne `BIGINT`, référencé par toutes les autres tables ;
l'identifiant externe ne sert qu'aux frontières (API, plateformes). La table n'est
**pas partitionnée** (environ 7 M de lignes par an : une partition imposerait la clé
de partition dans toute clé unique et toute clé étrangère) ; c'est `observations` qui
l'est. Dans l'API et les événements, l'identifiant d'un contenu est toujours une
chaîne, accompagnée de la plateforme.

## Port `PlatformClient`

Généralise l'actuel `XClient` :

```python
class PlatformClient(Protocol):
    platform: str
    cost_per_read_usd_micros: int

    async def get_item(self, external_id: str) -> ContentItem | None: ...
    async def get_thread_context(self, item: ContentItem) -> ThreadContext: ...
    async def get_metrics(self, external_id: str) -> Metrics | None: ...
    async def get_account(self, account_ref: str) -> PublicAccount | None: ...
    async def poll_account(self, account_ref: str, *, since: str | None) -> Sequence[ContentItem]: ...
```

Les méthodes non prises en charge par une plateforme lèvent une erreur typée ; le
pipeline s'adapte (par exemple, pas de métriques de vues sur certaines plateformes :
la portée se calcule alors sur les signaux disponibles).

## Plateformes prévues

| Plateforme | Accès (état relevé le 2026-11-10) | Coût | Usage prévu | Ordre |
|---|---|---|---|---|
| **X** | API officielle au paiement à l'usage : environ 0,005 $ par lecture de post, 0,010 $ par lecture d'utilisateur (sources secondaires) ; plafond mensuel de lectures (2 ou 3 millions selon les sources) | Payant | Comptes suivis (radar), confirmation avant promotion, revérification d'un post représentatif | Existant |
| **Bluesky** | Jetstream : flux WebSocket JSON public, **sans clé**, de tout le réseau ; pas de filtre par mot-clé côté serveur ; Bluesky prévient que ce n'est pas une API stable à long terme | Gratuit (bande passante) | Détection de vagues, comptes suivis présents sur Bluesky | Premier ajout |
| **Threads** | API officielle ; la recherche par mot-clé sur les posts publics exige l'autorisation `threads_keyword_search` (validation par Meta) | Gratuit sous conditions | Comptes suivis, recherche par mot-clé si approuvée | Ensuite |
| **Mastodon** | API publique par instance | Gratuit | Comptes suivis, instances francophones | Ensuite |
| **YouTube, TikTok, Telegram** | API officielles avec conditions variables (programmes chercheurs, quotas) ; **à étudier** au moment de l'intégration | Variable | Vidéos virales (après le multimodal) | Plus tard |

Exigence de Jules (2026-11-11) : même si les plateformes suivantes ne sont pas encore
choisies, le système doit **déjà les distinguer**. Concrètement, dès le lot 2 :
- chaque contenu, compte, observation, affirmation (via ses contenus), brouillon et
  événement porte sa **plateforme** (`x` pour l'instant) ;
- l'API, le bot et l'extension affichent la plateforme d'origine ;
- les statistiques, les vagues et les budgets peuvent être ventilés par plateforme ;
- ajouter une plateforme ne demande ni migration de schéma lourde ni changement du
  pipeline (voir la fin de cette page).

Interdits du projet, inchangés : aucun scraping, aucun compte automatisé pour lire,
uniquement des API officielles ou des flux publics prévus pour cet usage.

Pour Bluesky, un processus léger `radar-bluesky` (ou un mode de `radar`) consomme
Jetstream, filtre localement par mots-clés et comptes suivis (filtre en mémoire borné,
mis à jour depuis la base) et n'insère que les posts retenus. Une coupure du flux se
reprend au curseur (horodatage) enregistré en base.

## Comptes multi-plateformes

Une même personnalité a souvent des comptes sur plusieurs plateformes.
`watched_accounts` pointe vers une **entité** (personne ou organisation, rôle public)
qui possède plusieurs `platform_accounts`. Le niveau N3 s'applique à tous ses comptes.
Le rattachement d'un compte à une entité est une décision humaine journalisée.

## Médias : architecture prévue

### Table `media_assets`

| Champ | Rôle |
|---|---|
| `kind` | `image`, `video`, `audio`, `document` (CHECK) |
| `sha256` | Empreinte exacte du fichier |
| `phash` | Empreinte perceptuelle (image, ou images clés d'une vidéo) en `BIGINT`, indexée par bandes comme le SimHash. Calculée **dès l'ingestion** (lot 2), même avant la lecture d'images : les notes de contexte rattachées à un média en dépendent ([contexte.md](contexte.md)) |
| `duration_s`, `width`, `height` | Métadonnées |
| `first_seen_at`, `first_seen_ref` | Première apparition connue dans notre base |
| `ocr_text`, `transcript`, `description` | Texte extrait (M1) |
| `provenance` | C2PA lu, EXIF, résultats de recherche d'image inversée |
| `storage_ref` | Fichier conservé ou non (par défaut : non, seulement empreintes et extraits) |

Une affirmation porte `source_modality` (texte, image, vidéo, audio) et, si besoin,
`media_asset_id` : une affirmation tirée d'une image entre ensuite dans le pipeline
texte normal.

### Étages médias

```
M0 Empreintes ─▶ M1 Extraction ─▶ M2 Provenance ─▶ (affirmations texte) ─▶ E3…
```

| Étage | Traitement | Coût |
|---|---|---|
| **M0 Empreintes** | Téléchargement en flux (plafond de taille), SHA-256, empreinte perceptuelle (PDQ, algorithme ouvert de Meta, ou pHash) ; pour une vidéo, quelques images clés extraites par `ffmpeg` en sous-processus. Recherche des médias déjà vus (distance de Hamming) : un média connu hérite de son analyse. | Quasi nul |
| **M1 Extraction** | Texte dans l'image (OCR par un petit modèle à entrée image), transcription audio (environ 0,003 $ par minute avec `gpt-4o-mini-transcribe`, à confirmer), description factuelle courte | Faible, borné par niveau |
| **M2 Provenance** | Première apparition connue (base interne, puis recherche d'image inversée par API externe au niveau N2+), lecture des métadonnées C2PA et EXIF | Payant seulement N2+ |

Ce que la recherche indique, et que le plan applique :

- la forme la plus fréquente de désinformation visuelle est le **média authentique
  sorti de son contexte** (image recyclée, légende fausse), plus que le média fabriqué.
  La parade principale est donc la **date de première apparition**, pas un détecteur ;
- les empreintes perceptuelles résistent au réencodage et au redimensionnement, mais
  mal au recadrage fort : une seconde passe par embedding d'image peut être ajoutée
  plus tard pour les cas difficiles ;
- l'**absence** de métadonnées C2PA ne prouve rien (elles sont perdues au premier
  réencodage par une plateforme) ;
- les détecteurs de deepfakes ne sont **jamais** une base de verdict seuls : au mieux
  un indice transmis au relecteur.

### RAM et médias

- Service `media` sur un réplica dédié, une ou deux tâches simultanées.
- Fichiers traités en flux et supprimés après extraction ; images clés en mémoire
  une par une ; aucune vidéo chargée entièrement.
- Aucun modèle de vision local : OCR et description par API.
- `ffmpeg` dans l'image Docker seulement si le service `media` est livré (image
  séparée ou étape conditionnelle, à décider à ce moment-là).

## Ce qui change dans les autres pages quand on ajoute une plateforme

Rien dans le pipeline. Il faut : un adaptateur `PlatformClient`, éventuellement un
processus d'ingestion (flux), une valeur dans le CHECK de `platform`, la documentation
de ses limites, et des cas de test. C'est la mesure de « scalable dans toutes les
directions ».
