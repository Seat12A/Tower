# Ingestion des contenus

Réception des contenus de toutes les plateformes (extension Chromium aujourd'hui, radar
et flux plus tard), normalisation (étage E0), dédoublonnage, liaison des copies, liste
de suivi. Modèle de données : [factcheck/donnees.md](factcheck/donnees.md) et
[factcheck/plateformes-medias.md](factcheck/plateformes-medias.md) ; tables :
[database.md](database.md).

## Multi-plateforme

La plateforme est portée par chaque contenu, compte, observation et événement.
Valeurs prévues : `x`, `bluesky`, `threads`, `mastodon`, `youtube`, `tiktok`,
`telegram`, `web` (CHECK). Seul X est alimenté aujourd'hui. Identifiant natif en
`TEXT` (`external_id`), toujours exposé en chaîne ; pour X : chiffres uniquement, au
plus 19, borne `BIGINT` (contrainte CHECK) et index sur `(external_id::bigint)`.

## E0 : normalisation et empreintes (domaine pur)

| Fonction | Module | Rôle |
|---|---|---|
| `normalize` | `core/domain/text.py` | NFKC, minuscules, paramètres de suivi retirés des URL (`utm_*`, `fbclid`, `gclid`, `ref_src`…), mentions → `@user`, emojis décoratifs retirés, espaces compactés |
| `useful_word_count` | idem | Mots d'au moins deux lettres, hors URL et mentions |
| `content_hash` | idem | SHA-256 du texte normalisé (copie exacte) |
| `simhash64`, `simhash_bands`, `hamming` | idem | SimHash 64 bits (mots et bigrammes, `blake2b` ; URL et `@user` exclus des traits, pour qu'un ajout de lien ou de mention garde la même empreinte), en entier signé pour `BIGINT` ; 4 bandes de 16 bits |
| `detect_language` | `core/domain/language.py` | Indice de la plateforme s'il est valide, sinon écriture (arabe, cyrillique, hébreu, grec, CJK, dévanagari, thaï…), sinon mots outils (fr, en, es, de, it, pt, nl) ; `und` si incertain |

Le texte d'origine est **conservé tel quel** ; la normalisation ne sert qu'aux
empreintes. Chaque contenu stocke `lang` (sous-étiquette principale BCP 47 ou `und`),
`lang_confidence` et l'indice brut `lang_hint`. Les langues traitées par le fact-check
sont une configuration (`TOWER_FC_LANGUAGES`, `fr,en` par défaut) : l'ingestion
accepte toutes les langues, le filtre s'applique aux étages suivants.

## API

### `POST /v1/ingest/observations`

Authentification : en-tête `X-Installation-Token` (extension) **ou** token de service
portant `content:ingest` (radar, plus tard). Corps (256 Ko au plus, sinon
`TWR-ING-004`) :

```json
{
  "filter_version": "2026-11-11.1",
  "items": [
    {
      "platform": "x",
      "external_id": "1856000000000000000",
      "author": {"external_id": "44196397", "handle": "example"},
      "text": "…",
      "lang_hint": "fr",
      "created_at": "2026-11-11T08:00:00Z",
      "metrics": {"views": 1200, "likes": 40},
      "parent_external_id": null,
      "quoted_external_id": null,
      "media": [{"kind": "image", "url": "https://…"}],
      "observed_at": "2026-11-11T08:05:00Z"
    }
  ]
}
```

- Au plus `TOWER_INGEST_MAX_ITEMS` (50) items, sinon `TWR-ING-003`. Texte de 20 000
  caractères au plus. Les identifiants sont des **chaînes**.
- Un token de service peut préciser `"source": "radar"` ou `"feed"` ; l'extension est
  toujours `extension`.
- `observed_at` est accepté s'il se situe dans les 24 dernières heures (5 minutes
  d'avance tolérées), sinon remplacé par l'heure du serveur.
- Un item invalide (plateforme inconnue `TWR-ING-005`, identifiant invalide
  `TWR-ING-006`, y compris auteur, parent ou citation ; texte trop long ou média
  invalide `TWR-GEN-001`) est **refusé seul** ; le reste du lot est traité. Un même
  contenu présent deux fois dans le lot donne une ligne, deux observations et
  `observation_count` augmenté d'autant.
- Réponse `202` :

```json
{"accepted": 49, "new": 12, "duplicates": 37, "rejected": 1,
 "errors": [{"index": 7, "code": "TWR-ING-006"}]}
```

Traitement, dans une transaction : quota de l'installation, comptes auteurs
(`platform_accounts`), `INSERT … ON CONFLICT (platform, external_id) DO UPDATE` des
contenus (métriques, `last_seen_at`, `observation_count` ; le texte d'origine n'est
remplacé que par une lecture de la plateforme, voir « Vérification des contenus »), médias (`media_assets` dédoublonnés par empreinte de l'URL,
`content_media`), une observation par item accepté, puis pour **chaque nouveau
contenu seulement** l'événement `content.ingested` et le job `ingest.link_copies`.
Aucun événement par observation : une vague produirait des millions de lignes
d'outbox.

### `GET /v1/ingest/config`

Public. Configuration de l'extension pilotée par le serveur : version active et
filtres (`keywords`, `accounts`, `thresholds`). En-tête `ETag` (empreinte du
contenu) ; `If-None-Match` → `304`. `Cache-Control: public, max-age=300`. Sans version
active : `version` nul et filtres vides.

Publication d'une version (une seule active à la fois) :

```bash
uv run tower ingest publish-filters filters.json --version 2026-11-11.1
```

Format : `{"keywords": [str ≤ 100 car.] (≤ 500), "accounts": [{"platform",
"external_id"}] (≤ 2 000), "thresholds": {nom: nombre}}` ; toute autre clé est
refusée (`TWR-ING-011` ; plateforme ou identifiant de compte invalide :
`TWR-ING-005`/`006`) ; une version déjà publiée est refusée (`TWR-GEN-001`). Publie
`extension_filters.published`.

### Liste de suivi : `/v1/watched-accounts`

| Méthode | Chemin | Permission |
|---|---|---|
| GET | `/v1/watched-accounts?active=&platform=&limit=&offset=` | `watchlist:read` |
| POST | `/v1/watched-accounts` `{platform, external_id?, handle?, category, priority?, note?}` | `watchlist:write` |
| PATCH | `/v1/watched-accounts/{public_id}` `{version, category?, priority?, active?, note?, handle?}` | `watchlist:write` |

`category` est une catégorie fermée de rôle public (`head_of_state_or_government`,
`minister`, `member_of_parliament`, `elected_official`, `candidate`,
`political_party`, `public_institution`, `media_outlet`, `journalist`,
`public_figure`, `other`), **jamais un bord politique**. Priorité de -100 à 100.
Doublon → `TWR-ING-009` ; introuvable → `TWR-ING-008` ; version périmée →
`TWR-GEN-003`. `note: ""` efface la note ; un PATCH sans changement ne fait rien (ni
audit, ni événement, ni version). Chaque écriture est auditée (`watched_account_events`) et publie
`watchlist.changed`. Le suivi ne change que la surveillance et la latence, jamais le
verdict (règle 10).

**Ajout par pseudo.** `external_id` ou `handle` est obligatoire (`TWR-ING-006` si les
deux manquent ; sur X, pseudo `^[A-Za-z0-9_]{1,15}$`). Un pseudo déjà connu avec son
identifiant réutilise ce compte ; sinon un compte **en attente** est créé
(`platform_accounts.external_id` nul, un seul par plateforme et pseudo sans casse).
L'événement `watchlist.changed` (`external_id: null`) déclenche le job
`radar.resolve_account`, qui appelle le cas d'usage `resolve_watched_account` : il
renseigne l'identifiant, ou rattache le suivi à un compte déjà connu sous cet
identifiant (le compte en attente est supprimé), audite `resolved` et publie
`watchlist.changed` (`action: resolved`). Rejouer la résolution ne fait rien. Voir
[radar.md](radar.md).

## Quotas et limites

- **Par installation et par jour UTC** : `TOWER_INGEST_DAILY_QUOTA` (500) items
  acceptés, compté en base (`installation_daily`) par une mise à jour conditionnelle
  atomique ; dépassement → `429` `TWR-ING-002`, lot entier refusé.
- **Par adresse IP** : seau à jetons en mémoire (rafale de 20 requêtes, puis une
  toutes les 5 secondes), 10 000 adresses au plus (les plus anciennes évincées),
  jamais persisté ni journalisé → `429` `TWR-ING-007`. Derrière le proxy Railway,
  l'adresse retenue est la dernière entrée de `X-Forwarded-For` (ajoutée par le
  proxy). Limite par réplica : avec N réplicas gate, la limite effective est N fois
  plus large ; le quota en base reste exact.

