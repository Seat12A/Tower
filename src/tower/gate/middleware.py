"""Middleware ASGI pur (sans BaseHTTPMiddleware : moins coûteux, compatible SSE).

- ``request_id`` : repris de ``X-Request-ID`` ou généré, lié à tous les logs de la
  requête, renvoyé dans l'en-tête de réponse et dans les corps d'erreur.
- Une ligne de log JSON par requête (``/health`` et ``/ready`` exclus : le healthcheck
  Railway consommerait le quota de 500 lignes/s pour rien).
- Filet de sécurité : toute exception imprévue devient une réponse ``TWR-INT-001``
  stockée, au lieu d'une trace brute d'uvicorn.
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.core.logging import get_logger

log = get_logger("tower.gate.access")

_SILENT_PATHS = frozenset({"/health", "/ready"})


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        request_id = headers.get(b"x-request-id", b"").decode()[:64] or uuid.uuid4().hex[:16]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            if response_started:
                capture(ec.INT_UNEXPECTED, exc, http_path=scope.get("path"))
            else:
                from tower.gate.errors import unexpected_error_response

                await unexpected_error_response(exc)(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if path not in _SILENT_PATHS:
                duration_ms = round((time.perf_counter() - started) * 1000, 1)
                method = "warning" if status >= 500 else "info"
                getattr(log, method)(
                    f"{scope.get('method')} {path} {status}",
                    http_method=scope.get("method"),
                    http_path=path,
                    http_status=status,
                    duration_ms=duration_ms,
                )
            structlog.contextvars.unbind_contextvars("request_id")
