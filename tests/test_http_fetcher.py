from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from tower.core.adapters.http_fetcher import HttpFetcher
from tower.core.domain import error_codes as ec
from tower.core.ports.web import FetchError

UA = "Seat12ABot/1.0 (+https://example.org/bot)"
PUBLIC = "93.184.216.34"
PUBLIC2 = "93.184.216.35"

Handler = Callable[[httpx.Request], httpx.Response]


def html_response(body: bytes = b"<html>ok</html>", **kw: Any) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": "text/html"}, **kw)


class Site:
    """Fake network: routes requests by (host header, path) and records them."""

    def __init__(self, routes: dict[str, Handler | httpx.Response] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []
        self.robots = httpx.Response(404)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.headers["host"]
        path = request.url.raw_path.decode()
        if request.url.path == "/robots.txt":
            return self.robots
        route = (
            self.routes.get(f"{host}{path}")
            or self.routes.get(path)
            or self.routes.get(request.url.path)
        )
        if route is None:
            return httpx.Response(404, headers={"content-type": "text/plain"})
        return route(request) if callable(route) else route

    def page_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path != "/robots.txt"]

    def robots_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/robots.txt"]


def make(
    site: Site | Handler,
    hosts: dict[str, list[str]] | None = None,
    **kwargs: Any,
) -> HttpFetcher:
    table = hosts if hosts is not None else {}
    table.setdefault("example.com", [PUBLIC])

    async def resolver(host: str, port: int) -> list[str]:
        if host not in table:
            raise OSError("no such host")
        return table[host]

    return HttpFetcher(
        user_agent=UA,
        resolver=resolver,
        transport=httpx.MockTransport(site),
        **kwargs,
    )


async def expect(fetcher: HttpFetcher, url: str, code: ec.ErrorCode) -> FetchError:
    with pytest.raises(FetchError) as info:
        await fetcher.fetch(url)
    assert info.value.code is code
    return info.value


# --- nominal -------------------------------------------------------------------


async def test_nominal_html() -> None:
    body = "<html>café</html>".encode("latin-1")
    site = Site(
        {
            "/page": httpx.Response(
                200, content=body, headers={"content-type": "Text/HTML; charset=ISO-8859-1"}
            )
        }
    )
    fetcher = make(site)
    page = await fetcher.fetch("https://example.com/page?q=1#frag")
    await fetcher.aclose()

    assert page.text == "<html>café</html>"
    assert page.status == 200
    assert page.content_type == "text/html"
    assert page.sha256 == hashlib.sha256(body).hexdigest()
    assert page.final_url == "https://example.com/page?q=1"
    assert page.body == b""
    req = site.page_requests()[0]
    assert req.headers["user-agent"] == UA
    assert "text/html" in req.headers["accept"]
    assert req.headers["host"] == "example.com"
    assert req.url.host == PUBLIC
    assert req.url.query == b"q=1"
    assert req.extensions["sni_hostname"] == "example.com"


async def test_http_has_no_sni_and_ipv6_pinning() -> None:
    site = Site({"/": html_response()})
    fetcher = make(site, {"example.com": ["2606:2800:220:1::1"]})
    page = await fetcher.fetch("http://example.com/")
    assert page.final_url == "http://example.com/"
    req = site.page_requests()[0]
    assert req.url.host == "2606:2800:220:1::1"
    assert "sni_hostname" not in req.extensions
    assert req.headers["host"] == "example.com"


async def test_invalid_charset_falls_back_to_utf8() -> None:
    site = Site(
        {
            "/": httpx.Response(
                200, content="é".encode(), headers={"content-type": "text/plain; charset=nope"}
            )
        }
    )
    page = await make(site).fetch("https://example.com/")
    assert page.text == "é"


