"""Platform client selection (one process-level client per platform)."""

from __future__ import annotations

from tower.core.config import Settings
from tower.core.ports.platforms import PlatformNotConfigured, PollingPlatformClient

SUPPORTED_PLATFORMS = ("x",)

_clients: dict[str, PollingPlatformClient] = {}
_overrides: dict[str, PollingPlatformClient] = {}


def set_client_override(platform: str, client: PollingPlatformClient | None) -> None:
    """Replace the client of a platform (tests); ``None`` removes the override."""
    if client is None:
        _overrides.pop(platform, None)
    else:
        _overrides[platform] = client


def get_client(settings: Settings, platform: str) -> PollingPlatformClient:
    """Client of ``platform``; raises ``PlatformNotConfigured`` when it cannot be built."""
    if platform in _overrides:
        return _overrides[platform]
    if platform in _clients:
        return _clients[platform]
    if platform != "x":
        raise PlatformNotConfigured(f"Platform {platform!r} is not supported by the radar")
    if settings.x_bearer_token is None:
        raise PlatformNotConfigured("X bearer token is not configured")
    # Lazy import: httpx is only loaded by a process that really reads X.
    from tower.core.adapters.x_client import XPlatformClient

    client = XPlatformClient(
        settings.x_bearer_token.get_secret_value(),
        api_base=settings.x_api_base,
        post_read_usd_micros=round(settings.x_price_post_read_usd * 1e6),
        user_read_usd_micros=round(settings.x_price_user_read_usd * 1e6),
    )
    _clients[platform] = client
    return client
