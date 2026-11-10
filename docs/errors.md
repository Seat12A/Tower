# Catalogue des codes d'erreur

> Page générée par `tower errors catalog --markdown > docs/errors.md`.
> Ne pas éditer à la main : la source est `src/tower/core/domain/error_codes.py`.

Format : `TWR-<DOMAINE>-<NNN>`. Un code n'est jamais renuméroté ni réutilisé.
« Stocké » : l'occurrence est agrégée dans la table `error_reports`.

## AUTH : Authentification et autorisations

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-AUTH-001` | Service token invalid or revoked | 401 | warning | non | non |  |
| `TWR-AUTH-002` | X-Acting-Discord-Id provided without service token | 401 | warning | non | non |  |
| `TWR-AUTH-003` | X-Acting-Discord-Id invalid | 401 | warning | non | non |  |
| `TWR-AUTH-004` | Staff member unknown or inactive | 403 | warning | non | non | Add the member: tower staff <discord_id> --roles reviewer |
| `TWR-AUTH-005` | Missing permission | 403 | info | non | non |  |

## DB : Base de données

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-DB-001` | Database unavailable | 503 | critical | oui | oui |  |
| `TWR-DB-002` | LISTEN connection lost (reconnecting) | 500 | warning | oui | oui |  |

## DOS : Dossiers

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-DOS-001` | Dossier not found | 404 | info | non | non |  |
| `TWR-DOS-002` | Forbidden status transition | 409 | info | non | non |  |
| `TWR-DOS-003` | Invalid X publication link | 422 | info | non | non | Expected format: https://x.com/<account>/status/<id> |
| `TWR-DOS-004` | X publication link required | 422 | info | non | non |  |
| `TWR-DOS-005` | Publication link only accepted for published status | 422 | info | non | non |  |
| `TWR-DOS-006` | Priority out of bounds (-100..100) | 422 | info | non | non |  |

## EVT : Bus d'événements

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-EVT-001` | Invalid event cursor | 422 | info | non | non |  |
| `TWR-EVT-002` | Event consumer failed (batch replayed) | 500 | error | oui | oui | The cursor does not advance while the batch fails: fix the handler. |

## GEN : Général

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-GEN-001` | Invalid input | 422 | info | non | non |  |
| `TWR-GEN-002` | Resource not found | 404 | info | non | non |  |
| `TWR-GEN-003` | Concurrent modification (stale version) | 409 | info | non | non | Re-read the resource, then resend with the new version. |
| `TWR-GEN-004` | Too many requests | 429 | info | non | non |  |

## INT : Interne

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-INT-001` | Unexpected internal error | 500 | error | oui | non | Look up the fingerprint in error_reports or the request_id in Railway logs. |
| `TWR-INT-002` | Background task of a process stopped on error | 500 | critical | oui | non |  |

## JOB : File de travaux

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-JOB-001` | No handler for this job type | 500 | error | oui | non | The service is not enabled in TOWER_SERVICES on this replica. |
| `TWR-JOB-002` | Job failed after last attempt | 500 | error | oui | non |  |
| `TWR-JOB-003` | Job failed permanently (PermanentError) | 500 | error | oui | non |  |
| `TWR-JOB-004` | Lease expired: worker lost (OOM, hard kill) | 500 | warning | oui | oui | Check the replica's memory and drainingSeconds on Railway. |
| `TWR-JOB-005` | Job interrupted: execution timeout exceeded | 500 | warning | oui | oui |  |
| `TWR-JOB-006` | Job attempt failed, retry scheduled | 500 | warning | oui | oui |  |
| `TWR-JOB-007` | Unable to record job result | 500 | error | oui | non | The reaper will requeue the job when the lease expires. |

## MNT : Maintenance

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-MNT-001` | Maintenance task failed | 500 | error | oui | non |  |

