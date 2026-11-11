"""Safe HTTP fetcher: SSRF protection, DNS pinning, robots.txt, size cap.

``httpx`` is imported lazily (inside ``__init__``): importing this module stays cheap.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import ipaddress
import socket
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import SplitResult, urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from tower.core.domain import error_codes as ec
from tower.core.domain.net import is_public_ip
from tower.core.ports.web import FetchedPage, FetchError

if TYPE_CHECKING:
    import httpx

Resolver = Callable[[str, int], Awaitable[list[str]]]

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_TEXT_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "application/json",
        "application/xml",
        "text/xml",
        "application/rss+xml",
        "application/atom+xml",
    }
)
_BINARY_TYPES = frozenset({"application/pdf"})
_ALLOWED_TYPES = _TEXT_TYPES | _BINARY_TYPES
_ACCEPT = ", ".join(sorted(_ALLOWED_TYPES))
_ROBOTS_MAX_BYTES = 512 * 1024
_ROBOTS_FAILURE_TTL = 600.0
_DEFAULT_PORTS = {"http": 80, "https": 443}


async def _default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


@dataclass(frozen=True, slots=True)
class _Target:
    url: str
    scheme: str
    host: str  # ASCII hostname without brackets
    port: int
    ips: list[str]

    @property
    def origin(self) -> tuple[str, str, int]:
        return (self.scheme, self.host, self.port)


@dataclass(slots=True)
class _RobotsEntry:
    expires: float
    parser: RobotFileParser | None  # None → everything disallowed
    allow_all: bool = False


@dataclass(frozen=True, slots=True)
class _Raw:
    url: str
    status: int
    content_type: str
    charset: str | None
    body: bytes


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _parse_content_type(value: str) -> tuple[str, str | None]:
    main, _, params = value.partition(";")
    charset: str | None = None
    for param in params.split(";"):
        key, _, val = param.partition("=")
        if key.strip().lower() == "charset":
            charset = val.strip().strip("\"'") or None
    return main.strip().lower(), charset


def _decode(body: bytes, charset: str | None) -> str:
    encoding = "utf-8"
    if charset:
        try:
            encoding = codecs.lookup(charset).name
        except LookupError:
            encoding = "utf-8"
    return body.decode(encoding, errors="replace")


class HttpFetcher:
    """``WebFetcher`` over httpx with SSRF, robots.txt and size protections."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 15.0,
        max_bytes: int = 2_000_000,
        max_redirects: int = 5,
        robots_cache_size: int = 256,
        robots_ttl: float = 86_400.0,
        resolver: Resolver | None = None,
        transport: Any = None,
    ) -> None:
        import httpx

        self._httpx = httpx
        self._user_agent = user_agent
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._robots_cache_size = robots_cache_size
        self._robots_ttl = robots_ttl
        self._resolver: Resolver = resolver or _default_resolver
        self._robots: OrderedDict[tuple[str, str, int], _RobotsEntry] = OrderedDict()
        self._client = httpx.AsyncClient(
            transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- validation -------------------------------------------------------------

    @staticmethod
    def _blocked(message: str, url: str) -> FetchError:
        return FetchError(message, code=ec.WEB_BLOCKED_URL, url=url)

    async def _validate(self, url: str) -> _Target:
        try:
            parts: SplitResult = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise self._blocked("Malformed URL", url) from exc
        if parts.scheme not in _DEFAULT_PORTS:
            raise self._blocked("Only http and https are allowed", url)
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise self._blocked("Credentials in the URL are not allowed", url)
        raw_host = parts.hostname
        if not raw_host:
            raise self._blocked("URL has no host", url)
        port = port if port is not None else _DEFAULT_PORTS[parts.scheme]
        if port not in (80, 443):
            raise self._blocked("Only ports 80 and 443 are allowed", url)
        try:
            host = raw_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise self._blocked("Invalid host name", url) from exc

        try:
            literal = str(ipaddress.ip_address(host))
        except ValueError:
            literal = None
        if literal is not None:
            ips = [literal]
        else:
            try:
                ips = list(dict.fromkeys(await self._resolver(host, port)))
            except OSError as exc:
                raise self._blocked("Host does not resolve", url) from exc
        if not ips:
            raise self._blocked("Host does not resolve", url)
        if not all(is_public_ip(ip) for ip in ips):
            raise self._blocked("Host resolves to a non-public address", url)
        return _Target(url=url, scheme=parts.scheme, host=host, port=port, ips=ips)

    # --- low-level request ------------------------------------------------------

    async def _get(self, url: str, *, is_robots: bool) -> _Raw:
        """GET with manual redirects, every hop re-validated.

        Page fetches enforce status, content type and size; robots fetches return any
        status and truncate the body at the robots cap.
        """
        httpx = self._httpx
        current = urldefrag(url)[0]
        for hop in range(self._max_redirects + 1):
            target = await self._validate(current)
            if not is_robots:
                await self._check_robots(target)
            default_port = _DEFAULT_PORTS[target.scheme] == target.port
            host_header = _bracket(target.host) + ("" if default_port else f":{target.port}")
            parts = urlsplit(current)
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            ip_netloc = _bracket(target.ips[0]) + ("" if default_port else f":{target.port}")
            request_url = f"{target.scheme}://{ip_netloc}{path}"
            headers = {
                "User-Agent": self._user_agent,
                "Accept": "*/*" if is_robots else _ACCEPT,
                "Accept-Encoding": "identity",
                "Host": host_header,
            }
            extensions: dict[str, Any] = {}
            if target.scheme == "https":
                extensions["sni_hostname"] = target.host
            try:
                async with self._client.stream(
                    "GET", request_url, headers=headers, extensions=extensions
                ) as resp:
                    if resp.status_code in _REDIRECT_STATUSES:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError(
                                f"Redirect {resp.status_code} without Location",
                                code=ec.WEB_FETCH_FAILED,
                                url=current,
                            )
                        if hop >= self._max_redirects:
                            raise FetchError(
                                f"Too many redirects (limit {self._max_redirects})",
                                code=ec.WEB_FETCH_FAILED,
                                url=current,
                            )
                        current = urldefrag(urljoin(current, location))[0]
                        continue
                    return await self._read(resp, current, is_robots=is_robots)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                raise FetchError(
                    f"Network error: {type(exc).__name__}",
                    code=ec.WEB_FETCH_FAILED,
                    url=current,
                ) from exc
        raise FetchError("Too many redirects", code=ec.WEB_FETCH_FAILED, url=current)

    async def _read(self, resp: httpx.Response, url: str, *, is_robots: bool) -> _Raw:
        status = resp.status_code
        main, charset = _parse_content_type(resp.headers.get("content-type", ""))
        if is_robots:
            cap = _ROBOTS_MAX_BYTES
            if not 200 <= status < 300:
                return _Raw(url, status, main, charset, b"")
        else:
            cap = self._max_bytes
            if not 200 <= status < 300:
                raise FetchError(f"HTTP status {status}", code=ec.WEB_FETCH_FAILED, url=url)
            if main not in _ALLOWED_TYPES:
                raise FetchError(
                    f"Content type not allowed: {main or 'missing'}",
                    code=ec.WEB_BAD_CONTENT_TYPE,
                    url=url,
                )
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > cap:
                raise FetchError(
                    f"Content-Length {declared} exceeds {cap} bytes",
                    code=ec.WEB_TOO_LARGE,
                    url=url,
                )
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > cap:
                if is_robots:
                    del buf[cap:]
                    break
                raise FetchError(f"Body exceeds {cap} bytes", code=ec.WEB_TOO_LARGE, url=url)
        return _Raw(url, status, main, charset, bytes(buf))

    # --- robots.txt -------------------------------------------------------------

    async def _check_robots(self, target: _Target) -> None:
        entry = await self._robots_entry(target)
        if entry.allow_all:
            return
        if entry.parser is None or not entry.parser.can_fetch(self._user_agent, target.url):
            raise FetchError(
                "Disallowed by robots.txt", code=ec.WEB_ROBOTS_DISALLOWED, url=target.url
            )

    async def _robots_entry(self, target: _Target) -> _RobotsEntry:
        now = time.monotonic()
        key = target.origin
        cached = self._robots.get(key)
        if cached is not None:
            if cached.expires > now:
                self._robots.move_to_end(key)
                return cached
            del self._robots[key]

        default_port = _DEFAULT_PORTS[target.scheme] == target.port
        netloc = _bracket(target.host) + ("" if default_port else f":{target.port}")
        robots_url = f"{target.scheme}://{netloc}/robots.txt"
        entry: _RobotsEntry
        try:
            raw = await self._get(robots_url, is_robots=True)
        except FetchError:
            entry = _RobotsEntry(now + _ROBOTS_FAILURE_TTL, None)
        else:
            expires = now + self._robots_ttl
            if 200 <= raw.status < 300:
                parser = RobotFileParser()
                parser.parse(_decode(raw.body, raw.charset).splitlines())
                parser.modified()
                entry = _RobotsEntry(expires, parser)
            elif 400 <= raw.status < 500 and raw.status != 429:
                entry = _RobotsEntry(expires, None, allow_all=True)
            else:
                entry = _RobotsEntry(now + _ROBOTS_FAILURE_TTL, None)
        self._robots[key] = entry
        while len(self._robots) > max(self._robots_cache_size, 0):
            self._robots.popitem(last=False)
        return entry

    # --- public API -------------------------------------------------------------

    async def fetch(self, url: str) -> FetchedPage:
        raw = await self._get(url, is_robots=False)
        is_binary = raw.content_type in _BINARY_TYPES
        return FetchedPage(
            url=url,
            final_url=raw.url,
            status=raw.status,
            content_type=raw.content_type,
            text="" if is_binary else _decode(raw.body, raw.charset),
            sha256=hashlib.sha256(raw.body).hexdigest(),
            fetched_at=datetime.now(UTC),
            body=raw.body if is_binary else b"",
        )
