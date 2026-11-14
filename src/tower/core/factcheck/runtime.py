"""Process-level accessors of the fact-check pipeline (one instance per process, built
lazily so a process only loads what it runs). Tests replace them with ``set_override``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tower.core.domain.factcheck.confidence import Coefficients, Thresholds

if TYPE_CHECKING:
    from tower.core.adapters.knowledge.atlas import Atlas
    from tower.core.config import Settings
    from tower.core.db.database import Database
    from tower.core.llm.embeddings import EmbeddingService
    from tower.core.ports.llm import LLMClient
    from tower.core.ports.platforms import PlatformClient

__all__ = [
    "clear_overrides",
    "confidence_settings",
    "get_atlas",
    "get_embedder",
    "get_llm",
    "get_platform_client",
    "loaded_terms",
    "set_override",
]

_instances: dict[str, Any] = {}
_overrides: dict[str, Any] = {}


def set_override(name: str, value: Any) -> None:
    """Replace ``llm``, ``embedder``, ``atlas`` or ``platform:<name>`` (tests); ``None``
    removes it."""
    if value is None:
        _overrides.pop(name, None)
    else:
        _overrides[name] = value


def clear_overrides() -> None:
    _overrides.clear()
    _instances.clear()


def get_llm(settings: Settings, db: Database) -> LLMClient:
    """``LLMRouter`` over the configured providers; with ``TOWER_LLM_FAKE=1`` every
    provider key is served by ``FakeLLMProvider`` (refused in prod by the settings)."""
    if "llm" in _overrides:
        return _overrides["llm"]  # type: ignore[no-any-return]
    if "llm" not in _instances:
        from tower.core.llm.prompts import PromptRegistry
        from tower.core.llm.router import LLMRouter
        from tower.core.ports.llm import LLMProvider

        providers: dict[str, LLMProvider]
        if settings.llm_fake:
            from tower.core.adapters.fake_llm import FakeLLMProvider

            providers = {
                name: FakeLLMProvider(name) for name in ("openai", "openai_shared", "anthropic")
            }
        else:
            from tower.core.adapters.factory import build_llm_providers

            providers = build_llm_providers(settings)
        _instances["llm"] = LLMRouter(
            db,
            providers,
            PromptRegistry.default(),
            usage_tier=settings.openai_usage_tier,
            free_margin=settings.llm_free_margin,
            reservation_ttl_s=settings.budget_reservation_ttl_s,
        )
    return _instances["llm"]  # type: ignore[no-any-return]


def get_embedder(settings: Settings, db: Database) -> EmbeddingService:
    """``EmbeddingService`` over OpenAI (non-sharing organization key), or over the
    deterministic ``FakeEmbedder`` with ``TOWER_LLM_FAKE=1``. One instance per process
    (its cache is bounded)."""
    if "embedder" in _overrides:
        return _overrides["embedder"]  # type: ignore[no-any-return]
    if "embedder" not in _instances:
        from tower.core.llm.embeddings import EmbeddingService
        from tower.core.ports.embeddings import Embedder

        embedder: Embedder
        if settings.llm_fake:
            from tower.core.adapters.fake_llm import FakeEmbedder

            embedder = FakeEmbedder()
        else:
            from tower.core.adapters.openai_embedder import OpenAIEmbedder
            from tower.core.llm.errors import ProviderNotConfigured

            if settings.openai_api_key is None:
                raise ProviderNotConfigured("OpenAI API key is not configured", role="embed")
            embedder = OpenAIEmbedder(settings.openai_api_key.get_secret_value())
        _instances["embedder"] = EmbeddingService(
            db,
            embedder,
            reservation_ttl_s=settings.budget_reservation_ttl_s,
        )
    return _instances["embedder"]  # type: ignore[no-any-return]


async def get_atlas(settings: Settings, db: Database) -> Atlas:
    """Atlas with the configured adapters, the DB-backed source cache and a fresh
    registry snapshot (the registry is small; reloaded on every call)."""
    if "atlas" in _overrides:
        return _overrides["atlas"]  # type: ignore[no-any-return]
    from tower.core.adapters.knowledge.factory import build_atlas

    return await build_atlas(settings, db)


def get_platform_client(settings: Settings, platform: str) -> PlatformClient:
    """Official API client of ``platform`` (X only in lot 3); raises
    ``PlatformNotConfigured`` when it cannot be built."""
    key = f"platform:{platform}"
    if key in _overrides:
        return _overrides[key]  # type: ignore[no-any-return]
    if key not in _instances:
        from tower.core.ports.platforms import PlatformNotConfigured

        if platform != "x":
            raise PlatformNotConfigured(f"Platform {platform!r} is not supported")
        if settings.x_bearer_token is None:
            raise PlatformNotConfigured("X bearer token is not configured")
        from tower.core.adapters.x_client import XPlatformClient

        _instances[key] = XPlatformClient(
            settings.x_bearer_token.get_secret_value(),
            api_base=settings.x_api_base,
            post_read_usd_micros=round(settings.x_price_post_read_usd * 1e6),
            user_read_usd_micros=round(settings.x_price_user_read_usd * 1e6),
        )
    return _instances[key]  # type: ignore[no-any-return]


def confidence_settings(settings: Settings) -> tuple[Coefficients, Thresholds]:
    overrides: Mapping[str, float] = (
        json.loads(settings.fc_confidence_coefficients)
        if settings.fc_confidence_coefficients.strip()
        else {}
    )
    return Coefficients().with_overrides(overrides), Thresholds(
        cap=settings.fc_confidence_cap,
        high=settings.fc_confidence_high,
        medium=settings.fc_confidence_medium,
    )


_DEFAULT_TERMS = Path(__file__).with_name("loaded_terms.json")


@lru_cache(maxsize=4)
def _read_terms(path: str) -> dict[str, dict[str, list[str]]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {"loaded": data.get("loaded", {}), "allowed": data.get("allowed", {})}


def loaded_terms(settings: Settings) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """``(lexicon, allowed)`` by language, from ``TOWER_FC_LOADED_TERMS_FILE`` or the
    packaged ``loaded_terms.json``."""
    path = settings.fc_loaded_terms_file or str(_DEFAULT_TERMS)
    data = _read_terms(path)
    return data["loaded"], data["allowed"]