# --- blocked URLs ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "https://example.com:8080/",
        "http://example.com:443x/",
        "https://user:pw@example.com/",
        "https://user@example.com/",
        "https:///nohost",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://169.254.169.254/",
        "http://nowhere.invalid/",
    ],
)
async def test_blocked_urls(url: str) -> None:
    site = Site()
    await expect(make(site), url, ec.WEB_BLOCKED_URL)
    assert site.requests == []


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "::1", "::ffff:10.0.0.1", "100.64.0.1"])
async def test_host_resolving_to_non_public_blocked(ip: str) -> None:
    site = Site()
    await expect(make(site, {"evil.test": [ip]}), "https://evil.test/", ec.WEB_BLOCKED_URL)
    assert site.requests == []


async def test_mixed_public_and_private_blocked() -> None:
    site = Site()
    fetcher = make(site, {"mixed.test": [PUBLIC, "10.0.0.1"]})
    await expect(fetcher, "https://mixed.test/", ec.WEB_BLOCKED_URL)
    assert site.requests == []


async def test_empty_resolution_blocked() -> None:
    await expect(make(Site(), {"empty.test": []}), "https://empty.test/", ec.WEB_BLOCKED_URL)


async def test_public_ip_literal_allowed() -> None:
    site = Site({"/": html_response()})
    page = await make(site).fetch(f"http://{PUBLIC}/")
    assert page.final_url == f"http://{PUBLIC}/"


# --- redirects -----------------------------------------------------------------


def redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


async def test_redirect_to_private_host_blocked() -> None:
    site = Site({"/start": redirect("https://internal.test/secret")})
    fetcher = make(site, {"internal.test": ["10.0.0.5"]})
    err = await expect(fetcher, "https://example.com/start", ec.WEB_BLOCKED_URL)
    assert err.url == "https://internal.test/secret"
    assert len(site.page_requests()) == 1


async def test_redirect_to_bad_port_blocked() -> None:
    site = Site({"/start": redirect("https://example.com:8443/x")})
    await expect(make(site), "https://example.com/start", ec.WEB_BLOCKED_URL)


async def test_redirect_chain_over_limit() -> None:
    site = Site({f"/r{i}": redirect(f"/r{i + 1}") for i in range(10)})
    await expect(make(site, max_redirects=3), "https://example.com/r0", ec.WEB_FETCH_FAILED)
    assert len(site.page_requests()) == 4


async def test_redirect_chain_at_limit_ok() -> None:
    routes: dict[str, Handler | httpx.Response] = {
        "/r0": redirect("/r1", 301),
        "/r1": redirect("/r2", 307),
        "/r2": html_response(),
    }
    page = await make(Site(routes), max_redirects=2).fetch("https://example.com/r0")
    assert page.final_url == "https://example.com/r2"


async def test_relative_redirect_resolved() -> None:
    site = Site({"/a/b": redirect("../c?x=1", 303), "/c?x=1": html_response(b"final")})
    page = await make(site).fetch("https://example.com/a/b")
    assert page.final_url == "https://example.com/c?x=1"
    assert page.text == "final"


async def test_cross_host_redirect_revalidated_and_host_header() -> None:
    site = Site({"/go": redirect("https://other.test/land"), "/land": html_response()})
    fetcher = make(site, {"other.test": [PUBLIC2]})
    page = await fetcher.fetch("https://example.com/go")
    assert page.final_url == "https://other.test/land"
    last = site.page_requests()[-1]
    assert last.url.host == PUBLIC2
    assert last.headers["host"] == "other.test"
    assert last.extensions["sni_hostname"] == "other.test"


async def test_redirect_without_location_fails() -> None:
    site = Site({"/x": httpx.Response(302)})
    await expect(make(site), "https://example.com/x", ec.WEB_FETCH_FAILED)


# --- size and type -----------------------------------------------------------------


async def test_too_large_by_content_length() -> None:
    site = Site(
        {"/": httpx.Response(200, content=b"x" * 100, headers={"content-type": "text/plain"})}
    )
    await expect(make(site, max_bytes=50), "https://example.com/", ec.WEB_TOO_LARGE)


