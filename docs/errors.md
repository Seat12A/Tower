# Catalogue des codes d'erreur

> Page générée par `tower errors catalog --markdown > docs/errors.md`.
> Ne pas éditer à la main : la source est `src/tower/core/domain/error_codes.py`.

Format : `TWR-<DOMAINE>-<NNN>`. Un code n'est jamais renuméroté ni réutilisé.
« Stocké » : l'occurrence est agrégée dans la table `error_reports`.

## ATL : ATL

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-ATL-001` | Knowledge source unavailable (network, timeout, 5xx): skipped | 500 | warning | oui | oui |  |
| `TWR-ATL-002` | Knowledge source rate limit reached: skipped | 500 | info | non | oui |  |
| `TWR-ATL-003` | Unexpected knowledge source response | 500 | error | oui | non |  |
| `TWR-ATL-004` | Knowledge source not configured (missing key): skipped | 500 | info | non | non | TOWER_BRAVE_API_KEY, TOWER_GOOGLE_API_KEY, TOWER_PISTE_CLIENT_ID/SECRET… |
| `TWR-ATL-005` | Knowledge source refused the credentials | 500 | error | oui | non |  |
| `TWR-ATL-006` | Recent Wikipedia revision with a high revert risk: last stable revision used | 500 | info | non | non |  |
| `TWR-ATL-007` | Search budget exhausted: web search skipped | 500 | warning | non | oui |  |
| `TWR-ATL-008` | LEGI mirror synchronization failed | 500 | error | oui | oui | Check that echanges.dila.gouv.fr answers; the next daily run resumes from the last applied archive (atlas_sync_state). |
| `TWR-ATL-009` | News feed could not be read: skipped until the next poll | 500 | warning | oui | oui |  |
| `TWR-ATL-010` | XML document rejected (entity declaration, external entity or size limit) | 500 | warning | oui | non |  |
| `TWR-ATL-011` | Statistical indicator returned no usable value | 500 | info | non | non |  |
| `TWR-ATL-012` | Registry entry not found | 404 | info | non | non |  |
| `TWR-ATL-013` | Invalid registry entry | 422 | info | non | non |  |
| `TWR-ATL-014` | Registry entry changed meanwhile (stale version) | 409 | info | non | non | Re-read the entry and resend with its current version. |
| `TWR-ATL-015` | Wikidata seeding of controlled_by failed | 500 | warning | oui | oui |  |

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

## FCK : FCK

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-FCK-001` | Extracted claim not faithful to its post (number or entity absent, omission) | 500 | info | non | non | The post is replayed alone; repeated failures leave it without claims. |
| `TWR-FCK-002` | Model output rejected by a pipeline check after validation | 500 | warning | oui | oui |  |
| `TWR-FCK-003` | Model budget exhausted: the investigation waits for the next period | 503 | warning | non | oui | Raise the level:N3/level:N4 or global cap with tower llm budget, or wait. |
| `TWR-FCK-004` | Per-investigation cost cap reached: no escalation, draft flagged | 500 | info | non | non |  |
| `TWR-FCK-005` | Every model of the route refused: the draft is flagged for the reviewer | 500 | warning | oui | non |  |
| `TWR-FCK-006` | Investigation stage failed | 500 | error | oui | oui |  |
| `TWR-FCK-007` | Draft not found | 404 | info | non | non |  |
| `TWR-FCK-008` | The draft was already decided | 409 | info | non | non |  |
| `TWR-FCK-009` | Invalid review (missing veracity or note for an edit, missing reason for a rejection) | 422 | info | non | non |  |
| `TWR-FCK-010` | Daily draft quota reached: the draft waits for a free slot | 500 | info | non | non | TOWER_FC_DRAFT_DAILY_QUOTA (50 by default). |
| `TWR-FCK-011` | Content not found on the platform | 404 | info | non | non |  |
| `TWR-FCK-012` | Unsupported or invalid content URL | 422 | info | non | non |  |
| `TWR-FCK-013` | No validated verdict for this content | 404 | info | non | non |  |
| `TWR-FCK-014` | Quote not found verbatim in its passage | 500 | warning | oui | non | The citation is dropped from the draft; stored for prompt analysis. |
| `TWR-FCK-015` | Post deleted or modified since its ingestion (recheck) | 500 | info | non | non |  |
| `TWR-FCK-016` | TOWER_LLM_FAKE is refused in production | 500 | critical | non | non | Unset TOWER_LLM_FAKE; the fake provider is for development and smoke tests only. |
| `TWR-FCK-017` | Note translation failed | 500 | warning | oui | oui |  |
| `TWR-FCK-018` | Content language not processed by the fact-check pipeline | 422 | info | non | non | TOWER_FC_LANGUAGES lists the processed languages. |
| `TWR-FCK-019` | Platform read failed during a manual request or a recheck | 503 | warning | oui | oui |  |
| `TWR-FCK-020` | Claim group not found | 404 | info | non | non |  |
| `TWR-FCK-021` | Claim group already merged into another group | 409 | info | non | non | Act on the group it was merged into (merged_into in the group). |
| `TWR-FCK-022` | Both groups hold active verdicts with different outcomes | 409 | info | non | non | Expire or reject one verdict first; a merge never overrides a verdict. |
| `TWR-FCK-023` | Only the target group of a merge may hold the active verdict | 409 | info | non | non | Merge into the group that holds the verdict. |
| `TWR-FCK-024` | Invalid split (claims not active members, or the group would be empty) | 422 | info | non | non |  |
| `TWR-FCK-025` | Reference fact not found | 404 | info | non | non |  |
| `TWR-FCK-026` | Invalid reference fact | 422 | info | non | non |  |
| `TWR-FCK-027` | Reference fact not validated by a reviewer | 409 | info | non | non | A reference fact serves only once validated (POST /v1/reference-facts/{id}/validate). |
| `TWR-FCK-028` | Value band cannot be validated (no validated fact, no band, or no outcome) | 422 | info | non | non |  |
| `TWR-FCK-029` | Processing profile version not found | 404 | info | non | non |  |
| `TWR-FCK-030` | Processing profile outside the safety bounds | 422 | info | non | non | See the bounds in docs/fact-check.md (Profils et niveaux). |
| `TWR-FCK-031` | Invalid level override | 422 | info | non | non |  |
| `TWR-FCK-032` | Level override not found | 404 | info | non | non |  |
| `TWR-FCK-033` | Embedding request failed | 500 | warning | oui | oui |  |
| `TWR-FCK-034` | Pair arbiter failed: the pair is treated as different | 500 | warning | oui | oui | Safe default: no grouping and no reuse without the arbiter. |
| `TWR-FCK-035` | PostgreSQL extension pgvector (>= 0.8) is not available | 500 | critical | non | non | Use a PostgreSQL image that ships pgvector (Railway: pgvector template). |
| `TWR-FCK-036` | Invalid E1 router model (weights, dimensions or metrics) | 422 | info | non | non |  |
| `TWR-FCK-037` | Verdict revalidation failed | 500 | warning | oui | oui |  |
| `TWR-FCK-038` | Automatic reference fact feed failed (INSEE) | 500 | warning | oui | oui |  |
| `TWR-FCK-039` | Batch extraction output invalid (missing or unknown post ids) | 500 | warning | oui | oui | Missing posts are replayed alone. |
| `TWR-FCK-040` | Batch extraction contamination detected (canary or foreign figure) | 500 | error | oui | non | The batch is replayed post by post; lower TOWER_FC_BATCH_SIZE if it repeats. |
| `TWR-FCK-041` | Light check (E5) failed: the group keeps its priority without signal | 500 | warning | oui | oui |  |
| `TWR-FCK-042` | Promotion not confirmed by the platform (metrics read failed or below threshold) | 500 | info | non | non |  |
| `TWR-FCK-043` | Narrative not found | 404 | info | non | non |  |
| `TWR-FCK-044` | Invalid narrative operation (closed narrative, same narrative, unknown groups) | 422 | info | non | non |  |
| `TWR-FCK-045` | Scheduler held work back: queue at capacity | 500 | info | non | non |  |
| `TWR-FCK-046` | Not enough reviewed data to fit a model (threshold not reached): nothing produced | 422 | info | non | non | Confidence: 200 reviews for the reduced model, 100 refusals for the full one; E1 distillation: 200 labelled cases. |
| `TWR-FCK-047` | Calibration model not found | 404 | info | non | non |  |
| `TWR-FCK-048` | Candidate model not activated: its measurement does not beat the current one | 409 | info | non | non |  |

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
| `TWR-ING-012` | Extension content contradicts the public embed of the post | 500 | warning | oui | non | Text or author declared by an installation differs from X: possible forged submission. The content is marked mismatch and never opens a targeted fact-check. |
| `TWR-ING-013` | Public embed (X oEmbed) unavailable | 500 | warning | oui | oui | Network error, 5xx or 403 from publish.x.com; the content stays unverified. |
| `TWR-ING-014` | Public embed (X oEmbed) rate limited: verification paused | 500 | warning | oui | oui | Lower TOWER_OEMBED_PER_MINUTE if it recurs. |

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

