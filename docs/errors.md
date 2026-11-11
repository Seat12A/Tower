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

## BOT : BOT

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-BOT-001` | Gate unreachable from the bot | 500 | warning | non | oui |  |
| `TWR-BOT-002` | Event stream interrupted, resuming from the cursor | 500 | warning | non | oui |  |
| `TWR-BOT-003` | Discord message could not be delivered | 500 | warning | non | oui |  |
| `TWR-BOT-004` | Bot configuration missing or invalid | 500 | critical | non | non | TOWER_DISCORD_TOKEN, TOWER_GATE_URL and TOWER_GATE_TOKEN are required. |

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
| `TWR-EVT-003` | Invalid client cursor name (1-64 chars: a-z, 0-9, '.', '_', '-') | 422 | info | non | non |  |

## GEN : Général

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-GEN-001` | Invalid input | 422 | info | non | non |  |
| `TWR-GEN-002` | Resource not found | 404 | info | non | non |  |
| `TWR-GEN-003` | Concurrent modification (stale version) | 409 | info | non | non | Re-read the resource, then resend with the new version. |
| `TWR-GEN-004` | Too many requests | 429 | info | non | non |  |

## ING : ING

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-ING-001` | Invalid installation token | 401 | info | non | non | X-Installation-Token: base64url, 22 to 64 characters. |
| `TWR-ING-002` | Daily ingestion quota exceeded for this installation | 429 | info | non | non | The quota resets at 00:00 UTC. |
| `TWR-ING-003` | Too many items in the batch | 422 | info | non | non |  |
| `TWR-ING-004` | Request body too large | 413 | info | non | non |  |
| `TWR-ING-005` | Unknown platform | 422 | info | non | non |  |
| `TWR-ING-006` | Invalid platform identifier | 422 | info | non | non |  |
| `TWR-ING-007` | Too many ingestion requests | 429 | info | non | non |  |
| `TWR-ING-008` | Watched account not found | 404 | info | non | non |  |
| `TWR-ING-009` | Account is already on the watchlist | 409 | info | non | non |  |
| `TWR-ING-010` | No observations partition for this timestamp | 503 | error | oui | oui | The ingest service creates partitions ahead; check that a hangar runs it. |
| `TWR-ING-011` | Invalid extension filter definition | 422 | info | non | non |  |

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

## LLM : LLM

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-LLM-001` | LLM budget exhausted for the current period | 503 | warning | non | oui | Jobs retry at the start of the next period; raise the cap in budget_counters if needed. |
| `TWR-LLM-002` | No active model route for this role and level | 500 | error | oui | non | Run `tower llm seed` or add a route in llm_routes. |
| `TWR-LLM-003` | Unknown prompt id | 500 | error | oui | non | Add src/tower/services/<service>/prompts/<prompt_id>.md. |
| `TWR-LLM-004` | Every model of the route refused the request | 422 | warning | oui | non | Sensitive topic: flag the item for a human reviewer. |
| `TWR-LLM-005` | No schema-valid output from any model of the route | 500 | error | oui | oui | Check the prompt and the schema; compare the stored outputs in llm_calls. |
| `TWR-LLM-006` | Model provider temporarily unavailable (rate limit, timeout, 5xx) | 503 | warning | non | oui |  |
| `TWR-LLM-007` | Model provider rejected the request (authentication, bad request) | 500 | error | oui | non | Check the API key and the request parameters of the route. |
| `TWR-LLM-008` | No API key configured for the provider required by the route | 500 | error | oui | non | Set TOWER_OPENAI_API_KEY, TOWER_OPENAI_API_KEY_SHARED or TOWER_ANTHROPIC_API_KEY. |
| `TWR-LLM-009` | Route references an unknown or inactive model | 500 | error | oui | non | Run `tower llm seed` or fix llm_routes. |

## MNT : Maintenance

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-MNT-001` | Maintenance task failed | 500 | error | oui | non |  |
| `TWR-MNT-002` | Expired budget reservations released | 500 | warning | oui | non | A process died between reserving and settling a cost; the hold was released. |

## RAD : RAD

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-RAD-001` | Platform API refused the credentials (401/403) | 502 | error | oui | non | Check TOWER_X_BEARER_TOKEN and the app's access level on the developer portal. |
| `TWR-RAD-002` | Platform API rate limit reached (429) | 503 | info | non | oui | The job waits until the reset time given by the API. |
| `TWR-RAD-003` | Platform API unavailable (5xx, network, timeout) | 503 | warning | oui | oui |  |
| `TWR-RAD-004` | Unexpected platform API response | 502 | error | oui | non |  |
| `TWR-RAD-005` | Platform read budget exhausted for the period | 503 | warning | non | oui | Reads resume next period, or raise the provider:x cap. |
| `TWR-RAD-006` | Watched account not found on the platform | 404 | warning | oui | non | Fix the handle with /watchlist edit, or deactivate the account. |
| `TWR-RAD-007` | Platform API credentials not configured | 500 | error | oui | non | Set TOWER_X_BEARER_TOKEN on the hangar replica running radar. |

## WEB : WEB

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-WEB-001` | URL refused (scheme, port or non-public address) | 422 | warning | non | non | SSRF protection: only public http(s) addresses are fetched. |
| `TWR-WEB-002` | Fetched content exceeds the size cap | 500 | info | non | non |  |
| `TWR-WEB-003` | Content type not allowed | 500 | info | non | non |  |
| `TWR-WEB-004` | Disallowed by robots.txt | 500 | info | non | non |  |
| `TWR-WEB-005` | Fetch failed (network, timeout, too many redirects, HTTP error) | 500 | warning | non | oui |  |

