from __future__ import annotations

import uvicorn

from tower.core.config import Settings


def serve(settings: Settings) -> None:
    from tower.gate.app import create_app

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        loop="uvloop",
        http="httptools",
        log_config=None,  # nos logs structurés (tower.core.logging) restent en place
        access_log=False,  # remplacé par AccessLogMiddleware (JSON, /health exclu)
        proxy_headers=True,
        forwarded_allow_ips="*",  # derrière le proxy Railway
        timeout_graceful_shutdown=int(settings.shutdown_grace_seconds),
        server_header=False,
    )