async def test_too_large_by_streaming() -> None:
    consumed = 0

    async def stream() -> AsyncIterator[bytes]:
        nonlocal consumed
        for _ in range(100):
            consumed += 1
            yield b"x" * 10

    site = Site(
        {"/": httpx.Response(200, content=stream(), headers={"content-type": "text/plain"})}
    )
    await expect(make(site, max_bytes=55), "https://example.com/", ec.WEB_TOO_LARGE)
    assert consumed < 100


async def test_exact_size_allowed() -> None:
    site = Site(
        {"/": httpx.Response(200, content=b"x" * 50, headers={"content-type": "text/plain"})}
    )
    page = await make(site, max_bytes=50).fetch("https://example.com/")
    assert page.text == "x" * 50


async def test_disallowed_content_type_before_body() -> None:
    read = False

    async def stream() -> AsyncIterator[bytes]:
        nonlocal read
        read = True
        yield b"\x89PNG"

    site = Site({"/": httpx.Response(200, content=stream(), headers={"content-type": "image/png"})})
    await expect(make(site), "https://example.com/", ec.WEB_BAD_CONTENT_TYPE)
    assert not read


async def test_missing_content_type_rejected() -> None:
    site = Site({"/": httpx.Response(200, content=b"data")})
    await expect(make(site), "https://example.com/", ec.WEB_BAD_CONTENT_TYPE)


async def test_pdf_bytes_kept() -> None:
    raw = b"%PDF-1.7\x00\xff binary"
    site = Site(
        {"/d.pdf": httpx.Response(200, content=raw, headers={"content-type": "application/pdf"})}
    )
    page = await make(site).fetch("https://example.com/d.pdf")
    assert page.text == ""
    assert page.body == raw
    assert page.sha256 == hashlib.sha256(raw).hexdigest()
    assert page.content_type == "application/pdf"


# --- robots.txt ------------------------------------------------------------------


async def test_robots_disallow_path() -> None:
    site = Site({"/private/x": html_response(), "/public": html_response()})
    site.robots = httpx.Response(
        200, content=b"User-agent: *\nDisallow: /private/\n", headers={"content-type": "text/plain"}
    )
    fetcher = make(site)
    await expect(fetcher, "https://example.com/private/x", ec.WEB_ROBOTS_DISALLOWED)
    assert (await fetcher.fetch("https://example.com/public")).status == 200
    assert [r.url.path for r in site.page_requests()] == ["/public"]


async def test_robots_specific_user_agent_group() -> None:
    site = Site({"/": html_response()})
    site.robots = httpx.Response(200, content=b"User-agent: seat12abot\nDisallow: /\n")
    await expect(make(site), "https://example.com/", ec.WEB_ROBOTS_DISALLOWED)


async def test_robots_404_allows_all() -> None:
    site = Site({"/x": html_response()})
    assert (await make(site).fetch("https://example.com/x")).status == 200


@pytest.mark.parametrize("status", [401, 403])
async def test_robots_auth_errors_allow_all(status: int) -> None:
    site = Site({"/x": html_response()})
    site.robots = httpx.Response(status)
    assert (await make(site).fetch("https://example.com/x")).status == 200


@pytest.mark.parametrize("status", [429, 503])
async def test_robots_unavailable_disallows_all(status: int) -> None:
    site = Site({"/x": html_response()})
    site.robots = httpx.Response(status)
    await expect(make(site), "https://example.com/x", ec.WEB_ROBOTS_DISALLOWED)
    assert site.page_requests() == []


async def test_robots_network_error_disallows_all() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            raise httpx.ConnectError("boom")
        return html_response()

    await expect(make(handler), "https://example.com/x", ec.WEB_ROBOTS_DISALLOWED)


async def test_robots_cache_hit() -> None:
    site = Site({"/a": html_response(), "/b": html_response()})
    fetcher = make(site)
    await fetcher.fetch("https://example.com/a")
    await fetcher.fetch("https://example.com/b")
    assert len(site.robots_requests()) == 1


