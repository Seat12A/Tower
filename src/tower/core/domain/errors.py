"""Domain errors. Each carries a code from the catalog (``error_codes``).

gate translates them into HTTP responses ``{"error": {"code", "message", "details"}}``,
intercom into a Discord message. A subclass sets a default code; a use case
can specify a more specific one: ``InvalidInput("...", code=DOS_BAD_PRIORITY)``.
"""

from __future__ import annotations

from tower.core.domain import error_codes as ec
from tower.core.domain.error_codes import ErrorCode


class DomainError(Exception):
    default_code: ErrorCode = ec.GEN_INVALID_INPUT

    def __init__(self, message: str, *, code: ErrorCode | None = None, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = code or self.default_code
        self.details = details

    @property
    def code(self) -> str:
        return self.error_code.code

    @property
    def http_status(self) -> int:
        return self.error_code.http_status


class NotFound(DomainError):
    default_code = ec.GEN_NOT_FOUND


class PermissionDenied(DomainError):
    default_code = ec.AUTH_FORBIDDEN


class Unauthenticated(DomainError):
    default_code = ec.AUTH_INVALID_TOKEN


class InvalidTransition(DomainError):
    default_code = ec.DOS_INVALID_TRANSITION


class Conflict(DomainError):
    """Concurrent modification: the expected version no longer matches."""

    default_code = ec.GEN_CONFLICT


class InvalidInput(DomainError):
    default_code = ec.GEN_INVALID_INPUT
