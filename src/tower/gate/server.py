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
        log_config=None,  # our structured logs (tower.core.logging) stay in place
        access_log=False,  # replaced by AccessLogMiddleware (JSON, /health excluded)
        proxy_headers=True,
        forwarded_allow_ips="*",  # behind the Railway proxy
        timeout_graceful_shutdown=int(settings.shutdown_grace_seconds),
        server_header=False,
    )
