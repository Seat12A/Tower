"""Erreurs métier. Chacune porte un code du catalogue (``error_codes``).

gate les traduit en réponse HTTP ``{"error": {"code", "message", "details"}}``,
intercom en message Discord. Une sous-classe fixe un code par défaut ; un cas d'usage
peut en préciser un plus spécifique : ``InvalidInput("...", code=DOS_BAD_PRIORITY)``.
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
    """Modification concurrente : la version attendue ne correspond plus."""

    default_code = ec.GEN_CONFLICT


class InvalidInput(DomainError):
    default_code = ec.GEN_INVALID_INPUT
