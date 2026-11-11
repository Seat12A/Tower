"""Legacy contracts kept for compatibility (expand/contract applied to code).

``XClient`` predates ``PlatformClient`` (``tower.core.ports.platforms``), which
generalises it to every platform; new code uses ``PlatformClient``.
"""

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
    """Official X API (paid per read: each call has a cost). Deprecated: PlatformClient."""

    async def get_post(self, post_id: int) -> XPost | None: ...

    async def user_posts(self, user_id: int, *, since_id: int | None) -> Sequence[XPost]: ...


class Notifier(Protocol):
    """Outbound notification outside the event bus (budget alerts, incidents)."""

    async def notify(self, channel: str, message: str, **fields: Any) -> None: ...
