"""Logs structurés, pensés pour Railway.

Railway analyse un log comme un objet si la ligne est un JSON **sur une seule ligne**
avec un champ ``message`` ; ``level`` doit valoir ``debug``, ``info``, ``warn`` ou
``error``. Les autres clés de premier niveau deviennent des attributs filtrables
(``@dossier_id:42``, ``@duration_ms:>500``). Tout part sur **stdout** : Railway
classe stderr en erreur et l'affiche en rouge.

La bibliothèque standard (uvicorn, SQLAlchemy, Alembic…) passe par le même rendu.
Limite Railway : 500 lignes par seconde et par réplica, au-delà les logs sont perdus.
Ne jamais logguer dans une boucle chaude ; ``/health`` n'est pas journalisé.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import orjson
import structlog
from structlog.types import EventDict, Processor, WrappedLogger

from tower.core.config import Settings

_RAILWAY_LEVELS = {
    "debug": "debug",
    "info": "info",
    "warning": "warn",
    "warn": "warn",
    "error": "error",
    "exception": "error",
    "critical": "error",
}

_NOISY = {
    "uvicorn.access": logging.WARNING,  # remplacé par notre middleware d'accès
    "sqlalchemy.engine": logging.WARNING,
    "asyncio": logging.WARNING,
}


def _railway_level(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    level = str(event.get("level", "info")).lower()
    event["level"] = _RAILWAY_LEVELS.get(level, "info")
    return event


def _drop_color_message(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    event.pop("color_message", None)  # doublon ajouté par uvicorn
    return event


def _orjson_dumps(obj: Any, **_: Any) -> str:
    return orjson.dumps(obj, default=str).decode()


def configure_logging(settings: Settings) -> None:
    """Configure structlog et la bibliothèque standard. Idempotent."""
    static: dict[str, Any] = {"process": settings.process}
    if settings.railway_replica_id:
        static["replica"] = settings.railway_replica_id[:8]

    def add_static(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
        for key, value in static.items():
            event.setdefault(key, value)
        return event

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        add_static,
        _drop_color_message,
    ]

    renderer: Processor
    if settings.effective_log_format == "json":
        renderer = structlog.processors.JSONRenderer(serializer=_orjson_dumps)
        final: list[Processor] = [
            structlog.processors.format_exc_info,  # trace dans "exception", sur une ligne
            structlog.processors.EventRenamer("message"),
            _railway_level,
            renderer,
        ]
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
        final = [renderer]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *final],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    for name, level in _NOISY.items():
        logging.getLogger(name).setLevel(max(level, root.level))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
