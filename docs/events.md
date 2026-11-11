# Bus d'événements

Source : `src/tower/core/events/`.

## Principe : outbox transactionnelle

Un événement est une ligne de la table `events`, écrite **dans la même transaction**
que le changement d'état qu'il décrit, suivie d'un `pg_notify('tower_events', topic)`.

- Rollback : ni l'événement ni la notification n'existent.
- Commit : PostgreSQL délivre la notification à tous les processus en `LISTEN`.
- La notification n'est qu'un **réveil** : la vérité est dans la table. Un
  polling de secours (`TOWER_JOB_POLL_INTERVAL`) couvre les notifications perdues.

```python
from tower.core.events import publish

async with db.transaction() as session:
    ...  # changement d'état
    await publish(session, "dossier.transitioned", payload,
                  aggregate_type="dossier", aggregate_id=public_id, actor=actor.label)
```

## Lecture sans trou : `(xid, id)`

Lire « `id` > dernier vu » est faux : une transaction ouverte peut détenir un `id`
plus petit qu'un événement déjà validé, qui serait alors sauté pour toujours.

Chaque événement stocke `xid = pg_current_xact_id()`. Le lecteur :

1. ne garde que `xid < pg_snapshot_xmin(pg_current_snapshot())`, c'est-à-dire des
   transactions toutes terminées ;
2. trie par `(xid, id)` et avance un curseur `(xid, id)`.

Tout événement qui deviendra visible plus tard aura un `xid` supérieur au seuil
courant, donc au curseur. Couvert par `test_open_transaction_hides_later_commits`.

Le curseur public est la chaîne `"<xid>-<id>"` (opaque pour les clients).

## Consommateurs nommés (dans hangar)

`consume_batch` traite un lot dans **une transaction** : verrou de la ligne
`event_cursors` (`FOR UPDATE SKIP LOCKED`), lecture, gestionnaire, avance du curseur.

- Les effets en base du gestionnaire et l'avance du curseur sont atomiques.
- Si le gestionnaire échoue, le lot est rejoué (`TWR-EVT-002`, stocké).
- Un seul réplica traite un consommateur à la fois ; les autres sautent sans erreur.
- **Règle** : un gestionnaire doit être court. Il met des jobs en file, il ne fait
  pas d'appels réseau.

## Flux SSE pour les clients (gate)

`GET /v1/events` (permission `events:read`), voir [api.md](api.md).

- Reprise par l'en-tête `Last-Event-ID` (EventSource l'envoie automatiquement) ou
  par `?after=<curseur>`. `after=0` : depuis le début ; défaut : depuis maintenant.
- Filtre `?topics=dossier.opened&topics=incident.opened`.
- Keepalive toutes les `TOWER_SSE_HEARTBEAT_SECONDS`.
- Aucune connexion SQL n'est tenue par client : une session courte par lot, réveil
  par la connexion LISTEN partagée du réplica.

## Topics existants

| Topic | Émis par | Payload |
|---|---|---|
| `dossier.opened` | cas d'usage `open_dossier` | état du dossier |
| `dossier.transitioned` | `transition`, `mark_published` | état + `from`, `to`, `reason` |
| `incident.opened` | reporter d'erreurs (sévérité ≥ error, nouvelle empreinte) | code, titre, sévérité, processus, lieu, message |
| `incident.reopened` | reporter (erreur résolue qui réapparaît) | idem |
| `incident.resolved` | `errors.service.resolve` | empreinte, code, note |
| `ping.requested` / `ping.done` | service ping | démonstration |
| `content.ingested` | cas d'usage `ingest_items`, **nouveau contenu seulement** (jamais par observation) | `id` (clé interne en chaîne), `public_id`, `platform`, `external_id`, `lang`, `source` |
| `watchlist.changed` | cas d'usage de la liste de suivi | `action` (`added`, `updated`), compte suivi (`public_id`, `platform`, `external_id`, `handle`, `category`, `priority`, `active`, `version`) |
| `extension_filters.published` | `publish_filters` | `version` |
| `budget.threshold_reached` | routeur de modèles, au premier franchissement de 50, 80 ou 100 % d'une portée sur la période | `scope`, `period_start`, `threshold_pct`, `spent_usd_micros`, `cap_usd_micros` |

Convention de nommage : `<agrégat>.<verbe au participe passé>`.

## Rétention

Purge des événements de plus de `TOWER_EVENT_RETENTION_DAYS` (30 jours) par la
maintenance. Un consommateur en retard de plus de 30 jours perdrait des événements :
surveiller `event_cursors.updated_at`.
