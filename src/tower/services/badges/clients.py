"""Process-level clients of the badge service (lazily built; tests override them)."""

from __future__ import annotations

from tower.core.config import Settings
from tower.core.ports.badges import AccountBatchClient, WikidataBadgeSource
from tower.core.ports.platforms import PlatformNotConfigured

_wikidata: WikidataBadgeSource | None = None
_x: AccountBatchClient | None = None
_wikidata_override: WikidataBadgeSource | None = None
_x_override: AccountBatchClient | None = None


def set_overrides(
    *, wikidata: WikidataBadgeSource | None = None, x: AccountBatchClient | None = None
) -> None:
    """Replace the clients (tests); ``None`` removes an override."""
    global _wikidata_override, _x_override
    _wikidata_override = wikidata
    _x_override = x


def wikidata_source(settings: Settings) -> WikidataBadgeSource:
    global _wikidata
    if _wikidata_override is not None:
        return _wikidata_override
    if _wikidata is None:
        # Lazy import: httpx is only loaded by a replica that runs the import.
        from tower.core.adapters.wikidata_badges import WikidataSparqlSource

        _wikidata = WikidataSparqlSource(endpoint=settings.badges_wikidata_endpoint)
    return _wikidata


def x_client(settings: Settings) -> AccountBatchClient:
    """X client for account lookups; ``PlatformNotConfigured`` without a bearer token."""
    global _x
    if _x_override is not None:
        return _x_override
    if _x is None:
        if settings.x_bearer_token is None:
            raise PlatformNotConfigured("X bearer token is not configured")
        from tower.core.adapters.x_client import XPlatformClient

        _x = XPlatformClient(
            settings.x_bearer_token.get_secret_value(),
            api_base=settings.x_api_base,
            post_read_usd_micros=round(settings.x_price_post_read_usd * 1e6),
            user_read_usd_micros=round(settings.x_price_user_read_usd * 1e6),
        )
    return _x
