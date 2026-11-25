# API HTTP (gate)

Base : `https://<domaine-gate>/`. Réponses JSON. IDs X et Discord **en chaînes**.
Documentation interactive : `/docs` (hors production). Format d'erreur : voir
[error-handling.md](error-handling.md).

## En-têtes

| En-tête | Usage |
|---|---|
| `Authorization: Bearer t12a_…` | Client de service (bot, outils) |
| `X-Acting-Discord-Id: <id>` | Membre de l'équipe qui agit via le service |
| `X-Request-ID` | Optionnel ; renvoyé dans la réponse et dans les erreurs |
| `Last-Event-ID` | Reprise du flux SSE |

## Santé

| Méthode | Chemin | Description |
|---|---|---|
| GET | `/health` | Vivacité (sans base) : `{"status": "ok", "version": "…"}` |
| GET | `/ready` | Disponibilité : 200 si la base répond, sinon 503 `TWR-DB-001` |

## Cases

Les anciens chemins `/v1/dossiers…` répondent encore à l'identique (cachés du schéma) pendant la transition du renommage « dossier » → « case » (voir [decisions.md](decisions.md)).

**Identifiant d'un case : l'id court.** `{case_id}` est le `short_id` du case : les 8 premiers caractères hexadécimaux de son UUID, un de plus en cas de collision (`fbaa8363`). C'est lui qu'on affiche et qu'on met dans les liens (posts X, site, bot). La correspondance est **exacte**, jamais par préfixe : un lien déjà publié désigne toujours le même case. L'UUID complet (`public_id`) reste accepté et renvoyé pendant la transition (expand/contract). Id mal formé (moins de 8 caractères, non hexadécimal) → `422` `TWR-GEN-001` ; inconnu → `404` `TWR-DOS-001`.

| Méthode | Chemin | Permission | Description |
|---|---|---|---|
| GET | `/v1/cases` | public / `cases:read` | Liste (public : publiés seulement, vue réduite). Filtres `status`, `kind`, `limit` (≤ 200), `before_id` |
| POST | `/v1/cases` | `cases:create` | Ouvre un case `{kind, x_post_id?, title?, priority?, reason?}` |
| GET | `/v1/cases/{case_id}` | public / `cases:read` | Détail (public : publiés seulement). Vue interne : `discord_thread_id` (post du forum, chaîne) et `draft_id` (dernier brouillon de fact-check du case, seulement sur ce détail) |
| PUT | `/v1/cases/{case_id}/discord-thread` | `cases:read` (`204`) | Enregistre le post du forum Discord du case `{thread_id}` (chaîne de chiffres). Marqueur opérationnel : ni audit ni événement. Erreurs : `TWR-DOS-007` (id invalide), `TWR-DOS-001` |
| GET | `/v1/cases/{case_id}/history` | `cases:read` | Journal des transitions |
| POST | `/v1/cases/{case_id}/transitions` | selon la cible | `{to, reason?, expected_version?}` |
| POST | `/v1/cases/{case_id}/publish` | `cases:publish` | « Marquer comme publié » `{url, expected_version?}` |

Vue complète :

```json
{"short_id": "e65b1c07", "public_id": "e65b1c07-…", "kind": "fact_check", "status": "received",
 "x_post_id": "1850000000000000001", "title": null, "priority": 0,
 "assigned_to": null, "published_url": null, "version": 1,
 "created_at": "…", "updated_at": "…"}
```

Vue publique : `short_id`, `public_id`, `kind`, `x_post_id`, `title`, `published_url`, `updated_at`.

### Lecture pour le site : recherche, statistiques, détail complet

Code : `gate/routes/case_views.py`, `core/usecases/case_views.py`. Accès **public** :
cases **publiés** seulement, verdicts **validés** seulement (un brouillon ne sort jamais,
ni dans un filtre ni dans un compte). Avec `cases:read` : tous les cases, champs
internes en plus. Réponses anonymes avec `Cache-Control: public, max-age=300`. Aucune
route ne renvoie l'auteur du post : le site intègre le post depuis son lien.

