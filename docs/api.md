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
| GET | `/v1/content/{platform}/{external_id}/verdict?lang=` | **public** ; verdicts **validés** seulement, par affirmation : texte, issue, besoin et types de contexte, date de validation, note (langue choisie : `lang` demandée si elle existe, sinon langue du post, puis `en`, puis `fr`), fiche, liens, `machine_translated` ; en-tête `Cache-Control: public, max-age=300` ; `404` `TWR-FCK-013` sans verdict validé |
