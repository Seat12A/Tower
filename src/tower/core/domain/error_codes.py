"""Catalog of the Tower's error codes.

Format: ``TWR-<DOMAIN>-<NNN>``. A code is **stable**: it is never renumbered or reused
(clients, the bot and log searches rely on it). To retire a code, mark it
``deprecated`` without deleting it.

- ``http_status``: status returned by gate when the error reaches the API.
- ``severity``: log level and alert threshold (≥ error: published incident).
- ``store``: the occurrence is recorded in ``error_reports`` (aggregated by fingerprint).
  Client errors (4xx) are not stored: they are only logged.

The page ``docs/errors.md`` is generated from this file (``tower errors catalog``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return ["info", "warning", "error", "critical"].index(self.value)


@dataclass(frozen=True, slots=True)
class ErrorCode:
    code: str
    title: str
    http_status: int = 500
    severity: Severity = Severity.ERROR
    store: bool = False
    retryable: bool = False
    hint: str = ""
    deprecated: bool = False

    def __str__(self) -> str:
        return self.code


CATALOG: dict[str, ErrorCode] = {}


def _define(code: str, title: str, **kwargs: object) -> ErrorCode:
    if code in CATALOG:
        raise ValueError(f"Duplicate error code: {code}")
    entry = ErrorCode(code, title, **kwargs)  # type: ignore[arg-type]
    CATALOG[code] = entry
    return entry


_INFO, _WARN, _ERR, _CRIT = Severity.INFO, Severity.WARNING, Severity.ERROR, Severity.CRITICAL

# --- GEN: general --------------------------------------------------------------
GEN_INVALID_INPUT = _define("TWR-GEN-001", "Invalid input", http_status=422, severity=_INFO)
GEN_NOT_FOUND = _define("TWR-GEN-002", "Resource not found", http_status=404, severity=_INFO)
GEN_CONFLICT = _define(
    "TWR-GEN-003",
    "Concurrent modification (stale version)",
    http_status=409,
    severity=_INFO,
    hint="Re-read the resource, then resend with the new version.",
)
GEN_RATE_LIMITED = _define("TWR-GEN-004", "Too many requests", http_status=429, severity=_INFO)

# --- AUTH: authentication and authorization ----------------------------------
AUTH_INVALID_TOKEN = _define(
    "TWR-AUTH-001", "Service token invalid or revoked", http_status=401, severity=_WARN
)
AUTH_ACTING_WITHOUT_TOKEN = _define(
    "TWR-AUTH-002",
    "X-Acting-Discord-Id provided without service token",
    http_status=401,
    severity=_WARN,
)
AUTH_BAD_ACTING_ID = _define(
    "TWR-AUTH-003", "X-Acting-Discord-Id invalid", http_status=401, severity=_WARN
)
AUTH_UNKNOWN_STAFF = _define(
    "TWR-AUTH-004",
    "Staff member unknown or inactive",
    http_status=403,
    severity=_WARN,
    hint="Add the member: tower staff <discord_id> --roles reviewer",
)
AUTH_FORBIDDEN = _define("TWR-AUTH-005", "Missing permission", http_status=403, severity=_INFO)

# --- DOS: dossiers ------------------------------------------------------------
DOS_NOT_FOUND = _define("TWR-DOS-001", "Dossier not found", http_status=404, severity=_INFO)
DOS_INVALID_TRANSITION = _define(
    "TWR-DOS-002", "Forbidden status transition", http_status=409, severity=_INFO
)
DOS_BAD_PUBLICATION_URL = _define(
    "TWR-DOS-003",
    "Invalid X publication link",
    http_status=422,
    severity=_INFO,
    hint="Expected format: https://x.com/<account>/status/<id>",
)
DOS_PUBLICATION_URL_REQUIRED = _define(
    "TWR-DOS-004", "X publication link required", http_status=422, severity=_INFO
)
DOS_PUBLICATION_URL_UNEXPECTED = _define(
    "TWR-DOS-005",
    "Publication link only accepted for published status",
    http_status=422,
    severity=_INFO,
)
DOS_BAD_PRIORITY = _define(
    "TWR-DOS-006", "Priority out of bounds (-100..100)", http_status=422, severity=_INFO
)

# --- EVT: event bus ----------------------------------------------------
EVT_BAD_CURSOR = _define("TWR-EVT-001", "Invalid event cursor", http_status=422, severity=_INFO)
EVT_CONSUMER_FAILED = _define(
    "TWR-EVT-002",
    "Event consumer failed (batch replayed)",
    severity=_ERR,
    store=True,
    retryable=True,
    hint="The cursor does not advance while the batch fails: fix the handler.",
)
EVT_BAD_CURSOR_NAME = _define(
    "TWR-EVT-003",
    "Invalid client cursor name (1-64 chars: a-z, 0-9, '.', '_', '-')",
    http_status=422,
    severity=_INFO,
)

# --- JOB: job queue -----------------------------------------------------
JOB_UNKNOWN_KIND = _define(
    "TWR-JOB-001",
    "No handler for this job type",
    severity=_ERR,
    store=True,
    hint="The service is not enabled in TOWER_SERVICES on this replica.",
)
JOB_EXHAUSTED = _define("TWR-JOB-002", "Job failed after last attempt", severity=_ERR, store=True)
JOB_PERMANENT = _define(
    "TWR-JOB-003", "Job failed permanently (PermanentError)", severity=_ERR, store=True
)
JOB_LEASE_EXPIRED = _define(
    "TWR-JOB-004",
    "Lease expired: worker lost (OOM, hard kill)",
    severity=_WARN,
    store=True,
    retryable=True,
    hint="Check the replica's memory and drainingSeconds on Railway.",
)
JOB_TIMEOUT = _define(
    "TWR-JOB-005",
    "Job interrupted: execution timeout exceeded",
    severity=_WARN,
    store=True,
    retryable=True,
)
JOB_ATTEMPT_FAILED = _define(
    "TWR-JOB-006",
    "Job attempt failed, retry scheduled",
    severity=_WARN,
    store=True,
    retryable=True,
)
JOB_STATUS_UPDATE_FAILED = _define(
    "TWR-JOB-007",
    "Unable to record job result",
    severity=_ERR,
    store=True,
    hint="The reaper will requeue the job when the lease expires.",
)

# --- DB: database ------------------------------------------------------
DB_UNAVAILABLE = _define(
    "TWR-DB-001",
    "Database unavailable",
    http_status=503,
    severity=_CRIT,
    store=True,
    retryable=True,
)
DB_LISTENER_DOWN = _define(
    "TWR-DB-002",
    "LISTEN connection lost (reconnecting)",
    severity=_WARN,
    store=True,
    retryable=True,
)

# --- MNT: maintenance ---------------------------------------------------------
MNT_TASK_FAILED = _define("TWR-MNT-001", "Maintenance task failed", severity=_ERR, store=True)
MNT_RESERVATIONS_RELEASED = _define(
    "TWR-MNT-002",
    "Expired budget reservations released",
    severity=_WARN,
    store=True,
    hint="A process died between reserving and settling a cost; the hold was released.",
)

# --- INT: internal ----------------------------------------------------------------
INT_UNEXPECTED = _define(
    "TWR-INT-001",
    "Unexpected internal error",
    severity=_ERR,
    store=True,
    hint="Look up the fingerprint in error_reports or the request_id in Railway logs.",
)
INT_SERVICE_CRASHED = _define(
    "TWR-INT-002", "Background task of a process stopped on error", severity=_CRIT, store=True
)

# --- LLM: model router and providers ----------------------------------------------
LLM_BUDGET_EXHAUSTED = _define(
    "TWR-LLM-001",
    "LLM budget exhausted for the current period",
    http_status=503,
    severity=_WARN,
    retryable=True,
    hint="Jobs retry at the start of the next period; raise the cap in budget_counters if needed.",
)
LLM_NO_ROUTE = _define(
    "TWR-LLM-002",
    "No active model route for this role and level",
    severity=_ERR,
    store=True,
    hint="Run `tower llm seed` or add a route in llm_routes.",
)
LLM_UNKNOWN_PROMPT = _define(
    "TWR-LLM-003",
    "Unknown prompt id",
    severity=_ERR,
    store=True,
    hint="Add src/tower/services/<service>/prompts/<prompt_id>.md.",
)
LLM_ALL_REFUSED = _define(
    "TWR-LLM-004",
    "Every model of the route refused the request",
    http_status=422,
    severity=_WARN,
    store=True,
    hint="Sensitive topic: flag the item for a human reviewer.",
)
LLM_INVALID_OUTPUT = _define(
    "TWR-LLM-005",
    "No schema-valid output from any model of the route",
    severity=_ERR,
    store=True,
    retryable=True,
    hint="Check the prompt and the schema; compare the stored outputs in llm_calls.",
)
LLM_PROVIDER_UNAVAILABLE = _define(
    "TWR-LLM-006",
    "Model provider temporarily unavailable (rate limit, timeout, 5xx)",
    http_status=503,
    severity=_WARN,
    retryable=True,
)
LLM_PROVIDER_FATAL = _define(
    "TWR-LLM-007",
    "Model provider rejected the request (authentication, bad request)",
    severity=_ERR,
    store=True,
    hint="Check the API key and the request parameters of the route.",
)
LLM_PROVIDER_NOT_CONFIGURED = _define(
    "TWR-LLM-008",
    "No API key configured for the provider required by the route",
    severity=_ERR,
    store=True,
    hint="Set TOWER_OPENAI_API_KEY, TOWER_OPENAI_API_KEY_SHARED or TOWER_ANTHROPIC_API_KEY.",
)
LLM_UNKNOWN_MODEL = _define(
    "TWR-LLM-009",
    "Route references an unknown or inactive model",
    severity=_ERR,
    store=True,
    hint="Run `tower llm seed` or fix llm_routes.",
)

# --- ING: ingestion ------------------------------------------------------------------
ING_INVALID_INSTALLATION_TOKEN = _define(
    "TWR-ING-001",
    "Invalid installation token",
    http_status=401,
    severity=_INFO,
    hint="X-Installation-Token: base64url, 22 to 64 characters.",
)
ING_QUOTA_EXCEEDED = _define(
    "TWR-ING-002",
    "Daily ingestion quota exceeded for this installation",
    http_status=429,
    severity=_INFO,
    hint="The quota resets at 00:00 UTC.",
)
ING_BATCH_TOO_LARGE = _define(
    "TWR-ING-003", "Too many items in the batch", http_status=422, severity=_INFO
)
ING_BODY_TOO_LARGE = _define(
    "TWR-ING-004", "Request body too large", http_status=413, severity=_INFO
)
ING_UNKNOWN_PLATFORM = _define("TWR-ING-005", "Unknown platform", http_status=422, severity=_INFO)
ING_BAD_EXTERNAL_ID = _define(
    "TWR-ING-006", "Invalid platform identifier", http_status=422, severity=_INFO
)
ING_RATE_LIMITED = _define(
    "TWR-ING-007", "Too many ingestion requests", http_status=429, severity=_INFO
)
ING_WATCHED_NOT_FOUND = _define(
    "TWR-ING-008", "Watched account not found", http_status=404, severity=_INFO
)
ING_WATCHED_EXISTS = _define(
    "TWR-ING-009", "Account is already on the watchlist", http_status=409, severity=_INFO
)
ING_PARTITION_MISSING = _define(
    "TWR-ING-010",
    "No observations partition for this timestamp",
    http_status=503,
    severity=_ERR,
    store=True,
    retryable=True,
    hint="The ingest service creates partitions ahead; check that a hangar runs it.",
)
ING_BAD_FILTERS = _define(
    "TWR-ING-011", "Invalid extension filter definition", http_status=422, severity=_INFO
)

# --- WEB: outbound HTTP fetches ------------------------------------------------------
WEB_BLOCKED_URL = _define(
    "TWR-WEB-001",
    "URL refused (scheme, port or non-public address)",
    http_status=422,
    severity=_WARN,
    hint="SSRF protection: only public http(s) addresses are fetched.",
)
WEB_TOO_LARGE = _define("TWR-WEB-002", "Fetched content exceeds the size cap", severity=_INFO)
WEB_BAD_CONTENT_TYPE = _define("TWR-WEB-003", "Content type not allowed", severity=_INFO)
WEB_ROBOTS_DISALLOWED = _define("TWR-WEB-004", "Disallowed by robots.txt", severity=_INFO)
WEB_FETCH_FAILED = _define(
    "TWR-WEB-005",
    "Fetch failed (network, timeout, too many redirects, HTTP error)",
    severity=_WARN,
    retryable=True,
)

# --- RAD: radar (official platform APIs) ---------------------------------------------
RAD_AUTH_FAILED = _define(
    "TWR-RAD-001",
    "Platform API refused the credentials (401/403)",
    http_status=502,
    severity=_ERR,
    store=True,
    hint="Check TOWER_X_BEARER_TOKEN and the app's access level on the developer portal.",
)
RAD_RATE_LIMITED = _define(
    "TWR-RAD-002",
    "Platform API rate limit reached (429)",
    http_status=503,
    severity=_INFO,
    retryable=True,
    hint="The job waits until the reset time given by the API.",
)
RAD_UNAVAILABLE = _define(
    "TWR-RAD-003",
    "Platform API unavailable (5xx, network, timeout)",
    http_status=503,
    severity=_WARN,
    store=True,
    retryable=True,
)
RAD_BAD_RESPONSE = _define(
    "TWR-RAD-004",
    "Unexpected platform API response",
    http_status=502,
    severity=_ERR,
    store=True,
)
RAD_BUDGET_EXHAUSTED = _define(
    "TWR-RAD-005",
    "Platform read budget exhausted for the period",
    http_status=503,
    severity=_WARN,
    retryable=True,
    hint="Reads resume next period, or raise the provider:x cap.",
)
RAD_ACCOUNT_NOT_FOUND = _define(
    "TWR-RAD-006",
    "Watched account not found on the platform",
    http_status=404,
    severity=_WARN,
    store=True,
    hint="Fix the handle with /watchlist edit, or deactivate the account.",
)
RAD_NOT_CONFIGURED = _define(
    "TWR-RAD-007",
    "Platform API credentials not configured",
    severity=_ERR,
    store=True,
    hint="Set TOWER_X_BEARER_TOKEN on the hangar replica running radar.",
)

# --- BOT: intercom (Discord bot; logged by the bot, which has no database) -----------
BOT_GATE_UNREACHABLE = _define(
    "TWR-BOT-001", "Gate unreachable from the bot", severity=_WARN, retryable=True
)
BOT_STREAM_INTERRUPTED = _define(
    "TWR-BOT-002",
    "Event stream interrupted, resuming from the cursor",
    severity=_WARN,
    retryable=True,
)
BOT_DELIVERY_FAILED = _define(
    "TWR-BOT-003", "Discord message could not be delivered", severity=_WARN, retryable=True
)
BOT_BAD_CONFIG = _define(
    "TWR-BOT-004",
    "Bot configuration missing or invalid",
    severity=_CRIT,
    hint="TOWER_DISCORD_TOKEN, TOWER_GATE_URL and TOWER_GATE_TOKEN are required.",
)


def get(code: str) -> ErrorCode:
    return CATALOG[code]
