"""The pinned "How to review" post is created once, never at every start."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

from tower.intercom.delivery import ForumDelivery

FORUM_ID, BOT_ID = 10, 99


class FakeThread:
    def __init__(self, name: str, *, owner: int, pinned: bool = False, archived: bool = False):
        self.name = name
        self.owner_id = owner
        self.parent_id = FORUM_ID
        self.flags = SimpleNamespace(pinned=pinned)
        self.archived = archived
        self.edits: list[dict[str, Any]] = []

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


class FakeForum:
    def __init__(self, active: list[FakeThread], archived: list[FakeThread]) -> None:
        self.id = FORUM_ID
        self.created: list[str] = []

        async def active_threads() -> list[FakeThread]:
            return active

        self.guild = SimpleNamespace(active_threads=active_threads)
        self._archived = archived

    async def archived_threads(self, limit: int = 100) -> AsyncIterator[FakeThread]:
        for thread in self._archived:
            yield thread

    async def create_thread(self, *, name: str, **kwargs: Any) -> Any:
        self.created.append(name)
        return SimpleNamespace(thread=FakeThread(name, owner=BOT_ID))


def delivery() -> ForumDelivery:
    client = SimpleNamespace(user=SimpleNamespace(id=BOT_ID))
    return ForumDelivery(cast(Any, client), FORUM_ID)


async def test_existing_pinned_guide_is_kept() -> None:
    forum = FakeForum([FakeThread("How to review", owner=BOT_ID, pinned=True)], [])
    await delivery()._ensure_guide(cast(Any, forum))
    assert forum.created == []


async def test_unpinned_or_archived_guide_is_pinned_again_not_duplicated() -> None:
    old = FakeThread("How to review", owner=BOT_ID, archived=True)
    forum = FakeForum([FakeThread("Case 1234abcd", owner=BOT_ID)], [old])
    await delivery()._ensure_guide(cast(Any, forum))
    assert forum.created == [] and old.edits == [{"archived": False, "pinned": True}]


async def test_guide_is_created_once_when_missing() -> None:
    someone_else = FakeThread("How to review", owner=1)  # not the bot's: not the guide
    forum = FakeForum([someone_else], [])
    await delivery()._ensure_guide(cast(Any, forum))
    assert forum.created == ["How to review"]
