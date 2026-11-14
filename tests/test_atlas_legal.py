# ruff: noqa: E501
"""Lot 6 legal sources: LEGI sync (tar.gz stream), LEGI mirror, EUR-Lex, Fedlex, Canada."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from tests.conftest import make_settings
from tower.core.adapters.knowledge.canada_laws import CanadaLawsSource, match_acts, section_number
from tower.core.adapters.knowledge.eurlex import EurLexSource, celex_candidates
from tower.core.adapters.knowledge.fedlex import FedlexSource, sr_numbers
from tower.core.adapters.knowledge.legi_mirror import (
    LegiMirrorSource,
    article_at,
    parse_reference,
)
from tower.core.adapters.knowledge.legi_sync import (
    ArchiveTooLarge,
    TruncatedArchive,
    apply_archive,
    parse_listing,
    plan_sync,
    sync_legi,
)
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.atlas import LEGI_PRIORITY_TEXTS
from tower.core.errors import ErrorReporter
from tower.core.factcheck import names
from tower.core.jobs.queue import PermanentError, RetryLater
from tower.core.models.atlas import AtlasSyncState
from tower.core.ports.knowledge import SourceQuery, SourceUnavailable, Tier
from tower.core.services.base import JobContext
from tower.services.atlas import legal

UA = "Seat12A-Tower/test"
PENAL = "LEGITEXT000006070719"
TRAVAIL = "LEGITEXT000006072050"
CONSTITUTION = "LEGITEXT000006071194"
OTHER = "LEGITEXT000006069414"  # not in the priority list
TEXTS = frozenset(LEGI_PRIORITY_TEXTS)

LISTING = """<html><body><pre>
<a href="?C=N;O=D">Name</a> <a href="/OPENDATA/">Parent</a>
<a href="DILA_LEGI_Presentation_20170824.pdf">pdf</a>
<a href="Freemium_legi_global_20261101-140000.tar.gz">g1</a>
<a href="Freemium_legi_global_20261108-140000.tar.gz">g2</a>
<a href="LEGI_20261105-210000.tar.gz">d0</a>
<a href="LEGI_20261109-212854.tar.gz">d1</a>
<a href="LEGI_20261110-213159.tar.gz">d2</a>
</pre></body></html>"""


# --- archive builders ---------------------------------------------------------------------


def article_xml(
    art_id: str,
    num: str,
    etat: str,
    debut: str,
    fin: str,
    body: str,
    *,
    cid: str = PENAL,
    title: str = "Code pénal",
    nature: str = "CODE",
) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ARTICLE><META><META_COMMUN><ID>{art_id}</ID><NATURE>Article</NATURE></META_COMMUN>
<META_SPEC><META_ARTICLE><NUM>{num}</NUM><ETAT>{etat}</ETAT>
<DATE_DEBUT>{debut}</DATE_DEBUT><DATE_FIN>{fin}</DATE_FIN></META_ARTICLE></META_SPEC></META>
<CONTEXTE><TEXTE cid="{cid}" nature="{nature}"><TITRE_TXT>{title}</TITRE_TXT>
<TM><TITRE_TM>Partie législative</TITRE_TM><TM><TITRE_TM>Livre IV</TITRE_TM></TM></TM>
</TEXTE></CONTEXTE>
<BLOC_TEXTUEL><CONTENU><p>{body}</p></CONTENU></BLOC_TEXTUEL></ARTICLE>""".encode()


def member_path(cid: str, art_id: str) -> str:
    return (
        "20261109-212854/legi/global/code_et_TNC_en_vigueur/code_en_vigueur/LEGI/TEXT/"
        f"00/00/06/07/07/{cid}/article/LEGI/ARTI/00/00/33/91/27/{art_id}.xml"
    )


def make_archive(members: Mapping[str, bytes], *, deletions: list[str] | None = None) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        root = tarfile.TarInfo("20261109-212854")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if deletions is not None:
            data = ("\n".join(deletions) + "\n").encode()
            info = tarfile.TarInfo("20261109-212854/liste_suppression_legi.dat")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


A_OLD = "LEGIARTI000006418523"
A_NEW = "LEGIARTI000033912762"
A_ABROGE = "LEGIARTI000006418600"
A_TRAVAIL = "LEGIARTI000006900001"
A_CONST = "LEGIARTI000006527400"
A_OTHER = "LEGIARTI000006999999"
A_DELETED = "LEGIARTI000006111111"


