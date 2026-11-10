"""Contrats minimaux, à compléter quand les services correspondants seront écrits."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class XPost:
    id: int
    author_id: int
    text: str
    created_at: datetime
    metrics: dict[str, int]


class XClient(Protocol):
    """API officielle de X (lecture payante à l'unité : chaque appel a un coût)."""

    async def get_post(self, post_id: int) -> XPost | None: ...

    async def user_posts(self, user_id: int, *, since_id: int | None) -> Sequence[XPost]: ...


class LLMClient(Protocol):
    """Appel d'un modèle avec sortie structurée ; renvoie aussi l'usage pour le coût."""

    async def complete(
        self, *, model: str, system: str, messages: Sequence[dict[str, Any]], schema: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, int]]: ...


class SearchClient(Protocol):
    async def search(self, query: str, *, limit: int) -> Sequence[dict[str, Any]]: ...


class Notifier(Protocol):
    """Notification sortante hors bus d'événements (alertes budget, incidents)."""

    async def notify(self, channel: str, message: str, **fields: Any) -> None: ...
