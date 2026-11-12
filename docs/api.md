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

## Dossiers

| Méthode | Chemin | Permission | Description |
|---|---|---|---|
| GET | `/v1/dossiers` | public / `dossiers:read` | Liste (public : publiés seulement, vue réduite). Filtres `status`, `kind`, `limit` (≤ 200), `before_id` |
| POST | `/v1/dossiers` | `dossiers:create` | Ouvre un dossier `{kind, x_post_id?, title?, priority?, reason?}` |
| GET | `/v1/dossiers/{public_id}` | public / `dossiers:read` | Détail (public : publiés seulement) |
| GET | `/v1/dossiers/{public_id}/history` | `dossiers:read` | Journal des transitions |
| POST | `/v1/dossiers/{public_id}/transitions` | selon la cible | `{to, reason?, expected_version?}` |
| POST | `/v1/dossiers/{public_id}/publish` | `dossiers:publish` | « Marquer comme publié » `{url, expected_version?}` |

Vue complète :

```json
{"public_id": "e65b…", "kind": "fact_check", "status": "received",
 "x_post_id": "1850000000000000001", "title": null, "priority": 0,
 "assigned_to": null, "published_url": null, "version": 1,
 "created_at": "…", "updated_at": "…"}
```

Vue publique : `public_id`, `kind`, `x_post_id`, `title`, `published_url`, `updated_at`.

## Événements (SSE)

`GET /v1/events` — permission `events:read`. Voir [events.md](events.md).

| Paramètre | Description |
|---|---|
| `after` | Curseur `"<xid>-<id>"`, `0` (début) ou `latest` (défaut) |
| `topics` | Répétable : `?topics=dossier.opened&topics=incident.opened` |

```
id: 1300-42
event: dossier.transitioned
data: {"id":"1300-42","topic":"dossier.transitioned","aggregate_type":"dossier",...}
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
| GET | `/v1/errors/{empreinte}` | `errors:read` (avec trace) |
| POST | `/v1/errors/{empreinte}/resolve` `{note?}` | `errors:manage` |

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
| GET | `/v1/drafts?status=pending&notified=&limit=&before_id=` | `dossiers:read` ; brouillons en relecture (`drafted_at` posé), du plus récent au plus ancien ; `before_id` = champ `seq` |
| GET | `/v1/drafts/{id}` | `dossiers:read` ; brouillon complet : issue, besoin et types de contexte, confiance et bande, affirmation, contenu et lien, dossier, notes par langue (texte, fiche, origine), liens, justification avec citations (source, passage, traduction française), ce que les sources ne disent pas, points à vérifier, objection, critique, contrôles, indicateurs, sources citées, `version` |
| POST | `/v1/drafts/{id}/review` | `{decision: accept\|edit\|reject, veracity?, note_text?, note_lang?, labels?, reason?, expected_version?}` ; `dossiers:review` (avis favorable) ou `dossiers:decide` (validation, rejet) ; répond `{outcome, draft}`. Erreurs : `TWR-FCK-007` (inconnu), `TWR-FCK-008` (déjà décidé), `TWR-FCK-009` (revue invalide), conflit de version |
| POST | `/v1/drafts/{id}/notified` | `dossiers:read` (`204`) ; marque le brouillon comme affiché par le bot (idempotent) |
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
| GET | `/v1/groups/{id}` | `dossiers:read` ; groupe (niveau, état, `merged_into`, fiche et bande, verdict actif avec `reuse_rule`), membres actifs (texte d'origine et langue, texte pivot, posts), journal (50 derniers) |
| GET | `/v1/groups/{id}/members?include_removed=` | `dossiers:read` |
| POST | `/v1/groups/{target}/merge` | `claim_groups:manage` ; `{source_group_id, reason}` ; `TWR-FCK-020` à `023` |
| POST | `/v1/groups/{id}/split` | `claim_groups:manage` ; `{claim_ids, reason}` ; répond le nouveau groupe ; `TWR-FCK-024` |
| POST | `/v1/reference-facts` | `reference_facts:write` (`201`) ; fiche complète, statut `pending` ; `TWR-FCK-026` |
| POST | `/v1/reference-facts/from-verdict` | `reference_facts:write` (`201`) ; `{verdict_id, value, period_start, period_end, source_url, source_name, published_at, next_publication?, indicator?, label?}` ; verdict actif d'une statistique |
| GET | `/v1/reference-facts?status=&indicator=`, `/v1/reference-facts/{id}` | `dossiers:read` |
| POST | `/v1/reference-facts/{id}/validate` | `reference_facts:validate` ; `reference_fact.validated` |
| POST | `/v1/reference-facts/{id}/reject` | `reference_facts:validate` ; `{reason}` |
| GET | `/v1/reference-facts/{id}/bands` | `dossiers:read` |
| POST | `/v1/verdict-bands` | `reference_facts:validate` (`201`) ; `{fact_id, band, side, veracity, context_need, context_types, source_verdict_id}` ; `TWR-FCK-027`, `028` |
| POST | `/v1/verdict-bands/from-verdict` | `reference_facts:validate` (`201`) ; `{verdict_id}` : bande et issue du verdict |
| POST | `/v1/verdict-bands/{id}/revoke` | `reference_facts:validate` ; `{reason}` |
| GET | `/v1/profiles` | `profiles:read` ; versions (dont la version 0 par défaut) |
| GET | `/v1/profiles/active` | `profiles:read` ; version et réglages complets |
| POST | `/v1/profiles` | `profiles:manage` (`201`) ; `{settings?\|changes?, reason, activate}` (`changes` : chemins plats, ex. `levels.N1.e1_threshold`) ; `TWR-FCK-030` hors bornes |
| POST | `/v1/profiles/{version}/activate`, `/v1/profiles/rollback` | `profiles:manage` ; `{reason}` ; `TWR-FCK-029` |
| POST | `/v1/level-overrides` | `levels:override` (`201`) ; `{target_type: content\|group\|account, target, level, hours, reason}` ; répond le forçage et `applied` ; `TWR-FCK-031`, `032` |
| GET | `/v1/level-overrides?active=true` | `dossiers:read` |
| POST | `/v1/level-overrides/{id}/revoke` | `levels:override` ; `{reason}` |