async def test_robots_ttl_expiry_and_failure_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    site = Site({"/a": html_response()})
    fetcher = make(site, robots_ttl=100.0)
    await fetcher.fetch("https://example.com/a")
    now[0] += 99
    await fetcher.fetch("https://example.com/a")
    assert len(site.robots_requests()) == 1
    now[0] += 2
    await fetcher.fetch("https://example.com/a")
    assert len(site.robots_requests()) == 2

    # A failure is cached for 10 minutes only, whatever robots_ttl is.
    site.robots = httpx.Response(503)
    now[0] += 1000
    await expect(fetcher, "https://example.com/a", ec.WEB_ROBOTS_DISALLOWED)
    await expect(fetcher, "https://example.com/a", ec.WEB_ROBOTS_DISALLOWED)
    assert len(site.robots_requests()) == 3
    now[0] += 601
    site.robots = httpx.Response(404)
    assert (await fetcher.fetch("https://example.com/a")).status == 200


async def test_robots_lru_bound() -> None:
    site = Site({"/": html_response()})
    hosts = {f"h{i}.test": [PUBLIC] for i in range(4)}
    fetcher = make(site, hosts, robots_cache_size=2)
    for i in range(4):
        await fetcher.fetch(f"https://h{i}.test/")
        assert len(fetcher._robots) <= 2
    assert list(fetcher._robots) == [("https", "h2.test", 443), ("https", "h3.test", 443)]
    before = len(site.robots_requests())
    await fetcher.fetch("https://h0.test/")  # evicted → refetched
    assert len(site.robots_requests()) == before + 1


async def test_robots_checked_per_origin_and_per_redirect_hop() -> None:
    site = Site({"/go": redirect("https://other.test/land"), "/land": html_response()})
    fetcher = make(site, {"other.test": [PUBLIC2]})
    original = site.__call__

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt" and request.headers["host"] == "other.test":
            return httpx.Response(200, content=b"User-agent: *\nDisallow: /land\n")
        return original(request)

    fetcher = make(handler, {"other.test": [PUBLIC2]})
    await expect(fetcher, "https://example.com/go", ec.WEB_ROBOTS_DISALLOWED)


async def test_robots_fetch_blocked_when_redirecting_to_private() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return redirect("https://internal.test/robots.txt")
        return html_response()

    fetcher = make(handler, {"internal.test": ["10.0.0.1"]})
    await expect(fetcher, "https://example.com/x", ec.WEB_ROBOTS_DISALLOWED)


async def test_robots_not_fetched_for_robots_itself() -> None:
    site = Site()
    site.robots = httpx.Response(
        200, content=b"User-agent: *\nAllow: /\n", headers={"content-type": "text/plain"}
    )
    fetcher = make(site)
    page = await fetcher.fetch("https://example.com/robots.txt")
    assert page.text.startswith("User-agent")
    assert len(site.robots_requests()) == 2  # one for the robots check, one for the page


async def test_robots_body_truncated_not_failed() -> None:
    big = b"User-agent: *\nDisallow: /nope\n" + b"# pad\n" * 200_000
    site = Site({"/ok": html_response()})
    site.robots = httpx.Response(200, content=big)
    fetcher = make(site)
    assert (await fetcher.fetch("https://example.com/ok")).status == 200
    await expect(fetcher, "https://example.com/nope", ec.WEB_ROBOTS_DISALLOWED)


# --- failures ---------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 500, 410])
async def test_non_2xx_final_status(status: int) -> None:
    site = Site({"/": httpx.Response(status, headers={"content-type": "text/html"})})
    err = await expect(make(site), "https://example.com/", ec.WEB_FETCH_FAILED)
    assert str(status) in str(err)


async def test_transport_exception_mapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ReadTimeout("slow")

    await expect(make(handler), "https://example.com/", ec.WEB_FETCH_FAILED)


# --- import hygiene --------------------------------------------------------------------


def test_import_does_not_load_httpx() -> None:
    code = (
        "import sys, tower.core.adapters.http_fetcher; sys.exit(1 if 'httpx' in sys.modules else 0)"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
