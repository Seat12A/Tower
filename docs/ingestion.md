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
| `simhash64`, `simhash_bands`, `hamming` | idem | SimHash 64 bits (mots et bigrammes, `blake2b`), en entier signé pour `BIGINT` ; 4 bandes de 16 bits |
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
  `TWR-ING-006`, média invalide) est **refusé seul** ; le reste du lot est traité.
- Réponse `202` :

```json
{"accepted": 49, "new": 12, "duplicates": 37, "rejected": 1,
 "errors": [{"index": 7, "code": "TWR-ING-006"}]}
```

Traitement, dans une transaction : quota de l'installation, comptes auteurs
(`platform_accounts`), `INSERT … ON CONFLICT (platform, external_id) DO UPDATE` des
contenus (métriques, `last_seen_at`, `observation_count` ; le texte d'origine n'est
jamais remplacé), médias (`media_assets` dédoublonnés par empreinte de l'URL,
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
refusée (`TWR-ING-011`). Publie `extension_filters.published`.

### Liste de suivi : `/v1/watched-accounts`

| Méthode | Chemin | Permission |
|---|---|---|
| GET | `/v1/watched-accounts?active=&platform=&limit=&offset=` | `watchlist:read` |
| POST | `/v1/watched-accounts` `{platform, external_id, handle?, category, priority?, note?}` | `watchlist:write` |
| PATCH | `/v1/watched-accounts/{public_id}` `{version, category?, priority?, active?, note?, handle?}` | `watchlist:write` |

`category` est une catégorie fermée de rôle public (`head_of_state_or_government`,
`minister`, `member_of_parliament`, `elected_official`, `candidate`,
`political_party`, `public_institution`, `media_outlet`, `journalist`,
`public_figure`, `other`), **jamais un bord politique**. Priorité de -100 à 100.
Doublon → `TWR-ING-009` ; introuvable → `TWR-ING-008` ; version périmée →
`TWR-GEN-003`. Chaque écriture est auditée (`watched_account_events`) et publie
`watchlist.changed`. Le suivi ne change que la surveillance et la latence, jamais le
verdict (règle 10).

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

## Service `ingest`

`TOWER_SERVICES=ping,ingest`, file `ingest`.

| Élément | Rôle |
|---|---|
| Job `ingest.link_copies` (`dedupe_key` `copies:<id>`) | Copie exacte (même `content_hash`, sans limite de date) ou quasi-copie (SimHash à distance ≤ 3, trouvée par les 4 bandes sur 30 jours) ; `copy_of_id` reçoit le plus ancien contenu (ou sa racine s'il est lui-même une copie). Idempotent |
| Tâche `ingest:partitions` (singleton, toutes les heures) | Partitions à venir, agrégation et purge des anciennes |
| Tâche `ingest:secrets` (singleton, toutes les 10 minutes) | Secrets du jour et du lendemain, suppression des anciens, purge de `installation_daily` |

Les tâches singleton reposent sur le contrat `Service.periodic` (verrou consultatif,
voir [services.md](services.md)).

## Hors périmètre (lots suivants)

Radar X et appels à l'API X, empreinte perceptuelle des médias (colonnes `phash`
prêtes), filtrage par langue et nombre de mots utiles (étage E0 du fact-check),
registres par pays, compteurs agrégés anonymes des installations.
