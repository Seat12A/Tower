"""Configuration via environment variables.

All variables are prefixed with ``TOWER_``, except those Railway injects itself
(``DATABASE_URL``, ``PORT``, ``RAILWAY_*``), which are read as-is.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["dev", "test", "prod"]
LogFormat = Literal["json", "console"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TOWER_", env_file=".env", extra="ignore", frozen=True
    )

    env: Env = "dev"
    process: str = "tower"
    """Name of the current process (gate, hangar…), set by the CLI and added to every log."""

    # --- Database-------------------------------------------------------
    database_url: str = Field(
        default="postgresql://postgres@localhost:5432/tower",
        validation_alias=AliasChoices("TOWER_DATABASE_URL", "DATABASE_URL"),
    )
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=0, ge=0)
    db_pool_timeout: float = Field(default=10.0, gt=0)
    db_statement_timeout_ms: int = Field(default=30_000, ge=0)

    # --- Logs ------------------------------------------------------------------
    log_level: str = "info"
    log_format: LogFormat | None = None
    """``json`` (Railway) or ``console`` (human-readable, local). Default: json outside dev."""

    # --- gate (API) ------------------------------------------------------------
    host: str = "::"
    """``::`` listens on IPv4 and IPv6, required by Railway's private network."""
    port: int = Field(default=8000, validation_alias=AliasChoices("PORT", "TOWER_PORT"))
    auth_cache_ttl: float = Field(default=15.0, ge=0)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    sse_batch_size: int = Field(default=200, ge=1, le=1000)
    cors_origins: str = ""
    """Allowed origins, comma-separated (public site, extension)."""

    # --- hangar (worker) -------------------------------------------------------
    services: str = "ping"
    """Services enabled in this process, comma-separated."""
    queues: str = ""
    """Queues consumed, comma-separated. Empty: all queues of the enabled services."""
    worker_concurrency: int = Field(default=4, ge=1)
    job_lease_seconds: int = Field(default=300, ge=5)
    job_poll_interval: float = Field(default=5.0, gt=0)
    job_retention_days: int = Field(default=7, ge=1)
    event_retention_days: int = Field(default=30, ge=1)
    error_retention_days: int = Field(default=90, ge=1)
    """Retention period for *resolved* errors (open ones are kept)."""
    error_flush_interval: float = Field(default=5.0, gt=0)
    shutdown_grace_seconds: float = Field(default=20.0, ge=0)

    # --- Railway (read-only)----------------------------------------------
    railway_replica_id: str | None = Field(
        default=None, validation_alias=AliasChoices("RAILWAY_REPLICA_ID")
    )
    railway_deployment_id: str | None = Field(
        default=None, validation_alias=AliasChoices("RAILWAY_DEPLOYMENT_ID")
    )

    @field_validator("log_level")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()

    # --- Derived values------------------------------------------------------
    @property
    def effective_log_format(self) -> LogFormat:
        if self.log_format is not None:
            return self.log_format
        return "console" if self.env == "dev" else "json"

    @property
    def service_names(self) -> list[str]:
        return _split(self.services)

    @property
    def queue_names(self) -> list[str]:
        return _split(self.queues)

    @property
    def cors_origin_list(self) -> list[str]:
        return _split(self.cors_origins)

    @property
    def sqlalchemy_url(self) -> str:
        """URL for SQLAlchemy (asyncpg driver), without parameters asyncpg does not know."""
        scheme, rest = _normalized(self.database_url)
        return f"postgresql+asyncpg{rest}" if scheme else rest

    @property
    def asyncpg_dsn(self) -> str:
        """Raw DSN for a direct asyncpg connection (LISTEN)."""
        _, rest = _normalized(self.database_url)
        return f"postgresql{rest}"

    @property
    def db_ssl(self) -> bool | None:
        mode = dict(parse_qsl(urlsplit(self.database_url).query)).get("sslmode")
        if mode is None:
            return None
        return mode not in {"disable", "allow"}

    @property
    def instance_id(self) -> str:
        """Human-readable instance identifier (worker id, application_name)."""
        replica = (self.railway_replica_id or "local")[:8]
        return f"{self.process}-{replica}"


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _normalized(url: str) -> tuple[str, str]:
    """Return (original scheme, rest of the URL from ``://``), without ``sslmode``."""
    parts = urlsplit(url)
    if parts.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        raise ValueError(f"Expected a PostgreSQL URL, got scheme {parts.scheme!r}")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "sslmode"])
    rebuilt = urlunsplit(("x", parts.netloc, parts.path, query, parts.fragment))
    return parts.scheme, rebuilt[1:]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