| Méthode | Chemin | Permission | Description |
|---|---|---|---|
| GET | `/v1/cases/search` | public / `cases:read` | Recherche filtrée, **pages numérotées** avec le total |
| GET | `/v1/cases/stats` | public / `cases:read` | Comptes des cases qui passent les mêmes filtres |
| GET | `/v1/cases/{case_id}/details?lang=` | public / `cases:read` | Tout sur un case : post, affirmation, verdict, note, chronologie |

Filtres communs à `search` et `stats` (tous facultatifs, combinés en ET ; les valeurs
répétées d'un même filtre sont combinées en OU) :

| Paramètre | Description |
|---|---|
| `q` | Texte (≤ 200 caractères) cherché dans le titre et l'affirmation : plein texte français (`websearch_to_tsquery`, guillemets et `-mot` acceptés), plein texte du texte original, et sous-chaîne (`chôm`) |
| `kind` | `fact_check`, `moderation` |
| `status` | Équipe seulement (le public ne voit que `published`) |
| `veracity` | Répétable : `true`, `mostly_true`, `misleading`, `false`, `unverifiable` |
| `claim_type` | Répétable : types d'affirmation (`statistic`, `quote`…) |
| `context_need` | `none`, `useful`, `necessary` |
| `lang` | Langue originale de l'affirmation (`fr`, `en`…) |
| `jurisdiction` | Code ISO 3166, `EU` ou `INT` (insensible à la casse) |
| `platform` | `x`, `bluesky`… |
| `since`, `until` | Date de création du case, ISO 8601 (`since` inclus, `until` exclu ; sans fuseau = UTC) |

`GET /v1/cases/search` ajoute `sort` (`newest` par défaut, `oldest`, `relevance` = rang
du texte `q`), `page` (≥ 1, défaut 1) et `per_page` (1 à 100, défaut 20). Une page qui
commencerait au-delà de la 10 000ᵉ ligne → `422` `TWR-DOS-008` (affiner les filtres) ;
`since` après `until` → même code.

```json
{"items": [{"short_id": "e65b1c07", "public_id": "e65b1c07-…", "kind": "fact_check", "title": "Le chômage a doublé en un an",
  "platform": "x", "x_post_id": "1850…", "external_id": "1850…", "url": "https://x.com/i/status/1850…",
  "published_url": "https://x.com/seat12a/status/…", "published_at": "…", "created_at": "…",
  "updated_at": "…", "claim_type": "statistic", "claim_lang": "fr", "jurisdictions": ["FR"],
  "veracity": "false", "context_need": "necessary", "verdict_state": "current",
  "status": null, "priority": null, "assigned_to": null}],
 "page": 1, "per_page": 20, "total": 42, "pages": 3}
```

`verdict_state` : `current` (actif, non périmé), `expired` (péremption atteinte),
`superseded` (remplacé par un verdict plus récent), `null` sans verdict validé ; pour
l'équipe aussi `draft` et `rejected`. Un verdict périmé ou remplacé reste affiché avec
sa date de validation : au site de le signaler comme ancien. `published_at` = date de la
transition vers `published`. `status`, `priority`, `assigned_to` : équipe seulement.

`GET /v1/cases/stats` ajoute `interval` (`day`, `week`, `month` par défaut) :

```json
{"total": 42, "by_kind": {"fact_check": 42}, "by_veracity": {"false": 20, "misleading": 12},
 "by_claim_type": {"statistic": 15}, "by_lang": {"fr": 38, "en": 4},
 "by_jurisdiction": {"FR": 35, "US": 4}, "by_status": null, "interval": "month",
 "timeline": [{"period": "2026-11-01", "count": 42}], "generated_at": "…"}
```

Comptes triés du plus grand au plus petit ; `by_veracity` ne compte que les verdicts
validés (équipe comprise : un brouillon n'est pas un verdict) ; `by_jurisdiction` : 20
premières ; `by_status` : équipe seulement ; `timeline` : créations de cases par période
(début de période), 400 périodes au plus, de la plus ancienne à la plus récente.

`GET /v1/cases/{case_id}/details` renvoie les champs d'un résultat de recherche, plus :

| Champ | Contenu |
|---|---|
| `content` | `{platform, external_id, url, lang}` du post |
| `claim` | `{text, lang, text_fr, claim_type, subject, place, period_start, period_end, jurisdictions}` ; `text` en français si `lang=fr`, sinon dans la langue du post |
| `verdict` | `{id, veracity, context_need, context_types, validated_at, expires_at, state}` (`id` = identifiant public du verdict) |
| `note` | Note validée dans la meilleure langue (`lang` demandée, langue du post, `en`, `fr`) : `{lang, text, fact_sheet, machine_translated, links}` ; `null` si aucune note validée |
| `posts_count` | Nombre de posts qui portent la même affirmation (membres du groupe) |
| `timeline` | `[{status, at}]` ; équipe : aussi `from_status`, `actor`, `reason` |
| `internal` | Équipe seulement : `{discord_thread_id, draft_id, verdict_status, confidence, level, version, opened_by}` |

Case inconnu ou non publié (public) → `404` `TWR-DOS-001`.

## Événements (SSE)

`GET /v1/events` — permission `events:read`. Voir [events.md](events.md).

| Paramètre | Description |
|---|---|
| `after` | Curseur `"<xid>-<id>"`, `0` (début) ou `latest` (défaut) |
| `topics` | Répétable : `?topics=case.opened&topics=incident.opened` |

```
id: 1300-42
event: case.transitioned
data: {"id":"1300-42","topic":"case.transitioned","aggregate_type":"case",...}
```

### Curseurs stockés (clients de service)

| Méthode | Chemin | Permission |
|---|---|---|
| GET | `/v1/events/cursors/{nom}` → `{name, cursor, updated_at}` (`cursor` nul si jamais stocké) | `events:read`, client de service ou membre via un service |
| PUT | `/v1/events/cursors/{nom}` `{cursor: "<xid>-<id>"}` → idem | idem |

Erreurs : `TWR-EVT-003` (nom invalide), `TWR-EVT-001` (curseur invalide). Voir
[events.md](events.md#curseurs-stockés-des-clients-de-service).

## Usage et budgets

| Méthode | Chemin | Permission |
|---|---|---|
| GET | `/v1/llm/usage` | `usage:read` (`editor`, `admin`) |

Réponse : `{day, usage_tier, free: [{provider, free_group, used_tokens,
allowance_tokens}], period_start, period_end, scopes: [{scope, spent_usd_micros,
reserved_usd_micros, cap_usd_micros}]}`. Même source que `tower llm usage`
(`core/llm/usage.py`). Les portées incluent `global`, les portées de fournisseur et
`provider:x` (radar).

## Erreurs stockées

| Méthode | Chemin | Permission |
|---|---|---|
| GET | `/v1/errors/catalog` | public |
| GET | `/v1/errors?include_resolved=&code=&limit=` | `errors:read` |
| GET | `/v1/errors/{empreinte}` | `errors:read` (avec trace, `hint` et `retryable` du catalogue) |
| POST | `/v1/errors/{empreinte}/resolve` `{note?}` | `errors:manage` |

## Équipe

Rangs et permissions individuelles : règles dans [auth.md](auth.md#équipe--rangs-administrateurs-et-permissions-individuelles).
Toutes les routes passent par un token de service ; `/me` et les écritures exigent
`X-Acting-Discord-Id`. Les IDs Discord sont des **chaînes**.

| Méthode | Chemin | Permission |
|---|---|---|
| GET | `/v1/staff/me` | tout membre (administrateur synthétisé) ; renvoie aussi `actor_permissions` (droits effectifs, déjà ∩ scopes du bot) |
| GET | `/v1/staff?include_inactive=false` | `staff:read` ; `{"items": [StaffOut]}`, administrateurs en tête |
| GET | `/v1/staff/{discord_id}` | `staff:read`, ou soi-même ; `404 TWR-STF-001` si non classé |
| PUT | `/v1/staff/{discord_id}` | `staff:manage` ; corps `{"display_name": str\|null}` ; classe ou réactive ; `409 TWR-STF-002` si déjà classé |
| DELETE | `/v1/staff/{discord_id}` | `staff:manage` ; `204` ; déclasse (vide rôles et permissions) |
| PUT | `/v1/staff/{discord_id}/permissions` | `permissions:manage_others`, ou `permissions:manage_self` pour soi-même ; corps `{"permissions": [...]}` (liste complète) |

`StaffOut` : `discord_id`, `display_name`, `active`, `is_admin`, `roles`, `permissions`
(liste explicite), `effective_permissions` (rôles ∪ individuelles ; toutes les permissions
attribuables pour un administrateur), `created_at`, `updated_at`.

## Ingestion et liste de suivi

Détail des corps, quotas et erreurs : [ingestion.md](ingestion.md).

| Méthode | Chemin | Permission |
|---|---|---|
| POST | `/v1/ingest/observations` | `X-Installation-Token` ou `content:ingest` ; réponse `202` avec compteurs |
| GET | `/v1/ingest/config` | public (`ETag`, `If-None-Match` → `304`) |
| GET | `/v1/watched-accounts?active=&platform=&limit=&offset=` | `watchlist:read` |
| POST | `/v1/watched-accounts` | `watchlist:write` (`201`) ; `external_id` **ou** `handle` (un compte ajouté par pseudo est résolu par le radar) |
| PATCH | `/v1/watched-accounts/{public_id}` | `watchlist:write` (verrou optimiste `version`) |

## Fact-check

Voir [fact-check.md](fact-check.md). Identifiants en chaînes ; l'identifiant d'un
brouillon est l'identifiant public du verdict.

| Méthode | Chemin | Accès |
|---|---|---|
| POST | `/v1/factcheck/requests` | `factcheck:request` (`202`) ; corps `{url}` ou `{platform, external_id}` ; répond `{platform, external_id, accepted, duplicate}`. Enfile `factcheck.request` (N4) |
| POST | `/v1/factcheck/manual` | `cases:review` + `cases:create` (`202`) ; note écrite par l'équipe, sans modèle : `{url | platform+external_id, claim_text, claim_type, veracity, context_need?, context_types?, note_text?, note_lang?, links?, fact_sheet?}` ; répond `{platform, external_id, status: drafted|queued, draft_id, duplicate}`. Erreur de saisie `TWR-FCK-049`. Voir [fact-check.md](fact-check.md#notes-manuelles-sans-ia) |
| GET | `/v1/drafts?status=pending&notified=&limit=&before_id=` | `cases:read` ; brouillons en relecture (`drafted_at` posé), du plus récent au plus ancien ; `before_id` = champ `seq` |
| GET | `/v1/drafts/{id}` | `cases:read` ; brouillon complet : issue, besoin et types de contexte, confiance et bande, affirmation, contenu et lien, case (`id`, `short_id`, `status`, `discord_thread_id`), notes par langue (texte, fiche, origine), liens, justification avec citations (source avec `tier` effectif, `standing` et `tier_basis` — voir [factcheck/sources.md](factcheck/sources.md) —, passage, traduction française), ce que les sources ne disent pas, points à vérifier, objection, critique, contrôles, indicateurs, sources citées, `version` |
| POST | `/v1/drafts/{id}/review` | `{decision: accept\|edit\|reject, veracity?, note_text?, note_lang?, labels?, reason?, expected_version?}` ; `cases:review` (avis favorable) ou `cases:decide` (validation, rejet) ; répond `{outcome, draft}`. Erreurs : `TWR-FCK-007` (inconnu), `TWR-FCK-008` (déjà décidé), `TWR-FCK-009` (revue invalide), conflit de version |
| POST | `/v1/drafts/{id}/notified` | `cases:read` (`204`) ; marque le brouillon comme affiché par le bot (idempotent) |
| GET | `/v1/verdict-index/{platform}/{bucket}` | **public** ; index des verdicts pour l'extension : `{platform, bucket, hashes}`, `hashes` = SHA-256 (hex) des IDs natifs des posts de la boîte qui ont un verdict visible (même règle que la route ci-dessous) ; `bucket` = 4 premiers caractères hex de `sha256(ID natif UTF-8)` ; `Cache-Control: public, max-age=300`, `ETag` fort (`If-None-Match` → `304`) ; boîte ou plateforme invalide → `422` `TWR-FCK-050`. Détails : [extension-api.md](extension-api.md) |
| GET | `/v1/content/{platform}/{external_id}/verdict?lang=` | **public** ; verdicts **validés** et non périmés seulement (lot 4 : aussi ceux qui couvrent la variante par rattachement ou par bande, avec `inherited: {rule, origin_verdict_id, origin_url}` ; la note d'origine n'est montrée que si son régime le permet), par affirmation : texte, issue, besoin et types de contexte, date de validation, note (langue choisie : `lang` demandée si elle existe, sinon langue du post, puis `en`, puis `fr`), fiche, liens, `machine_translated` ; en-tête `Cache-Control: public, max-age=300` ; `404` `TWR-FCK-013` sans verdict validé |

### Fact-check V1 (lot 4)

Voir [fact-check.md](fact-check.md#lot-4--v1--embeddings-groupes-réutilisation-profils).
Le brouillon (`GET /v1/drafts/{id}`) gagne `group` (`id`, `level`, `members`),
`other_posts` (10 autres posts du groupe), `platforms`, `reusable_verdict` (verdict du
groupe proche suggéré), `reference_fact`, `band` (`band`, `side`, `label`), `outdated`,
`external_factchecks` ; `POST /v1/drafts/{id}/review` accepte `validate_band: true`
(un éditeur valide aussi la bande du brouillon).

| Méthode | Chemin | Accès |
|---|---|---|
| GET | `/v1/groups/{id}` | `cases:read` ; groupe (niveau, état, `merged_into`, fiche et bande, verdict actif avec `reuse_rule`), membres actifs (texte d'origine et langue, texte pivot, posts), journal (50 derniers) |
| GET | `/v1/groups/{id}/members?include_removed=` | `cases:read` |
| POST | `/v1/groups/{target}/merge` | `claim_groups:manage` ; `{source_group_id, reason}` ; `TWR-FCK-020` à `023` |
| POST | `/v1/groups/{id}/split` | `claim_groups:manage` ; `{claim_ids, reason}` ; répond le nouveau groupe ; `TWR-FCK-024` |
| POST | `/v1/reference-facts` | `reference_facts:write` (`201`) ; fiche complète, statut `pending` ; `TWR-FCK-026` |
| POST | `/v1/reference-facts/from-verdict` | `reference_facts:write` (`201`) ; `{verdict_id, value, period_start, period_end, source_url, source_name, published_at, next_publication?, indicator?, label?}` ; verdict actif d'une statistique |
| GET | `/v1/reference-facts?status=&indicator=`, `/v1/reference-facts/{id}` | `cases:read` |
| POST | `/v1/reference-facts/{id}/validate` | `reference_facts:validate` ; `reference_fact.validated` |
| POST | `/v1/reference-facts/{id}/reject` | `reference_facts:validate` ; `{reason}` |
| GET | `/v1/reference-facts/{id}/bands` | `cases:read` |
| POST | `/v1/verdict-bands` | `reference_facts:validate` (`201`) ; `{fact_id, band, side, veracity, context_need, context_types, source_verdict_id}` ; `TWR-FCK-027`, `028` |
| POST | `/v1/verdict-bands/from-verdict` | `reference_facts:validate` (`201`) ; `{verdict_id}` : bande et issue du verdict |
| POST | `/v1/verdict-bands/{id}/revoke` | `reference_facts:validate` ; `{reason}` |
| GET | `/v1/profiles` | `profiles:read` ; versions (dont la version 0 par défaut) |
| GET | `/v1/profiles/active` | `profiles:read` ; version et réglages complets |
| POST | `/v1/profiles` | `profiles:manage` (`201`) ; `{settings?\|changes?, reason, activate}` (`changes` : chemins plats, ex. `levels.N1.e1_threshold`) ; `TWR-FCK-030` hors bornes |
| POST | `/v1/profiles/{version}/activate`, `/v1/profiles/rollback` | `profiles:manage` ; `{reason}` ; `TWR-FCK-029` |
| POST | `/v1/level-overrides` | `levels:override` (`201`) ; `{target_type: content\|group\|account, target, level, hours, reason}` ; répond le forçage et `applied` ; `TWR-FCK-031`, `032` |
| GET | `/v1/level-overrides?active=true` | `cases:read` |
| POST | `/v1/level-overrides/{id}/revoke` | `levels:override` ; `{reason}` |

### Fact-check V2 (lot 5) : récits et vagues

Voir [fact-check.md](fact-check.md#lot-5--v2--flux-de-masse-ordonnanceur-récits-et-vagues).
Tag OpenAPI `narratives`. Le brouillon (`GET /v1/drafts/{id}`) gagne `narrative`
(`{id, title, state, groups}` quand son groupe appartient à un récit, sinon nul) et
`coordination` (signaux informatifs, voir ci-dessous) ; `GET /v1/groups/{id}` (et les
réponses de fusion et de scission) gagne `narrative` (`{id, state}` ou nul).

| Méthode | Chemin | Accès |
|---|---|---|
| GET | `/v1/narratives?state=&limit=` | `cases:read` ; `state` parmi `active`, `watch`, `surge`, `alert`, `closed` ; sans `state` : récits ouverts, activité la plus récente d'abord. Par récit : `id`, `title`, `title_status`, `state`, `theme`, `groups`, `stats_1h` (`window_end`, `share`, `accounts`, `platforms`), `opened_at`, `last_activity_at` |
| GET | `/v1/narratives/{id}` | `cases:read` ; en plus : `groups_count`, `closed_at`, `medoids`, `groups` (`id`, `level`, `state`, `verdict` `{id, status, veracity}`, `light_signal`, `reach`), `stats` (`5m`, `1h`, `6h` : 12 derniers points, du plus ancien au plus récent), `journal` (50 derniers), `representative_group`, `previous_narrative`, `merged_into`, `split_suggested`, `novelty` (10 empreintes qui ont forcé une extraction) ; `TWR-FCK-043` |
| POST | `/v1/narratives/{target}/merge` | `claim_groups:manage` ; `{source_id, reason}` ; répond le récit cible ; `TWR-FCK-043`, `044` ; `narrative.merged` |
| POST | `/v1/narratives/{id}/split` | `claim_groups:manage` ; `{group_ids, reason}` ; répond le nouveau récit ; `TWR-FCK-043`, `044` ; `narrative.split` |
| POST | `/v1/narratives/{id}/title` | `cases:review` ; `{title?}` : sans titre, valide le brouillon de titre ; avec, enregistre le titre corrigé (validé) |

**Signaux de coordination** (`coordination` du brouillon) : liste de `{kind, count,
window_minutes, detail}`, **informative seulement**, jamais utilisée pour un verdict ni
pour la priorité, jamais fondée sur un bord politique supposé :

| `kind` | Condition |
|---|---|
| `near_identical_burst` | Au moins 10 comptes distincts publient des copies (même racine `copy_of_id` ou même empreinte) des posts du groupe en 10 minutes |
| `new_accounts` | Au moins 50 % des auteurs (10 au moins) vus pour la première fois par Seat 12A depuis moins de 7 jours (date de première observation, pas l'âge réel du compte) |
| `installation_burst` | Une installation de l'extension produit au moins 20 observations des posts du groupe en 10 minutes |
| `media_reuse` | Un même média est attaché à des posts d'au moins 2 groupes (`window_minutes` = 0 ; les légendes peuvent se contredire : l'équipe vérifie) |

### Lot 6 : registre de fiabilité et registres par pays

Voir [fact-check.md](fact-check.md#lot-6--v3--atlas-complet-et-qualité). Tag OpenAPI
`registry`. Lecture : `registry:read` (relecteurs et plus) ; écriture : `registry:write`
(éditeurs, admin). `{ref}` est l'identifiant numérique (chaîne) ou le domaine. Chaque
écriture incrémente `version`, ajoute un instantané dans `source_registry_versions` et
publie l'événement dans la même transaction. Verrouillage optimiste : `version` attendue
dans le corps, sinon `TWR-ATL-014` (409).

| Méthode | Chemin | Accès |
|---|---|---|
| GET | `/v1/registry?country=&tier=&active=&q=&limit=&offset=` | `registry:read` ; entrées (domaine, éditeur, niveau, type, pays, motif, `aliases`, `qid`, `controlled_by`, `controlled_by_qid`, `controlled_by_aliases`, `control_origin`, `statutory_independence`, `independence_basis`, `active`, `version`, `updated_by`) |
| GET | `/v1/registry/export` | `registry:read` ; registre publié : entrées actives et empreinte (`digest`) — versionné et publiable |
| GET | `/v1/registry/{ref}` | `registry:read` ; `TWR-ATL-012` |
| GET | `/v1/registry/{ref}/history` | `registry:read` ; versions (instantané, `change`, `reason`, `changed_by`) |
| POST | `/v1/registry` | `registry:write` ; `{domain, publisher, tier, kind, country?, reason, aliases?, qid?, controlled_by?, …, statutory_independence?, independence_basis?}` ; `TWR-ATL-013` (motif vide, domaine invalide, indépendance sans critère externe) ; `source_registry.changed` |
| PATCH | `/v1/registry/{ref}` | `registry:write` ; `{version, reason, …champs modifiés}` ; un `controlled_by` posé par un humain passe `control_origin` à `team` ; `TWR-ATL-012`, `013`, `014` |
| POST | `/v1/registry/{ref}/deactivate` | `registry:write` ; `{version, reason}` |
| POST | `/v1/registry/{ref}/seed-control` | `registry:write` ; enfile `atlas.control_seed` (Wikidata P749 puis P127) ; 202 `{enqueued}` |
| GET / PUT | `/v1/countries/{country}/sources` | `jurisdiction_sources` d'un pays ; PUT = une entrée, clé `(source_type, key)` (`key` = adaptateur sinon domaine), `version` optionnelle ; `country_registry.changed` |
| GET / PUT | `/v1/countries/{country}/legal-portals` | `legal_portals` ; PUT clé `url` |
| GET / PUT | `/v1/countries/{country}/elections` | `election_calendar` ; PUT clé `(kind, first_round)` |

Le niveau d'une source repose sur le **processus éditorial**, jamais sur l'orientation
politique : tout changement de niveau exige un motif, et l'indépendance statutaire exige
un critère externe (`imf_sdds`, `imf_sdds_plus`, `ess_peer_review`, `statute`).

## Badges de vérification

Règles : [badges.md](badges.md). IDs natifs en **chaînes**, jamais de pseudo.
`{p}/{id}` = plateforme et identifiant natif (`x/12345`).

| Méthode | Chemin | Permission |
|---|---|---|
| GET | `/v1/badges/categories` | public ; catégories, couleurs, rang (cache 24 h) |
| GET | `/v1/badges/lookup?platform=x&ids=1,2` | public ; ≤ 100 IDs ; `Cache-Control`, `ETag`, `304` |
| GET | `/v1/badges/logos/{sha256}.{ext}` | public ; icône d'organisation stockée par Tower (`png`, `ico`, `jpg`, `gif`, `webp`) ; immuable (`Cache-Control: public, max-age=31536000, immutable`), `nosniff`, CSP `sandbox` ; `TWR-BDG-029` si absente, 422 si le nom est mal formé |
| GET | `/v1/badges/accounts/{p}/{id}` | public ; badge et affiliations actives ; `404 TWR-BDG-001` |
| POST | `/v1/badges/requests` | public (`badges:submit`), limité par IP ; `201 {public_id, status}` ; corps `kind`, `account`, `category` ou `org`, `title`, `justification`, `contact`, `evidence_urls` (https, ≤ 10) |
| POST | `/v1/badges/reports` | public, limité par IP ; `201` ; corps `account`, `reason`, `comment` |
| POST | `/v1/badges/x-government-hints` | public, limité par IP ; `202 {queued}` ; corps `{"ids": [...]}` (≤ 50 IDs X) |
| GET | `/v1/badges/resolve/x/{handle}` | `badges:manage` ; ID natif d'un pseudo X lu par l'API officielle (une lecture d'utilisateur facturée sur `provider:x`, sans cache) ; `{platform, external_id, handle, display_name, verified_type}` ; `404 TWR-BDG-025`, `409 TWR-BDG-024` (budget), `503 TWR-BDG-026` (pas de `TOWER_X_BEARER_TOKEN` sur gate) |
| GET | `/v1/badges?status=active&category=&source=&platform=&limit=&offset=` | `badges:read` |
| GET | `/v1/badges/accounts/{p}/{id}/badge` | `badges:read` ; ligne complète (tout état, note interne) |
| PUT | `/v1/badges/accounts/{p}/{id}` | `badges:manage` ; corps `category`, `label`, `logo_url` (https ; pour une organisation, logo affiché à côté du pseudo de ses affiliés), `expertise` (badge `expert` seulement, sinon `422 TWR-BDG-027` ; `""` l'efface), `note`, `handle`, `version` (verrou optimiste) ; `409 TWR-BDG-004` si bloqué |
| POST | `/v1/badges/accounts/{p}/{id}/revoke` | `badges:manage` ; corps `{"reason"}` |
| POST | `/v1/badges/accounts/{p}/{id}/block` | `badges:manage` ; corps `{"reason"}` |
| POST | `/v1/badges/accounts/{p}/{id}/unblock` | `badges:manage` |
| GET | `/v1/badges/accounts/{p}/{id}/history?limit=` | `badges:read` ; actions, acteur, source, motif, changements |
| POST | `/v1/badges/accounts/{p}/{id}/org-keys` | `badges:manage` ; `201 {public_id, label, token}` (token affiché une fois) |
| GET | `/v1/badges/accounts/{p}/{id}/org-keys` | `badges:read` |
| POST | `/v1/badges/org-keys/{public_id}/revoke` | `badges:manage` ; `204` |
| GET | `/v1/badges/affiliations?status=&org_id=&member_id=&limit=&offset=` | `badges:read`, ou organisation (les siennes) |
| POST | `/v1/badges/affiliations` | `badges:manage` ; corps `member`, `org`, `title`, `validate_now` (défaut `true`) |
| GET | `/v1/badges/affiliations/{id}` | `badges:read`, ou l'organisation concernée |
| POST | `/v1/badges/affiliations/{id}/validate` \| `reconfirm` \| `reject` \| `revoke` | `badges:manage`, ou l'organisation concernée (`403 TWR-BDG-018` sinon) ; `reject`/`revoke` : corps `{"reason"}` |
| POST | `/v1/badges/affiliations/{id}/primary` | `badges:manage` |
| GET | `/v1/badges/requests?status=pending&kind=` | `badges:read` (contact inclus) |
| GET | `/v1/badges/requests/{id}` | `badges:read` (contact inclus) |
| POST | `/v1/badges/requests/{id}/approve` \| `reject` | `badges:manage` ; corps `category` (approbation d'un badge), `label`, `expertise` (badge `expert`), `note` |
| GET | `/v1/badges/reports?status=open` | `badges:read` |
| GET | `/v1/badges/reports/{id}` | `badges:read` |
| POST | `/v1/badges/reports/{id}/resolve` | `badges:manage` ; corps `status` (`resolved`, `dismissed`), `note` |
| POST | `/v1/badges/imports/wikidata` | `badges:manage` ; `202` ; corps `{"scopes": [...]}` (défaut : `TOWER_BADGES_WIKIDATA_SCOPES`) |
| GET | `/v1/badges/imports` | `badges:read` ; dernier import par portée |

Espace organisation (en-tête `X-Organization-Token`, voir
[badges.md](badges.md#organisations--clés-dorganisation)) :

| Méthode | Chemin | Effet |
|---|---|---|
| GET | `/v1/org/me` | l'organisation et son badge |
| GET | `/v1/org/affiliations?status=` | ses affiliations |
| POST | `/v1/org/affiliations` | affilie un compte (`member`, `title`) : actif tout de suite, un an |

`account`, `member`, `org` : `{"platform": "x", "external_id": "…", "handle": "…"}`
(`handle` informatif).

## Identifiants courts

| Méthode | Chemin | Permission | Description |
|---|---|---|---|
| GET | `/v1/ids/{kind}/{ref}` | `cases:read` | Identifiant complet de l'élément dont l'identifiant commence par `ref` (6 caractères hexadécimaux ou plus, tirets ignorés). `kind` : `case`, `group`, `claim`, `narrative`, `draft` ; badges (`badges:read` au lieu de `cases:read`) : `affiliation`, `badge_request`, `badge_report`, `org_key`. Répond `{kind, id}`. Recherche par plage sur l'index unique ; pour `case`, un `short_id` exact l'emporte sur le préfixe (un id court publié peut être le début d'un case plus récent). Les routes des cases acceptent directement l'id court : ce détour n'est plus nécessaire pour eux. Erreurs : `TWR-GEN-001` (préfixe invalide), `TWR-GEN-002` (aucun), `TWR-GEN-005` (plusieurs : taper plus de caractères) |
