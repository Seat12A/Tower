"""Content URLs of the fact-check pipeline. Pure domain: standard library only.

Only X/Twitter status URLs can be parsed today; other platforms are addressed by
``(platform, external_id)``.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput
from tower.core.domain.platforms import X_MAX_ID, Platform

__all__ = ["content_url", "parse_content_url"]

_X_HOSTS = frozenset({"x.com", "twitter.com"})
_X_SUBDOMAINS = frozenset({"www", "mobile", "m"})
_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_STATUS_ID = re.compile(r"^[0-9]{1,19}$")
_MAX_URL_LENGTH = 2048


def _bad(url: str, reason: str) -> InvalidInput:
    return InvalidInput(
        "Unsupported content URL (expected https://x.com/<account>/status/<id>)",
        code=ec.FCK_BAD_URL,
        reason=reason,
        url=url[:200],
    )


def parse_content_url(url: str) -> tuple[str, str]:
    """Return ``(platform, external_id)`` of an X status URL.

    Accepted hosts: ``x.com`` and ``twitter.com`` with an optional ``www.``, ``mobile.``
    or ``m.`` prefix. Accepted paths: ``/<handle>/status/<id>`` and ``/i/status/<id>``,
    followed by anything (``/photo/1``, query string, fragment). Anything else raises
    ``InvalidInput`` (``FCK_BAD_URL``).
    """
    raw = url.strip() if isinstance(url, str) else ""
    if not raw or len(raw) > _MAX_URL_LENGTH or any(c.isspace() for c in raw):
        raise _bad(str(url), "empty_or_malformed")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
    except ValueError:
        raise _bad(url, "malformed") from None
    if parts.scheme not in ("http", "https"):
        raise _bad(url, "scheme")
    labels = host.split(".")
    if len(labels) == 3 and labels[0] in _X_SUBDOMAINS:
        host = ".".join(labels[1:])
    if host not in _X_HOSTS:
        raise _bad(url, "host")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 3 or segments[1] != "status":
        raise _bad(url, "path")
    handle, status_id = segments[0], segments[2]
    if handle != "i" and not _HANDLE.match(handle):
        raise _bad(url, "handle")
    if not _STATUS_ID.match(status_id) or int(status_id) > X_MAX_ID:
        raise _bad(url, "status_id")
    return Platform.X.value, status_id


def content_url(platform: str, external_id: str) -> str | None:
    """Canonical link of a content, or ``None`` for a platform without a known scheme."""
    if platform == Platform.X.value:
        return f"https://x.com/i/status/{external_id}"
    return None
