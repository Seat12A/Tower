# File de travaux et worker hangar

Sources : `src/tower/core/jobs/`, `src/tower/hangar/`.

## Mettre un job en file

```python
from tower.core.jobs import enqueue

async with db.transaction() as session:
    await enqueue(session, "default", "factcheck.triage", {"post_id": "…"},
                  priority=5, delay=0, dedupe_key="triage:1850…", max_attempts=5)
```

- Transactionnel : le job n'existe que si la transaction valide.
- `dedupe_key` : unique **parmi les jobs actifs** (`queued`, `running`). Renvoie
  `None` si un doublon est actif ; une fois le job terminé, la clé redevient libre.
- `pg_notify('tower_jobs', queue)` réveille les workers immédiatement.

## Cycle d'un job

```
queued ──claim──▶ running ──▶ succeeded
   ▲                 │
   └── retry/backoff ┤──▶ failed      (dernière tentative, PermanentError, type inconnu)
   └── reaper (bail expiré)
```

- **Réservation** : `FOR UPDATE SKIP LOCKED` par lot, tri priorité décroissante puis
  `run_at`. Pose un bail `locked_until = now() + TOWER_JOB_LEASE_SECONDS`.
- **Succès** : `succeeded`.
- **Exception** : nouvelle tentative avec backoff exponentiel plafonné avec gigue
  (5 s, 10 s, 20 s… ≤ 1 h). À la dernière tentative : `failed`. Codes : `TWR-JOB-006`
  (tentative), `TWR-JOB-005` (timeout), `TWR-JOB-002` (épuisé).
- **`RetryLater(delay)`** : reprogrammé sans consommer de tentative (ex. quota API).
- **`PermanentError`** : `failed` immédiatement (`TWR-JOB-003`), ex. post supprimé.
- **Type inconnu** : `failed` (`TWR-JOB-001`), service non activé sur ce réplica.
- **Arrêt (SIGTERM)** : hangar arrête de réserver, laisse
  `TOWER_SHUTDOWN_GRACE_SECONDS` aux jobs en cours, puis annule le reste et le remet en
  file **sans consommer de tentative**.
- **Worker tué** (OOM, SIGKILL) : le bail expire, le reaper remet le job en file
  (`TWR-JOB-004`) ou en échec si les tentatives sont épuisées.

`jobs.last_error` commence toujours par le code du catalogue.

## Timeout

`JobSpec.timeout` (sinon 90 % du bail). Il doit rester **inférieur au bail** : vérifié
au démarrage de hangar (`ServiceSet.validate`).

## Idempotence

Un job peut être exécuté plus d'une fois (bail expiré pendant un travail lent,
crash après l'effet mais avant l'enregistrement). Tout handler doit être
**idempotent** : clés naturelles, `ON CONFLICT DO NOTHING`, vérification d'état.

## Maintenance (singleton)

Chaque réplica hangar tente, sous `pg_try_advisory_xact_lock` (un seul l'exécute) :

| Tâche | Fréquence | Effet |
|---|---|---|
| `tower:jobs:reap` | 30 s | Récupère les baux expirés |
| `tower:retention:purge` | 1 h | Purge jobs terminés, outbox ancienne, erreurs résolues anciennes |

## Mise à l'échelle

- Plus de débit sur une file : augmenter les réplicas hangar ou `TOWER_WORKER_CONCURRENCY`.
- Isoler une charge (ex. embeddings gourmands en RAM, enquêtes lentes) : un service
  Railway hangar dédié avec `TOWER_SERVICES=factcheck` et `TOWER_QUEUES=deep`.
- Surveiller : jobs `queued` dont `run_at` est ancien (retard), taux de `failed`.

```sql
SELECT queue, count(*) FILTER (WHERE status = 'queued') AS en_file,
       max(now() - run_at) FILTER (WHERE status = 'queued') AS retard_max,
       count(*) FILTER (WHERE status = 'failed') AS echecs
FROM jobs GROUP BY queue;
```
