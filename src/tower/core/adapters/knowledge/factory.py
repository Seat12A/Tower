"""Builds the Atlas from settings (adapters with a key only, DB source cache, registry)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tower.core.adapters.knowledge.atlas import Atlas
    from tower.core.config import Settings
    from tower.core.db.database import Database

__all__ = ["build_atlas"]


async def build_atlas(settings: Settings, db: Database) -> Atlas:
    raise NotImplementedError
