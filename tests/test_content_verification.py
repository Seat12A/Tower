"""Verification of extension contents: platform reads win, public embed check (X oEmbed)
with its limits, and targeted fact-checks opened only on trusted contents."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import structlog
from sqlalchemy import text

from tests.conftest import make_settings
from tests.test_factcheck_service import event, jobs_of
from tests.test_ingestion import new_token, to_inputs
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.errors import PermissionDenied
from tower.core.domain.security import Actor
from tower.core.domain.verification import (
    EmbedPost,
    Verification,
    comparable,
    compare,
    embed_text,
    handle_from_author_url,
)
from tower.core.errors.reporter import ErrorReporter
from tower.core.factcheck import names
from tower.core.jobs.queue import JobRecord, RetryLater
from tower.core.ports.platforms import (
    PlatformBadResponse,
    PlatformRateLimited,
    PlatformUnavailable,
)
from tower.core.services.base import JobContext
from tower.core.usecases import ingestion as uc
from tower.core.usecases import watchlist as watch_uc
from tower.services.factcheck.jobs import on_content_ingested
from tower.services.ingest import verify

NOW = datetime.now(UTC)
SYSTEM = Actor.system("test")
HTML = (
    '<blockquote class="twitter-tweet" data-dnt="true"><p lang="fr" dir="ltr">'
    "Le chômage est à 7 % &amp; il baisse<br>Source : "
    '<a href="https://t.co/AbC123">https://t.co/AbC123</a> '
    '<a href="https://t.co/pic">pic.x.com/xyz</a></p>&mdash; Le Média (@LeMedia) '
    '<a href="https://x.com/LeMedia/status/1">March 21, 2026</a></blockquote>'
)


# ------------------------------------------------------------------------- domain


def test_embed_text_and_handle() -> None:
    assert embed_text(HTML) == (
        "Le chômage est à 7 % & il baisse\nSource : https://t.co/AbC123 pic.x.com/xyz"
    )
    assert embed_text("<blockquote>no paragraph</blockquote>") is None
    assert embed_text("<p></p>") == ""
    assert handle_from_author_url("https://x.com/LeMedia") == "lemedia"
    assert handle_from_author_url("https://twitter.com/le_media/") == "le_media"
    assert handle_from_author_url("https://evil.example/LeMedia") is None
    assert handle_from_author_url(None) is None


def test_compare_outcomes() -> None:
    shown = EmbedPost(embed_text(HTML) or "", "lemedia")
    page = "Le chômage est à 7 % & il baisse 📉\nSource : lemonde.fr/eco/chom… "
    assert compare(page, "@LeMedia", shown) is Verification.VERIFIED
    assert compare(page, None, shown) is Verification.VERIFIED
    assert compare(page, "Autre", shown) is Verification.MISMATCH
    assert compare("Le chômage est à 12 % & il baisse", "LeMedia", shown) is (Verification.MISMATCH)
    assert compare(page, "LeMedia", None) is Verification.UNAVAILABLE
    # A changed mention is a different post (unlike the E0 normalisation).
    a = EmbedPost("Merci @alice pour tout", "x")
    assert compare("Merci @bob pour tout", None, a) is Verification.MISMATCH
    # Possible truncation of a long post: inconclusive.
    long_text = "mot " * 120
    truncated = EmbedPost(long_text[:300] + "…", None)
    assert compare(long_text + "fin", None, truncated) is Verification.UNVERIFIED
    short = EmbedPost("court", None)
    assert compare("court et plus long", None, short) is Verification.MISMATCH
    assert comparable("A  B‍\nhttps://x.co/a C") == "a b c"


# ------------------------------------------------------------------------ adapter


def _client(handler: Any) -> Any:
    from tower.core.adapters.x_oembed import XOEmbedClient

    return XOEmbedClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_adapter_parses_and_maps_errors() -> None:
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"author_url": "https://x.com/LeMedia", "html": HTML})

    post = await _client(ok).lookup("123")
    assert post is not None and post.author_handle == "lemedia"
    assert post.text.startswith("Le chômage")
    params = seen[0].url.params
    assert params["url"] == "https://x.com/i/status/123"
    assert params["omit_script"] == "1" and params["dnt"] == "true"

    assert await _client(lambda r: httpx.Response(404)).lookup("1") is None
    with pytest.raises(PlatformUnavailable):
        await _client(lambda r: httpx.Response(403)).lookup("1")
    with pytest.raises(PlatformUnavailable):
        await _client(lambda r: httpx.Response(503)).lookup("1")
    with pytest.raises(PlatformRateLimited) as limited:
        await _client(lambda r: httpx.Response(429, headers={"retry-after": "5"})).lookup("1")
    assert limited.value.retry_after == 60.0  # clamped to at least one minute
    with pytest.raises(PlatformBadResponse):
        await _client(lambda r: httpx.Response(200, text="not json")).lookup("1")
    with pytest.raises(PlatformBadResponse):
        await _client(lambda r: httpx.Response(200, json={"html": "<div></div>"})).lookup("1")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(PlatformUnavailable):
        await _client(boom).lookup("1")


# ---------------------------------------------------------------------- ingestion


def _raw(ext: str, body: str, *, author: str = "500", handle: str = "lemedia") -> dict[str, Any]:
    return {
        "platform": "x",
        "external_id": ext,
        "text": body,
        "lang_hint": "fr",
        "author": {"external_id": author, "handle": handle},
    }


async def _ingest(db: Database, raw: list[dict[str, Any]], *, source: str) -> None:
    extension = source == "extension"
    async with db.transaction() as s:
        await uc.ingest_items(
            s,
            Actor.installation() if extension else SYSTEM,
            to_inputs(raw),
            source="radar" if extension else source,
            filter_version=None,
            installation_token=new_token() if extension else None,
            max_items=50,
            daily_quota=500,
        )


async def _content(db: Database, ext: str) -> Any:
    async with db.session() as s:
        return (
            await s.execute(
                text(
                    "SELECT c.id, c.text, c.verification, c.checked_at, c.lang, "
                    "a.external_id AS author FROM content_items c "
                    "LEFT JOIN platform_accounts a ON a.id = c.author_account_id "
                    "WHERE c.external_id = :e"
                ),
                {"e": ext},
            )
        ).one()


async def _events(db: Database, topic: str) -> list[dict[str, Any]]:
    async with db.session() as s:
        rows = await s.execute(
            text("SELECT payload FROM events WHERE topic = :t ORDER BY id"), {"t": topic}
        )
        return [r[0] for r in rows]


async def test_platform_read_corrects_extension_content(db: Database) -> None:
    await _ingest(db, [_raw("101", "Faux texte attribué", author="666")], source="extension")
    row = await _content(db, "101")
    assert row.verification == "unverified" and row.author == "666"

    await _ingest(db, [_raw("101", "Le vrai texte du post", author="500")], source="radar")
    row = await _content(db, "101")
    assert row.text == "Le vrai texte du post"
    assert row.author == "500" and row.verification == "platform"
    verified = await _events(db, "content.verified")
    assert [(e["id"], e["source"]) for e in verified] == [(str(row.id), "radar")]

    # A platform text is never replaced, by the extension nor by a later read.
    await _ingest(db, [_raw("101", "Encore un faux", author="666")], source="extension")
    await _ingest(db, [_raw("101", "Texte modifié depuis", author="500")], source="radar")
    row = await _content(db, "101")
    assert row.text == "Le vrai texte du post" and row.verification == "platform"
    assert len(await _events(db, "content.verified")) == 1


async def test_platform_read_upgrades_verified_without_event(db: Database) -> None:
    await _ingest(db, [_raw("102", "Texte confirmé")], source="extension")
    row = await _content(db, "102")
    async with db.transaction() as s:
        assert await uc.record_verification(s, SYSTEM, row.id, Verification.VERIFIED)
    await _ingest(db, [_raw("102", "Texte confirmé, version lue")], source="radar")
    row = await _content(db, "102")
    assert row.verification == "platform" and row.text == "Texte confirmé"
    assert [e["source"] for e in await _events(db, "content.verified")] == ["oembed"]


async def test_record_verification_rules(db: Database) -> None:
    await _ingest(db, [_raw("103", "Un texte")], source="extension")
    await _ingest(db, [_raw("104", "Un autre texte")], source="radar")
    a = (await _content(db, "103")).id
    b = (await _content(db, "104")).id
    async with db.transaction() as s:
        assert await uc.record_verification(s, SYSTEM, a, Verification.MISMATCH)
        assert not await uc.record_verification(s, SYSTEM, a, Verification.VERIFIED)
        assert not await uc.record_verification(s, SYSTEM, b, Verification.VERIFIED)
        with pytest.raises(ValueError, match="platform reads only"):
            await uc.record_verification(s, SYSTEM, a, Verification.PLATFORM)
        with pytest.raises(PermissionDenied):
            await uc.record_verification(s, Actor.anonymous(), a, Verification.VERIFIED)
    assert (await _content(db, "103")).verification == "mismatch"
    assert (await _content(db, "104")).verification == "platform"
    assert await _events(db, "content.verified") == []


# ------------------------------------------------------------------------- the job


class FakeEmbeds:
    def __init__(self, posts: dict[str, EmbedPost | Exception | None]) -> None:
        self.posts = posts
        self.calls: list[str] = []

    async def lookup(self, external_id: str) -> EmbedPost | None:
        self.calls.append(external_id)
        value = self.posts.get(external_id)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def fake_embeds() -> Any:
    holder = FakeEmbeds({})
    verify.set_client(holder)
    yield holder
    verify.set_client(None)


def _ctx(db: Database, settings: Settings) -> JobContext:
    job = JobRecord(
        id=1,
        queue=verify.QUEUE,
        kind=verify.JOB_VERIFY,
        payload={},
        priority=verify.PRIORITY,
        attempts=1,
        max_attempts=3,
        dedupe_key=verify.DEDUPE_KEY,
        created_at=NOW,
    )
    return JobContext(job=job, db=db, settings=settings, log=structlog.get_logger("test"))


async def _watch(db: Database, ext: str) -> None:
    async with db.transaction() as s:
        await watch_uc.add_watched_account(
            s, SYSTEM, platform="x", external_id=ext, category="media_outlet"
        )


async def test_batch_outcomes_and_watched_first(
    db: Database, fake_embeds: FakeEmbeds, reporter: ErrorReporter
) -> None:
    await _watch(db, "500")
    await _ingest(db, [_raw("201", "Texte exact", author="900")], source="extension")
    await _ingest(db, [_raw("202", "Texte inventé")], source="extension")
    await _ingest(db, [_raw("203", "Post disparu", author="900")], source="extension")
    fake_embeds.posts = {
        "201": EmbedPost("Texte exact", "lemedia"),
        "202": EmbedPost("Ce que le compte a vraiment écrit", "lemedia"),
        "203": None,
    }
    await verify.verify_batch(_ctx(db, make_settings(oembed_per_minute=10)))
    assert fake_embeds.calls[0] == "202"  # watched author first
    assert {e: (await _content(db, e)).verification for e in ("201", "202", "203")} == {
        "201": "verified",
        "202": "mismatch",
        "203": "unavailable",
    }
    assert (await _content(db, "201")).checked_at is not None
    verified = await _events(db, "content.verified")
    assert [e["source"] for e in verified] == ["oembed"]
    await reporter.flush()
    async with db.session() as s:
        stored = (
            await s.execute(text("SELECT count(*) FROM error_reports WHERE code = 'TWR-ING-012'"))
        ).scalar_one()
    assert stored == 1
    # Each content is checked once.
    await verify.verify_batch(_ctx(db, make_settings(oembed_per_minute=10)))
    assert len(fake_embeds.calls) == 3


async def test_batch_limits(db: Database, fake_embeds: FakeEmbeds) -> None:
    await _ingest(
        db, [_raw(str(300 + i), f"Texte numéro {i}") for i in range(6)], source="extension"
    )
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE content_items SET first_seen_at = :t WHERE external_id = '305'"),
            {"t": NOW - timedelta(hours=30)},
        )
    fake_embeds.posts = {str(300 + i): EmbedPost(f"Texte numéro {i}", None) for i in range(6)}

    await verify.verify_batch(_ctx(db, make_settings(oembed_per_minute=2)))
    assert len(fake_embeds.calls) == 2
    # Daily cap: 3 per day, 2 already used today.
    await verify.verify_batch(_ctx(db, make_settings(oembed_per_minute=10, oembed_daily_max=3)))
    assert len(fake_embeds.calls) == 3
    await verify.verify_batch(_ctx(db, make_settings(oembed_per_minute=10, oembed_daily_max=3)))
    assert len(fake_embeds.calls) == 3
    # Without the cap, the content first seen 30 h ago is still never checked.
    await verify.verify_batch(_ctx(db, make_settings(oembed_per_minute=10)))
    assert sorted(fake_embeds.calls) == ["300", "301", "302", "303", "304"]
    # Disabled: nothing.
    fake_embeds.calls.clear()
    await verify.verify_batch(_ctx(db, make_settings(oembed_enabled=False)))
    assert fake_embeds.calls == []


async def test_batch_pauses_on_rate_limit_and_outage(db: Database, fake_embeds: FakeEmbeds) -> None:
    await _ingest(db, [_raw("401", "Un"), _raw("402", "Deux")], source="extension")
    fake_embeds.posts = {
        "401": PlatformRateLimited("429", retry_after=600.0),
        "402": PlatformRateLimited("429", retry_after=600.0),
    }
    settings = make_settings(oembed_per_minute=10, oembed_concurrency=1)
    with pytest.raises(RetryLater) as paused:
        await verify.verify_batch(_ctx(db, settings))
    assert paused.value.delay == 600.0
    assert len(fake_embeds.calls) == 1  # the pause stops the batch
    assert (await _content(db, "401")).checked_at is None

    fake_embeds.calls.clear()
    fake_embeds.posts = {"401": PlatformUnavailable("503"), "402": PlatformUnavailable("503")}
    with pytest.raises(RetryLater) as down:
        await verify.verify_batch(_ctx(db, settings))
    assert down.value.delay == verify.PAUSE_ON_FAILURE_S
    assert (await _content(db, "402")).verification == "unverified"


async def test_schedule_is_deduplicated(db: Database) -> None:
    settings = make_settings()
    async with db.transaction() as s:
        assert await verify.schedule_verification(s, settings) == 1
        assert await verify.schedule_verification(s, settings) == 0
        assert await verify.schedule_verification(s, make_settings(oembed_enabled=False)) == 0
    assert len(await jobs_of(db, verify.JOB_VERIFY)) == 1


# ------------------------------------------------------------- targeted fact-check


async def test_n3_waits_for_trusted_content(db: Database, fake_embeds: FakeEmbeds) -> None:
    await _watch(db, "500")
    await _ingest(db, [_raw("501", "Le chômage est à 7 % en France.")], source="extension")
    row = await _content(db, "501")
    ingested = event(names.CONTENT_INGESTED, {"id": str(row.id), "lang": row.lang})
    async with db.transaction() as s:
        await on_content_ingested(s, [ingested])
    assert await jobs_of(db, names.JOB_EXTRACT) == []  # declared only: no N3

    fake_embeds.posts = {"501": EmbedPost("Le chômage est à 7 % en France.", "lemedia")}
    await verify.verify_batch(_ctx(db, make_settings()))
    payload = (await _events(db, "content.verified"))[0]
    async with db.transaction() as s:
        await on_content_ingested(s, [event(names.CONTENT_VERIFIED, payload, 2)])
    queued = await jobs_of(db, names.JOB_EXTRACT)
    assert [q["content_id"] for q in queued] == [row.id]
    assert queued[0]["level"] == "N3"