## Vie privée

- L'extension n'envoie que les posts filtrés, avec un payload minimal et **rien sur
  l'utilisateur**.
- Le jeton d'installation (aléatoire, généré par l'extension, base64url de 22 à 64
  caractères) n'est **jamais stocké** ni journalisé. Seul est stocké le pseudonyme du
  jour : `HMAC-SHA256(secret du jour, jeton)` tronqué à 16 octets.
- `installation_secrets` : la maintenance crée le secret du jour et celui du
  lendemain, et **supprime** ceux des jours passés. Une fois le secret détruit, il
  est impossible de relier les pseudonymes de deux jours différents : aucun profilage
  ni traçage possible.
- Aucune adresse IP n'est stockée.
- **Sauvegardes** : la destruction n'est réelle que si les sauvegardes de la base ont
  une rétention courte (quelques jours). À vérifier dans la configuration des
  sauvegardes Railway.
- `installation_daily` est supprimée au-delà de 30 jours.

## Partitions des observations

`observations` est partitionnée par semaine (lundi 00:00 UTC) sur `observed_at`,
partitions `observations_pAAAAMMJJ`. La migration crée la semaine précédente à deux
semaines d'avance ; le service `ingest` crée ensuite chaque heure les partitions des
deux semaines suivantes. Les partitions dont la fin date de plus de 30 jours sont
agrégées dans `observation_counters` (contenu, heure, source, nombre) puis supprimées,
dans la même transaction. `content_items` n'est **pas** partitionnée.