def base_members() -> dict[str, bytes]:
    return {
        member_path(PENAL, A_OLD): article_xml(
            A_OLD,
            "432-13",
            "MODIFIE",
            "1994-03-01",
            "2002-01-01",
            "Ancienne rédaction prise illégale",
        ),
        member_path(PENAL, A_NEW): article_xml(
            A_NEW,
            "432-13",
            "VIGUEUR",
            "2017-01-22",
            "2999-01-01",
            "Est puni de trois ans d'emprisonnement la prise illégale d'intérêts",
        ),
        member_path(PENAL, A_ABROGE): article_xml(
            A_ABROGE, "432-12", "ABROGE", "1994-03-01", "2000-06-01", "Disposition abrogée jadis"
        ),
        member_path(TRAVAIL, A_TRAVAIL): article_xml(
            A_TRAVAIL,
            "L1234-5",
            "VIGUEUR",
            "2008-05-01",
            "2999-01-01",
            "Indemnité compensatrice de préavis du licenciement",
            cid=TRAVAIL,
            title="Code du travail",
        ),
        member_path(CONSTITUTION, A_CONST): article_xml(
            A_CONST,
            "49",
            "VIGUEUR",
            "2008-07-24",
            "2999-01-01",
            "Le Premier ministre engage la responsabilité du Gouvernement",
            cid=CONSTITUTION,
            title="Constitution du 4 octobre 1958",
        ),
        member_path(OTHER, A_OTHER): article_xml(
            A_OTHER, "1", "VIGUEUR", "2000-01-01", "2999-01-01", "Hors périmètre", cid=OTHER
        ),
        "20261109-212854/legi/global/some/other/file.txt": b"ignored",
    }


async def chunks(data: bytes, size: int = 1000) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


@pytest.fixture
async def legal_db(db: Database) -> Database:
    async with db.transaction() as session:
        await session.execute(text("TRUNCATE legal_articles, atlas_sync_state RESTART IDENTITY"))
    return db


async def rows(db: Database) -> dict[str, Any]:
    async with db.session() as session:
        result = await session.execute(
            text(
                "SELECT article_id, text_id, number, status, valid_from, valid_to, "
                "section_path, url, body FROM legal_articles ORDER BY article_id"
            )
        )
        return {r.article_id: r for r in result}


# --- listing and plan ---------------------------------------------------------------------


def test_parse_listing_and_plan() -> None:
    archives = parse_listing(LISTING)
    assert archives[0].name == "Freemium_legi_global_20261101-140000.tar.gz"
    assert [a.kind for a in archives] == ["global", "diff", "global", "diff", "diff"]
    # No state: latest global, then only the diffs after it (the 11-05 diff is older).
    plan = plan_sync(archives, None)
    assert [a.name for a in plan] == [
        "Freemium_legi_global_20261108-140000.tar.gz",
        "LEGI_20261109-212854.tar.gz",
        "LEGI_20261110-213159.tar.gz",
    ]
    # With state: newer diffs only, never a global.
    plan = plan_sync(archives, "LEGI_20261109-212854.tar.gz")
    assert [a.name for a in plan] == ["LEGI_20261110-213159.tar.gz"]
    assert plan_sync(archives, "LEGI_20261110-213159.tar.gz") == []
    assert plan_sync([], None) == []


# --- streaming apply ----------------------------------------------------------------------


async def test_apply_in_tiny_chunks_matches_one_chunk(legal_db: Database) -> None:
    archive = make_archive(base_members(), deletions=[])
    stats = await apply_archive(
        legal_db, chunks(archive, 1000), texts=TEXTS, archive_name="a", max_bytes=10**9
    )
    first = await rows(legal_db)
    assert set(first) == {A_OLD, A_NEW, A_ABROGE, A_TRAVAIL, A_CONST}  # OTHER ignored
    assert stats.upserted == 5 and stats.unchanged == 0 and stats.skipped == 0
    assert stats.members >= 7 and stats.bytes == len(archive)
    async with legal_db.transaction() as session:
        await session.execute(text("TRUNCATE legal_articles"))
    await apply_archive(
        legal_db, chunks(archive, len(archive)), texts=TEXTS, archive_name="a", max_bytes=10**9
    )
    second = await rows(legal_db)
    assert {k: tuple(v) for k, v in first.items()} == {k: tuple(v) for k, v in second.items()}
    new = first[A_NEW]
    assert new.number == "432-13" and new.status == "VIGUEUR"
    assert new.valid_from == date(2017, 1, 22) and new.valid_to is None
    assert new.section_path == "Partie législative > Livre IV"
    assert new.url == f"https://www.legifrance.gouv.fr/codes/article_lc/{A_NEW}"
    assert first[A_OLD].valid_to == date(2002, 1, 1)


