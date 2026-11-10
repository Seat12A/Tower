"""Configuration par variables d'environnement.

Toutes les variables sont préfixées par ``TOWER_``, sauf celles que Railway injecte
lui-même (``DATABASE_URL``, ``PORT``, ``RAILWAY_*``), lues telles quelles.
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
    """Nom du processus courant (gate, hangar…), fixé par la CLI, ajouté à chaque log."""

    # --- Base de données -------------------------------------------------------
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
    """``json`` (Railway) ou ``console`` (lisible en local). Par défaut : json hors dev."""

    # --- gate (API) ------------------------------------------------------------
    host: str = "::"
    """``::`` écoute en IPv4 et IPv6, requis par le réseau privé Railway."""
    port: int = Field(default=8000, validation_alias=AliasChoices("PORT", "TOWER_PORT"))
    auth_cache_ttl: float = Field(default=15.0, ge=0)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    sse_batch_size: int = Field(default=200, ge=1, le=1000)
    cors_origins: str = ""
    """Origines autorisées, séparées par des virgules (site public, extension)."""

    # --- hangar (worker) -------------------------------------------------------
    services: str = "ping"
    """Services activés dans ce processus, séparés par des virgules."""
    queues: str = ""
    """Files consommées, séparées par des virgules. Vide : toutes celles des services."""
    worker_concurrency: int = Field(default=4, ge=1)
    job_lease_seconds: int = Field(default=300, ge=5)
    job_poll_interval: float = Field(default=5.0, gt=0)
    job_retention_days: int = Field(default=7, ge=1)
    event_retention_days: int = Field(default=30, ge=1)
    error_retention_days: int = Field(default=90, ge=1)
    """Durée de conservation des erreurs *résolues* (les ouvertes sont gardées)."""
    error_flush_interval: float = Field(default=5.0, gt=0)
    shutdown_grace_seconds: float = Field(default=20.0, ge=0)

    # --- Railway (lecture seule) ----------------------------------------------
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

    # --- Valeurs dérivées ------------------------------------------------------
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
        """URL pour SQLAlchemy (pilote asyncpg), sans paramètres inconnus d'asyncpg."""
        scheme, rest = _normalized(self.database_url)
        return f"postgresql+asyncpg{rest}" if scheme else rest

    @property
    def asyncpg_dsn(self) -> str:
        """DSN brut pour une connexion asyncpg directe (LISTEN)."""
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
        """Identifiant lisible de l'instance (worker id, application_name)."""
        replica = (self.railway_replica_id or "local")[:8]
        return f"{self.process}-{replica}"


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _normalized(url: str) -> tuple[str, str]:
    """Renvoie (schéma d'origine, reste de l'URL à partir de ``://``), sans ``sslmode``."""
    parts = urlsplit(url)
    if parts.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        raise ValueError(f"URL PostgreSQL attendue, reçu le schéma {parts.scheme!r}")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "sslmode"])
    rebuilt = urlunsplit(("x", parts.netloc, parts.path, query, parts.fragment))
    return parts.scheme, rebuilt[1:]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