Une observation sans partition (maintenance arrêtée depuis plus de deux semaines)
échoue avec `TWR-ING-010` (503).

## Vérification des contenus

L'extension est un client **non fiable** : le texte et l'auteur qu'elle déclare pour
un identifiant de post ne sont que des affirmations (une extension modifiée peut
inventer un post et l'attribuer à un compte suivi). `content_items.verification`
(domaine pur : `core/domain/verification.py`) dit d'où vient la confiance :

| Valeur | Sens |
|---|---|
| `platform` | Texte et auteur lus sur la plateforme par un service (`radar`, `manual`, `feed`) |
| `verified` | Déclaré par l'extension et confirmé par l'intégration publique du post (X oEmbed) |
| `unverified` | Déclaré par l'extension, pas (encore) vérifié, ou vérification non concluante |
| `mismatch` | L'intégration publique contredit le texte ou l'auteur déclaré |
| `unavailable` | Pas d'intégration publique : post inexistant, supprimé ou protégé |

Règles :

- **Une lecture de la plateforme fait foi.** Un lot d'une source de service marque ses
  contenus `platform` ; s'il retrouve un contenu `unverified`, `mismatch` ou
  `unavailable`, il remplace son texte, son auteur, sa langue et ses empreintes, puis
  publie `content.verified`. Un texte `platform` ou `verified` n'est jamais remplacé (des
  affirmations en ont été extraites) ; un contenu `verified` passe seulement à
  `platform`, sans événement.
