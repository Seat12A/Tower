from __future__ import annotations

import socket

import uvicorn

from tower.core.config import Settings


def bind_socket(host: str, port: int) -> socket.socket | None:
    """A dual-stack socket for ``::`` (IPv4 and IPv6), ``None`` for any other host.
    Without IPv6 on the machine, an IPv4 socket on all interfaces.

    asyncio's ``create_server`` sets ``IPV6_V6ONLY`` on IPv6 sockets, so ``host="::"``
    alone would only accept IPv6: the Railway private network uses IPv6 while its
    healthcheck and public proxy connect over IPv4.
    """
    if host != "::":
        return None
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:  # no IPv6 support
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))  # noqa: S104 - same exposure as "::"
        return sock
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind((host, port))
    return sock


def serve(settings: Settings) -> None:
    from tower.gate.app import create_app

    config = uvicorn.Config(
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
    sock = bind_socket(settings.host, settings.port)
    uvicorn.Server(config).run(sockets=[sock] if sock is not None else None)