async def test_long_name_member_is_read(legal_db: Database) -> None:
    long_dir = "x" * 120
    name = member_path(PENAL, A_NEW).replace("20261109-212854", long_dir)
    assert len(name) > 100
    archive = make_archive(
        {name: article_xml(A_NEW, "1", "VIGUEUR", "2017-01-22", "2999-01-01", "Texte long")}
    )
    stats = await apply_archive(
        legal_db, chunks(archive, 777), texts=TEXTS, archive_name="a", max_bytes=10**9
    )
    assert stats.upserted == 1
    assert list(await rows(legal_db)) == [A_NEW]


async def test_deletions_idempotence_and_changes(legal_db: Database) -> None:
    members = base_members()
    members[member_path(PENAL, A_DELETED)] = article_xml(
        A_DELETED, "1-1", "VIGUEUR", "2000-01-01", "2999-01-01", "A supprimer"
    )
    await apply_archive(
        legal_db, chunks(make_archive(members)), texts=TEXTS, archive_name="g", max_bytes=10**9
    )
    assert A_DELETED in await rows(legal_db)
    # Diff: deletion list + one changed article + one identical article.
    diff_members = {
        member_path(PENAL, A_NEW): article_xml(
            A_NEW, "432-13", "MODIFIE", "2017-01-22", "2024-01-01", "Texte modifié"
        ),
        member_path(PENAL, A_OLD): base_members()[member_path(PENAL, A_OLD)],
    }
    diff = make_archive(
        diff_members,
        deletions=[
            "legi/global/code_et_TNC_en_vigueur/code_en_vigueur/LEGI/TEXT/x/article/" + A_DELETED,
            "not-an-article-line",
        ],
    )
    stats = await apply_archive(
        legal_db, chunks(diff), texts=TEXTS, archive_name="d", max_bytes=10**9
    )
    assert stats.deleted == 1 and stats.upserted == 1 and stats.unchanged == 1
    state = await rows(legal_db)
    assert A_DELETED not in state
    assert state[A_NEW].status == "MODIFIE" and state[A_NEW].body == "Texte modifié"
    # Re-applying the same diff changes nothing (idempotent).
    again = await apply_archive(
        legal_db, chunks(diff), texts=TEXTS, archive_name="d", max_bytes=10**9
    )
    assert again.upserted == 0 and again.unchanged == 2 and again.deleted == 0


async def test_unsafe_xml_member_is_skipped_and_captured(legal_db: Database) -> None:
    evil = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "boom">]><ARTICLE><META/>&x;</ARTICLE>'
    members = {
        member_path(PENAL, "LEGIARTI000000000001"): evil,
        member_path(PENAL, A_NEW): base_members()[member_path(PENAL, A_NEW)],
    }
    stats = await apply_archive(
        legal_db, chunks(make_archive(members)), texts=TEXTS, archive_name="u", max_bytes=10**9
    )
    assert stats.unsafe == 1 and stats.skipped == 1 and stats.upserted == 1
    assert list(await rows(legal_db)) == [A_NEW]


async def test_oversized_member_is_skipped(legal_db: Database) -> None:
    huge = article_xml(A_NEW, "1", "VIGUEUR", "2017-01-22", "2999-01-01", "mot " * 600_000)
    stats = await apply_archive(
        legal_db,
        chunks(make_archive({member_path(PENAL, A_NEW): huge}), 65_536),
        texts=TEXTS,
        archive_name="h",
        max_bytes=10**9,
    )
    assert stats.skipped == 1 and stats.upserted == 0


