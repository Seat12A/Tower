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


def get(code: str) -> ErrorCode:
    return CATALOG[code]
