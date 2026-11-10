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

## Erreurs stockées

| Méthode | Chemin | Permission |
|---|---|---|
| GET | `/v1/errors/catalog` | public |
| GET | `/v1/errors?include_resolved=&code=&limit=` | `errors:read` |
| GET | `/v1/errors/{empreinte}` | `errors:read` (avec trace) |
| POST | `/v1/errors/{empreinte}/resolve` `{note?}` | `errors:manage` |

## À venir

Ingestion de l'extension (token d'installation anonyme, rate-limit, payload minimal),
verdicts publics, configuration pilotée par le serveur pour l'extension.