async def test_size_cap_and_truncated_archive(legal_db: Database) -> None:
    archive = make_archive(base_members())
    with pytest.raises(ArchiveTooLarge):
        await apply_archive(
            legal_db, chunks(archive), texts=TEXTS, archive_name="c", max_bytes=len(archive) // 2
        )
    with pytest.raises(TruncatedArchive):
        await apply_archive(
            legal_db,
            chunks(archive[: len(archive) // 2]),
            texts=TEXTS,
            archive_name="t",
            max_bytes=10**9,
        )
    with pytest.raises(TruncatedArchive):
        await apply_archive(
            legal_db,
            chunks(b"not gzip at all" * 50),
            texts=TEXTS,
            archive_name="n",
            max_bytes=10**9,
        )


# --- sync orchestration -------------------------------------------------------------------


def legi_transport(
    archives: Mapping[str, bytes], *, fail: set[str] | None = None
) -> tuple[httpx.MockTransport, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if not name:
            return httpx.Response(200, text=LISTING)
        seen.append(name)
        if fail and name in fail:
            return httpx.Response(503)
        return httpx.Response(200, content=archives[name])

    return httpx.MockTransport(handler), seen


def diff_archive(art_id: str, body: str) -> bytes:
    return make_archive(
        {
            member_path(PENAL, art_id): article_xml(
                art_id, "1", "VIGUEUR", "2020-01-01", "2999-01-01", body
            )
        }
    )


async def test_sync_updates_state_per_archive_and_resumes(
    legal_db: Database, settings: Settings
) -> None:
    archives = {
        "Freemium_legi_global_20261108-140000.tar.gz": make_archive(base_members()),
        "LEGI_20261109-212854.tar.gz": diff_archive("LEGIARTI000000000010", "premier diff"),
        "LEGI_20261110-213159.tar.gz": diff_archive("LEGIARTI000000000011", "second diff"),
    }
    transport, seen = legi_transport(archives, fail={"LEGI_20261110-213159.tar.gz"})
    with pytest.raises(httpx.HTTPStatusError):
        await sync_legi(legal_db, settings, transport=transport)
    async with legal_db.session() as session:
        state = (await session.get(AtlasSyncState, "legi")).state  # type: ignore[union-attr]
    assert state["last_applied"] == "LEGI_20261109-212854.tar.gz"  # first two applied
    assert len(await rows(legal_db)) == 5 + 1
    # Resume: only the remaining diff is fetched.
    transport, seen = legi_transport(archives)
    report = await sync_legi(legal_db, settings, transport=transport)
    assert report.archives == ["LEGI_20261110-213159.tar.gz"]
    assert [n for n in seen if n.endswith(".gz")] == ["LEGI_20261110-213159.tar.gz"]
    assert report.stats.upserted == 1
    assert len(await rows(legal_db)) == 7
    # Up to date: nothing fetched.
    transport, seen = legi_transport(archives)
    assert (await sync_legi(legal_db, settings, transport=transport)).up_to_date
    assert [n for n in seen if n.endswith(".gz")] == []


async def test_sync_refuses_archive_over_cap(legal_db: Database) -> None:
    small = make_settings(atlas_legi_max_archive_mb=1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/"):
            return httpx.Response(200, text=LISTING)
        return httpx.Response(200, content=b"x", headers={"content-length": "5000000"})

    with pytest.raises(ArchiveTooLarge):
        await sync_legi(legal_db, small, transport=httpx.MockTransport(handler))


# --- job ---------------------------------------------------------------------------------


def job_context(db: Database, settings: Settings, attempts: int = 1) -> JobContext:
    from tower.core.jobs.queue import JobRecord
    from tower.core.logging import get_logger

    record = JobRecord(
        id=1,
        queue=names.ATLAS_QUEUE,
        kind=names.JOB_LEGI_SYNC,
        payload={},
        priority=0,
        attempts=attempts,
        max_attempts=3,
        dedupe_key=None,
        created_at=datetime.now(UTC),
    )
    return JobContext(job=record, db=db, settings=settings, log=get_logger("test"))


async def test_job_legi_sync_end_to_end(
    legal_db: Database, settings: Settings, reporter: ErrorReporter
) -> None:
    archives = {
        "Freemium_legi_global_20261108-140000.tar.gz": make_archive(base_members()),
        "LEGI_20261109-212854.tar.gz": diff_archive("LEGIARTI000000000010", "diff"),
        "LEGI_20261110-213159.tar.gz": diff_archive("LEGIARTI000000000011", "diff"),
    }
    transport, _ = legi_transport(archives)
    legal.set_legi_overrides(transport=transport)
    try:
        # The job never downloads the global dump: it needs the CLI bootstrap first.
        await legal.legi_sync(job_context(legal_db, settings))
        assert len(await rows(legal_db)) == 0
        report = await sync_legi(legal_db, settings, transport=transport, max_archives=1)
        assert report.archives == ["Freemium_legi_global_20261108-140000.tar.gz"]
        await legal.legi_sync(job_context(legal_db, settings))
        assert len(await rows(legal_db)) == 7

        # Failure: first attempt retries later, last attempt is permanent.
        def broken(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        legal.set_legi_overrides(transport=httpx.MockTransport(broken))
        with pytest.raises(RetryLater):
            await legal.legi_sync(job_context(legal_db, settings, attempts=1))
        with pytest.raises(PermanentError):
            await legal.legi_sync(job_context(legal_db, settings, attempts=3))
        await reporter.flush()
        async with legal_db.session() as session:
            codes = (await session.execute(text("SELECT code FROM error_reports"))).scalars().all()
        assert ec.ATL_LEGI_SYNC_FAILED.code in codes
    finally:
        legal.set_legi_overrides()


async def test_enqueue_legi_sync_dedupes(legal_db: Database, settings: Settings) -> None:
    async with legal_db.transaction() as session:
        assert await legal.enqueue_legi_sync(session, settings) == 1
        assert await legal.enqueue_legi_sync(session, settings) == 0
    async with legal_db.session() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM jobs WHERE kind = :k AND queue = :q"),
                {"k": names.JOB_LEGI_SYNC, "q": names.ATLAS_QUEUE},
            )
        ).scalar_one()
    assert count == 1


# --- LEGI mirror -------------------------------------------------------------------------


@pytest.fixture
async def mirror(legal_db: Database) -> LegiMirrorSource:
    await apply_archive(
        legal_db,
        chunks(make_archive(base_members())),
        texts=TEXTS,
        archive_name="g",
        max_bytes=10**9,
    )
    return LegiMirrorSource(legal_db)


def q(text_: str, as_of: str | None = None, **kw: Any) -> SourceQuery:
    when = datetime.fromisoformat(as_of).replace(tzinfo=UTC) if as_of else None
    return SourceQuery(text=text_, lang="fr", as_of=when, **kw)


def test_parse_reference_and_article_at() -> None:
    ref = parse_reference("l'article 432-13 du code pénal")
    assert ref is not None and ref.candidates == ("432-13",) and ref.text_id == PENAL
    ref = parse_reference("art. L. 1234-5 du Code du travail")
    assert ref is not None and ref.candidates == ("L1234-5",) and ref.text_id == TRAVAIL
    ref = parse_reference("l'article 49.3 de la Constitution")
    assert ref is not None and ref.candidates == ("49-3", "49") and ref.text_id == CONSTITUTION
    ref = parse_reference("article 78-2 du code de procédure pénale")
    assert ref is not None and ref.text_id == "LEGITEXT000006071154"
    assert parse_reference("la loi sur les retraites") is None

    class V:
        def __init__(self, a: date | None, b: date | None) -> None:
            self.valid_from, self.valid_to = a, b

    old, new = V(date(2000, 1, 1), date(2010, 1, 1)), V(date(2010, 1, 1), None)
    assert article_at([old, new], date(2005, 1, 1)) is old
    assert article_at([old, new], date(2010, 1, 1)) is new
    assert article_at([old], date(2020, 1, 1)) is old  # none in force: latest version
    assert article_at([], date(2020, 1, 1)) is None


async def test_mirror_exact_lookup_picks_version_in_force(mirror: LegiMirrorSource) -> None:
    now = await mirror.lookup(q("Que dit l'article 432-13 du code pénal ?"))
    assert len(now) == 1
    ev = now[0]
    assert ev.stable_id == A_NEW and ev.tier is Tier.PRIMARY and ev.lang == "fr"
    assert ev.text.startswith(
        "Code pénal, article 432-13 (in force from 2017-01-22, still in force; status VIGUEUR): "
    )
    assert ev.extra["valid_from"] == "2017-01-22" and ev.extra["valid_to"] == ""
    assert ev.extra["status"] == "VIGUEUR" and ev.extra["in_force_at_as_of"] == "yes"
    assert ev.version == "2017-01-22" and ev.published_at == datetime(2017, 1, 22, tzinfo=UTC)
    past = await mirror.lookup(q("article 432-13 du code pénal", "1999-06-01"))
    assert past[0].stable_id == A_OLD
    assert past[0].text.startswith(
        "Code pénal, article 432-13 (in force from 1994-03-01 to 2002-01-01; status MODIFIE): "
    )
    assert past[0].extra["as_of"] == "1999-06-01"


async def test_mirror_shows_abrogated_status_and_other_codes(mirror: LegiMirrorSource) -> None:
    ev = (await mirror.lookup(q("article 432-12 du code pénal")))[0]
    assert ev.text.startswith(
        "Code pénal, article 432-12 (in force from 1994-03-01 to 2000-06-01; status ABROGE): "
    )
    assert ev.extra["in_force_at_as_of"] == "no"
    assert (await mirror.lookup(q("art. L. 1234-5 du code du travail")))[0].stable_id == A_TRAVAIL
    # "49.3" falls back to article 49 of the Constitution.
    assert (await mirror.lookup(q("l'article 49.3 de la Constitution")))[0].stable_id == A_CONST


async def test_mirror_full_text_search(mirror: LegiMirrorSource) -> None:
    found = await mirror.lookup(q("prise illégale d'intérêts emprisonnement", limit=3))
    assert [e.stable_id for e in found] == [A_NEW]
    assert found[0].text.startswith("Code pénal, article 432-13 (in force from 2017-01-22")
    # Nothing in force at the date: the latest version is returned with its status.
    old = await mirror.lookup(q("disposition abrogée", "2024-01-01"))
    assert [e.stable_id for e in old] == [A_ABROGE]
    assert "status ABROGE" in old[0].text
    assert await mirror.lookup(q("zzzz inconnu")) == []
    assert await mirror.lookup(q("")) == []


async def test_mirror_body_truncated(legal_db: Database) -> None:
    members = {
        member_path(PENAL, A_NEW): article_xml(
            A_NEW, "1", "VIGUEUR", "2017-01-22", "2999-01-01", "mot " * 2000
        )
    }
    await apply_archive(
        legal_db, chunks(make_archive(members)), texts=TEXTS, archive_name="g", max_bytes=10**9
    )
    ev = (await LegiMirrorSource(legal_db).lookup(q("article 1 du code pénal")))[0]
    assert len(ev.text) < 1_800 and ev.text.endswith("[…]")


# --- on-demand adapters ------------------------------------------------------------------


class DictCache:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.ttls: list[int] = []

    @staticmethod
    def _k(adapter: str, request: Mapping[str, object]) -> str:
        return adapter + json.dumps(dict(request), sort_keys=True, default=str)

    async def get(self, adapter: str, request: Mapping[str, object]) -> dict[str, Any] | None:
        return self.data.get(self._k(adapter, request))

    async def put(
        self,
        adapter: str,
        request: Mapping[str, object],
        response: Mapping[str, Any],
        *,
        ttl_s: int,
    ) -> None:
        self.data[self._k(adapter, request)] = dict(response)
        self.ttls.append(ttl_s)


def counting(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped), seen


GDPR_SPARQL = {
    "head": {"link": [], "vars": ["celex", "title", "date", "inforce"]},
    "results": {
        "distinct": False,
        "ordered": True,
        "bindings": [
            {
                "celex": {"type": "literal", "value": "32016R0679"},
                "title": {
                    "type": "literal",
                    "value": "Regulation (EU) 2016/679 of the European Parliament and of the "
                    "Council of 27 April 2016 on the protection of natural persons (GDPR)",
                },
                "date": {
                    "type": "literal",
                    "datatype": "http://www.w3.org/2001/XMLSchema#date",
                    "value": "2016-04-27",
                },
                "inforce": {
                    "type": "literal",
                    "datatype": "http://www.w3.org/2001/XMLSchema#boolean",
                    "value": "1",
                },
            }
        ],
    },
}


def test_celex_candidates() -> None:
    assert celex_candidates("Regulation (EU) 2016/679 says") == ["32016R0679"]
    assert celex_candidates("directive 2019/1937 on whistleblowers") == ["32019L1937"]
    assert celex_candidates("règlement (UE) n° 1215/2012") == ["32012R1215"]
    assert celex_candidates("see 32016R0679 and 32019L1937") == ["32016R0679", "32019L1937"]
    assert celex_candidates("nothing here") == []


async def test_eurlex_celex_lookup_cache_and_errors() -> None:
    transport, seen = counting(lambda r: httpx.Response(200, json=GDPR_SPARQL))
    cache = DictCache()
    src = EurLexSource(
        user_agent=UA, timeout=5, cache=cache, cache_ttl_s=10**9, transport=transport
    )
    out = await src.lookup(SourceQuery(text="Regulation (EU) 2016/679 on data", lang="en"))
    assert len(out) == 1 and out[0].stable_id == "32016R0679"
    assert out[0].url == "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32016R0679"
    assert out[0].text.startswith("Regulation (EU) 2016/679")
    assert "2016-04-27" in out[0].text and "in force: yes" in out[0].text
    assert out[0].tier is Tier.PRIMARY and out[0].extra["celex"] == "32016R0679"
    assert 'STR(?celex) = "32016R0679"' in seen[0].url.params["query"]
    assert seen[0].url.params["format"] == "application/sparql-results+json"
    assert cache.ttls == [7 * 86_400]  # capped at 7 days
    await src.lookup(SourceQuery(text="Regulation (EU) 2016/679 on data", lang="en"))
    assert len(seen) == 1  # cache hit
    assert await src.lookup(SourceQuery(text="hello", lang="en")) == []  # nothing to search
    title_query = await src.lookup(
        SourceQuery(text="persons who report breaches of union law", lang="en")
    )
    assert "bif:contains" in seen[-1].url.params["query"] and len(title_query) == 1
    await src.aclose()
    down = EurLexSource(
        user_agent=UA, timeout=5, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    with pytest.raises(SourceUnavailable) as exc:
        await down.lookup(SourceQuery(text="Regulation (EU) 2016/679", lang="en"))
    assert exc.value.code is ec.ATL_SOURCE_UNAVAILABLE
    await down.aclose()


FEDLEX_JSON = {
    "head": {"link": [], "vars": ["act", "sr", "title", "inforce", "abrogated"]},
    "results": {
        "distinct": False,
        "ordered": True,
        "bindings": [
            {
                "act": {"type": "uri", "value": "https://fedlex.data.admin.ch/eli/cc/1965/1_1_1"},
                "sr": {"type": "literal", "value": "312.3"},
                "title": {"type": "literal", "value": "Bundesratsbeschluss über Strafentscheide"},
                "inforce": {"type": "literal", "value": "1965-01-01"},
                "abrogated": {"type": "literal", "value": "1970-01-01"},
            },
            {
                "act": {
                    "type": "uri",
                    "value": "https://fedlex.data.admin.ch/eli/cc/54/757_781_799",
                },
                "sr": {"type": "literal", "value": "311.0"},
                "title": {"type": "literal", "value": "Code pénal suisse du 21 décembre 1937"},
                "inforce": {"type": "literal", "value": "1942-01-01"},
            },
        ],
    },
}


async def test_fedlex_sr_and_title_lookup() -> None:
    assert sr_numbers("selon l'RS 311.0 et SR 101") == ["311.0", "101"]
    transport, seen = counting(lambda r: httpx.Response(200, json=FEDLEX_JSON))
    cache = DictCache()
    src = FedlexSource(user_agent=UA, timeout=5, cache=cache, transport=transport)
    out = await src.lookup(SourceQuery(text="Que dit le Code pénal RS 311.0 ?", lang="fr"))
    assert [e.extra["sr"] for e in out] == ["311.0", "312.3"]  # in force first
    assert out[0].url == "https://fedlex.data.admin.ch/eli/cc/54/757_781_799"
    assert out[0].text.startswith(
        "Code pénal suisse du 21 décembre 1937 (SR 311.0; in force since 1942-01-01"
    )
    assert "still in force" in out[0].text and "no longer in force since 1970-01-01" in out[1].text
    assert out[0].lang == "fr" and out[0].tier is Tier.PRIMARY
    sparql = seen[0].url.params["query"]
    assert 'STR(?sr) = "311.0"' in sparql and "language/FRA" in sparql
    await src.lookup(SourceQuery(text="Que dit le Code pénal RS 311.0 ?", lang="fr"))
    assert len(seen) == 1
    await src.lookup(SourceQuery(text="Schweizerisches Strafgesetzbuch Diebstahl", lang="de"))
    assert 'CONTAINS(LCASE(STR(?title)), "strafgesetzbuch")' in seen[-1].url.params["query"]
    assert "language/DEU" in seen[-1].url.params["query"]
    assert await src.lookup(SourceQuery(text="loi", lang="fr")) == []
    await src.aclose()
    down = FedlexSource(
        user_agent=UA, timeout=5, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    with pytest.raises(SourceUnavailable):
        await down.lookup(SourceQuery(text="RS 311.0", lang="fr"))
    await down.aclose()


CANADA_INDEX = """<?xml version="1.0" encoding="utf-8"?>
<ActsRegsList><Acts>
<Act><UniqueId>C-46</UniqueId><OfficialNumber>C-46</OfficialNumber><Language>eng</Language>
<LinkToXML>http://laws-lois.justice.gc.ca/eng/XML/C-46.xml</LinkToXML>
<Title>Criminal Code</Title><CurrentToDate>2026-10-19</CurrentToDate>
<RegsMadeUnderAct><Reg idRef="556660e" /></RegsMadeUnderAct></Act>
<Act><UniqueId>C-46</UniqueId><OfficialNumber>C-46</OfficialNumber><Language>fra</Language>
<LinkToXML>http://laws-lois.justice.gc.ca/fra/XML/C-46.xml</LinkToXML>
<Title>Code criminel</Title><CurrentToDate>2026-10-19</CurrentToDate></Act>
<Act><UniqueId>A-1</UniqueId><OfficialNumber>A-1</OfficialNumber><Language>eng</Language>
<Title>Access to Information Act</Title><CurrentToDate>2026-10-19</CurrentToDate></Act>
</Acts><Regulations><Regulation id="1"><UniqueId>SOR-2007-151</UniqueId>
<Language>eng</Language><Title>MV Sonia Remission Order</Title>
<CurrentToDate>2026-10-19</CurrentToDate></Regulation></Regulations></ActsRegsList>"""

CANADA_ACT = """<?xml version="1.0"?><Statute xmlns:lims="http://justice.gc.ca/lims" in-force="yes">
<Identification><ShortTitle>Criminal Code</ShortTitle></Identification><Body>
<Heading level="1"><TitleText>Short Title</TitleText></Heading>
<Section lims:inforce-start-date="2003-01-01" lims:lastAmendedDate="2003-01-01">
<MarginalNote>Short title</MarginalNote><Label>1</Label><Text>This Act may be cited as the Criminal Code.</Text></Section>
<Section lims:inforce-start-date="2004-04-29" lims:lastAmendedDate="2026-07-18">
<MarginalNote>Public incitement of hatred</MarginalNote><Label>319</Label>
<Subsection><Label>(1)</Label><Text>Every one who, by communicating statements in any public place, incites hatred against any identifiable group is guilty of</Text>
<Paragraph><Label>(a)</Label><Text>an indictable offence; or</Text></Paragraph></Subsection></Section>
<Section lims:inforce-start-date="2004-04-29"><Label>320</Label><Text>Never reached.</Text></Section>
</Body></Statute>"""


def canada_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("Legis.xml"):
        return httpx.Response(200, content=CANADA_INDEX.encode())
    if path.endswith("C-46.xml"):
        return httpx.Response(200, content=CANADA_ACT.encode())
    return httpx.Response(404)


def test_canada_matching() -> None:
    index = [
        ["C-46", "eng", "Criminal Code", "2026-10-19"],
        ["C-46", "fra", "Code criminel", "2026-10-19"],
        ["A-1", "eng", "Access to Information Act", "2026-10-19"],
    ]
    assert match_acts(index, "section 319 of the Criminal Code", "eng")[0][0] == "C-46"
    assert match_acts(index, "article 319 du Code criminel", "fra")[0][1] == "fra"
    assert match_acts(index, "see C-46 s. 319", "eng")[0][0] == "C-46"
    assert match_acts(index, "something else", "eng") == []
    assert section_number("section 319 of the Criminal Code") == "319"
    assert section_number("art. 83.01 du Code criminel") == "83.01"
    assert section_number("the Criminal Code") is None


async def test_canada_section_lookup_cache_and_errors() -> None:
    transport, seen = counting(canada_handler)
    cache = DictCache()
    src = CanadaLawsSource(user_agent=UA, timeout=5, cache=cache, transport=transport)
    out = await src.lookup(SourceQuery(text="section 319 of the Criminal Code", lang="en"))
    assert len(out) == 1
    ev = out[0]
    assert ev.text.startswith(
        "Criminal Code, section 319 (current to 2026-10-19; this version in force since 2004-04-29): "
    )
    assert "Public incitement of hatred" in ev.text and "Never reached" not in ev.text
    assert ev.url == "https://laws-lois.justice.gc.ca/eng/acts/C-46/index.html"
    assert ev.extra["section"] == "319" and ev.lang == "en" and ev.tier is Tier.PRIMARY
    index = cache.data['canada_laws{"op": "index"}']["acts"]
    assert ["C-46", "fra", "Code criminel", "2026-10-19"] in index
    assert all(row[0] != "SOR-2007-151" for row in index)  # acts only, reduced rows
    assert len(seen) == 2
    await src.lookup(SourceQuery(text="section 319 of the Criminal Code", lang="en"))
    assert len(seen) == 2  # index and section both cached
    assert cache.ttls == [7 * 86_400, 7 * 86_400]
    fr = await src.lookup(SourceQuery(text="article 319 du Code criminel", lang="fr"))
    assert fr == [] or fr[0].lang == "fr"  # fra file not served by the mock: 404 → unavailable
    await src.aclose()


async def test_canada_missing_section_and_http_error() -> None:
    src = CanadaLawsSource(user_agent=UA, timeout=5, transport=httpx.MockTransport(canada_handler))
    assert await src.lookup(SourceQuery(text="section 999 of the Criminal Code", lang="en")) == []
    meta = await src.lookup(SourceQuery(text="the Criminal Code", lang="en"))
    assert "Metadata only" in meta[0].text and meta[0].extra["act"] == "C-46"
    await src.aclose()
    down = CanadaLawsSource(
        user_agent=UA, timeout=5, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    with pytest.raises(SourceUnavailable) as exc:
        await down.lookup(SourceQuery(text="section 319 of the Criminal Code", lang="en"))
    assert exc.value.code is ec.ATL_SOURCE_UNAVAILABLE
    await down.aclose()
    unsafe = CanadaLawsSource(
        user_agent=UA,
        timeout=5,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><ActsRegsList/>'
            )
        ),
    )
    with pytest.raises(SourceUnavailable) as exc2:
        await unsafe.lookup(SourceQuery(text="section 1 of the Criminal Code", lang="en"))
    assert exc2.value.code is ec.ATL_UNSAFE_XML
    await unsafe.aclose()