- **Seuls les contenus `platform` ou `verified` déclenchent un fact-check ciblé** (N3
  d'un compte suivi, forçage de compte N3/N4) : voir [fact-check.md](fact-check.md).
  Un post de compte suivi déclaré par l'extension attend sa vérification
  (`content.verified`) ou la lecture du radar.
- Les contenus `mismatch` sont exclus des micro-lots d'extraction N1 (flux de masse).
- Les contenus non vérifiés restent comptés (observations, statistiques, vagues) ;
  pour des statistiques fiables, filtrer sur `verification`.

### Vérification par l'intégration publique (X oEmbed)

`GET https://publish.x.com/oembed?url=https://x.com/i/status/<id>&omit_script=1&dnt=true`
([documentation officielle](https://docs.x.com/x-for-websites/oembed-api)) : sans
authentification, sans quota de l'API X ; la réponse donne l'auteur (`author_url`) et le
texte (premier `<p>` du champ `html`). Usage validé par Jules le 2026-11-14 (voir
[decisions.md](decisions.md)). Adaptateur : `core/adapters/x_oembed.py` (port
`PublicEmbedClient`).

Comparaison (`compare`) : liens retirés des deux côtés (l'intégration montre des liens
`t.co`, la page des URL raccourcies), NFKC, minuscules, pictogrammes retirés, espaces
compactés ; **les mentions sont gardées** (une mention changée est un autre post). Le
pseudo déclaré, s'il existe, doit être celui de `author_url`. Un texte d'intégration
d'au moins 250 caractères qui n'est qu'un début du texte déclaré (post long
possiblement tronqué) est **non concluant** : le contenu reste `unverified`.

Limites, pour ne pas solliciter X plus que nécessaire :

| Limite | Mécanisme | Variable |
|---|---|---|
| Un seul lot à la fois, tous réplicas confondus | Tâche singleton `ingest:verify` (60 s) qui enfile le job `ingest.verify` (`dedupe_key` `oembed:batch`, priorité 10 devant le backlog de copies) | — |
| Requêtes par minute | Taille d'un lot | `TOWER_OEMBED_PER_MINUTE` (30) |
| Requêtes simultanées | Sémaphore dans le lot | `TOWER_OEMBED_CONCURRENCY` (2) |
| Requêtes par jour UTC | Comptées sur `checked_at` ; au-delà, les contenus restent `unverified` | `TOWER_OEMBED_DAILY_MAX` (20 000) |
| Fenêtre | Seuls les contenus vus depuis moins de N heures, chacun **une seule fois** | `TOWER_OEMBED_MAX_AGE_H` (24) |
| Pause sur 429 | Le job est reporté du délai annoncé (60 s à 1 h, 15 min par défaut) ; resté actif, il bloque les lots suivants. `TWR-ING-014` | — |
| Pause sur panne | Si toutes les requêtes d'un lot échouent (réseau, 5xx, 403) : 5 min. `TWR-ING-013` | — |
| Durée d'un lot | Aucune requête ne démarre après 80 s (délai du job : 110 s) | `TOWER_OEMBED_TIMEOUT_S` (10 s par requête) |

Ordre : contenus des comptes suivis d'abord (ils peuvent ouvrir un fact-check ciblé),
puis les plus récents. Aucune transaction ouverte pendant les requêtes. Un 403 n'est
jamais pris pour « post absent » (seul un 404 donne `unavailable`) : un refus d'X ne
doit pas marquer tous les posts. Une divergence est capturée (`TWR-ING-012`,
avertissement stocké). `TOWER_OEMBED_ENABLED=false` coupe la vérification.

## Service `ingest`

`TOWER_SERVICES=ping,ingest`, file `ingest`.

| Élément | Rôle |
|---|---|
| Job `ingest.link_copies` (`dedupe_key` `copies:<id>`) | Copie exacte (même `content_hash`, sans limite de date) ou quasi-copie (SimHash à distance ≤ 3, trouvée par les 4 bandes sur 30 jours) ; `copy_of_id` reçoit le plus ancien contenu (ou sa racine s'il est lui-même une copie). Idempotent |
| Tâche `ingest:verify` (singleton, toutes les minutes) et job `ingest.verify` | Vérification des contenus de l'extension par l'intégration publique (ci-dessus) |
| Tâche `ingest:partitions` (singleton, toutes les heures) | Partitions à venir, agrégation et purge des anciennes |
| Tâche `ingest:secrets` (singleton, toutes les 10 minutes) | Secrets du jour et du lendemain, suppression des anciens, purge de `installation_daily` |

Les tâches singleton reposent sur le contrat `Service.periodic` (verrou consultatif,
voir [services.md](services.md)).

## Hors périmètre (lots suivants)

Radar X et appels à l'API X, empreinte perceptuelle des médias (colonnes `phash`
prêtes), filtrage par langue et nombre de mots utiles (étage E0 du fact-check),
registres par pays, compteurs agrégés anonymes des installations.
