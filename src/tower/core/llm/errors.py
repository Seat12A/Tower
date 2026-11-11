"""Model router errors (stable ``TWR-LLM-*`` codes) and their translation for jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tower.core.domain import error_codes as ec
from tower.core.domain.error_codes import ErrorCode

__all__ = [
    "AllRefused",
    "BudgetExhausted",
    "LLMRouterError",
    "NoRoute",
    "NoValidOutput",
    "ProviderNotConfigured",
    "ProviderRejected",
    "ProviderUnavailable",
    "UnknownModel",
    "UnknownPrompt",
    "to_job_error",
]


class LLMRouterError(Exception):
    """Base of router errors: ``code`` is the catalogue entry, ``details`` is context."""

    default_code: ErrorCode = ec.LLM_PROVIDER_FATAL

    def __init__(self, message: str, *, code: ErrorCode | None = None, **details: Any) -> None:
        super().__init__(message)
        self.code: ErrorCode = code or self.default_code
        self.details: dict[str, Any] = dict(details)


class BudgetExhausted(LLMRouterError):
    default_code = ec.LLM_BUDGET_EXHAUSTED

    def __init__(self, scope: str, retry_at: datetime) -> None:
        super().__init__(
            f"LLM budget exhausted for scope {scope}", scope=scope, retry_at=retry_at.isoformat()
        )
        self.scope = scope
        self.retry_at = retry_at


class NoRoute(LLMRouterError):
    default_code = ec.LLM_NO_ROUTE


class UnknownPrompt(LLMRouterError):
    default_code = ec.LLM_UNKNOWN_PROMPT


class AllRefused(LLMRouterError):
    default_code = ec.LLM_ALL_REFUSED


class NoValidOutput(LLMRouterError):
    default_code = ec.LLM_INVALID_OUTPUT


class ProviderUnavailable(LLMRouterError):
    default_code = ec.LLM_PROVIDER_UNAVAILABLE

    def __init__(self, message: str, *, retry_after: float | None = None, **details: Any) -> None:
        super().__init__(message, retry_after=retry_after, **details)
        self.retry_after = retry_after


class ProviderRejected(LLMRouterError):
    default_code = ec.LLM_PROVIDER_FATAL


class ProviderNotConfigured(LLMRouterError):
    default_code = ec.LLM_PROVIDER_NOT_CONFIGURED


class UnknownModel(LLMRouterError):
    default_code = ec.LLM_UNKNOWN_MODEL


def to_job_error(exc: LLMRouterError) -> Exception:
    """Translate a router error into the exception a job handler should raise."""
    from tower.core.jobs.queue import PermanentError, RetryLater

    if isinstance(exc, BudgetExhausted):
        retry_at = exc.retry_at
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        delay = (retry_at - datetime.now(UTC)).total_seconds()
        return RetryLater(max(60.0, delay), str(exc))
    if isinstance(exc, ProviderUnavailable):
        return RetryLater(exc.retry_after or 60.0, str(exc))
    if isinstance(exc, NoValidOutput):
        return exc  # normal retry with backoff
    return PermanentError(str(exc))
