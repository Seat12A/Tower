#!/usr/bin/env python3
"""Wave load test for 12A Tower (lot 5).

Posts a synthetic wave (variants of one French rumour spread over many fake authors and
installations, plus unrelated background posts) through ``POST /v1/ingest/observations``
against running processes, then prints ingestion throughput, the ``content.ingested`` to
``draft.ready`` delay of the watched (N3) posts, queue backlog per queue, narrative state
transitions, the sampled-out share and the cost per post.

Standalone: not part of the package, not collected by pytest. Dependencies: httpx and
asyncpg (both already project dependencies). See docs/development.md ("Test de charge").

    TOWER_LLM_FAKE=1 uv run tower gate / hangar   (see docs/development.md)
    uv run python scripts/loadtest_wave.py --gate-url http://127.0.0.1:8765 \\
        --database-url postgresql://postgres@localhost:5433/tower_smoke \\
        --posts 5000 --minutes 10 --watched-post-every 60 --watched-post-file posts.json
"""

# ruff: noqa: S311, E501
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import random
import secrets
import statistics
import sys
import time
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any

BATCH_ITEMS = 50
PER_INSTALLATION_PER_REQUEST = 50
FILTER_VERSION = "loadtest.1"

RUMOUR_FIGURES = ("12 000", "15 000", "30 000", "45 000", "2 millions", "80 %")
RUMOUR_TEMPLATES = (
    "URGENT : le gouvernement cache que {n} personnes ont été touchées par le scandale de l'eau du robinet #{tag}",
    "On nous ment : {n} personnes touchées par l'eau du robinet contaminée, partagez avant la censure #{tag}",
    "Scandale de l'eau du robinet : {n} victimes selon des documents internes. Retweetez ! #{tag}",
    "Eau du robinet contaminée, {n} personnes malades et personne n'en parle #{tag}",
)
TRANSLATIONS = (
    "[fr: {n} personnes touchées par l'eau du robinet contaminée, le gouvernement le cache] "
    "Contaminated tap water, {n} people affected, the government hides it #{tag}",
    "[fr: scandale de l'eau du robinet, {n} victimes] Wasserskandal: {n} Betroffene #{tag}",
)
BACKGROUND_TOPICS = (
    "Le prix de la baguette a augmenté de {n} centimes cette année dans ma boulangerie",
    "Match nul hier soir : {n} spectateurs au stade, ambiance incroyable",
    "La ligne de métro {n} sera fermée ce week-end pour travaux",
    "Nouvelle exposition au musée : {n} oeuvres présentées jusqu'en mars",
    "Le marché de Noël ouvre demain avec {n} stands cette année",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="loadtest_wave.py",
        description="Synthetic wave through POST /v1/ingest/observations, with measurements.",
    )
    p.add_argument("--gate-url", default="http://127.0.0.1:8765", help="gate base URL")
    p.add_argument(
        "--database-url",
        default=os.environ.get("TOWER_DATABASE_URL", ""),
        help="PostgreSQL URL for the measurements (default: $TOWER_DATABASE_URL)",
    )
    p.add_argument("--posts", type=int, default=2000, help="posts of the wave (rumour variants)")
    p.add_argument("--minutes", type=float, default=5.0, help="duration of the injection")
    p.add_argument(
        "--narrative-variants", type=int, default=30, help="distinct variants of the rumour"
    )
    p.add_argument(
        "--background-share", type=float, default=0.3, help="unrelated posts, share of --posts"
    )
    p.add_argument(
        "--authors", type=int, default=0, help="fake authors (default: posts / 3, at least 50)"
    )
    p.add_argument(
        "--installations",
        type=int,
        default=0,
        help="installation tokens (default: enough for the per-installation daily quota)",
    )
    p.add_argument(
        "--watched-post-every",
        type=int,
        default=0,
        metavar="SECONDS",
        help="one post of the watched account every SECONDS (0: none)",
    )
    p.add_argument(
        "--watched-post-file",
        metavar="PATH",
        help="posts.json of the fake X server (docs/factcheck/prompts/lot-5-briefs/fake_x.py): "
        "watched posts are appended there and picked up by the radar service",
    )
    p.add_argument("--watched-author-id", default="1001", help="fake X user id of the account")
    p.add_argument(
        "--watched-service-token",
        help="alternative to --watched-post-file: service token with content:ingest; watched "
        "posts are then posted directly with source=radar",
    )
    p.add_argument("--watched-handle", default="AccountA", help="handle of the watched account")
    p.add_argument("--drain-seconds", type=float, default=300.0, help="wait after the injection")
    p.add_argument("--sample-seconds", type=float, default=10.0, help="backlog sampling period")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--concurrency", type=int, default=8, help="parallel requests")
    return p.parse_args(argv)


# ------------------------------------------------------------------------- generation


def installation_token() -> str:
    """A valid installation token: base64url, 22 to 64 characters."""
    return base64.urlsafe_b64encode(secrets.token_bytes(24)).decode().rstrip("=")


def _fake_id(rng: random.Random) -> str:
    return str(rng.randrange(10**18, 2 * 10**18))


def rumour_text(rng: random.Random, variant: int) -> str:
    """Variants: exact copies (same variant id), near copies (hashtag), new figures,
    translations with the ``[fr: …]`` marker."""
    kind = variant % 10
    figure = RUMOUR_FIGURES[0] if kind < 6 else rng.choice(RUMOUR_FIGURES)
    tag = f"eau{variant}"
    if kind == 9:
        return TRANSLATIONS[variant % len(TRANSLATIONS)].format(n=figure, tag=tag)
    template = RUMOUR_TEMPLATES[variant % len(RUMOUR_TEMPLATES)]
    return template.format(n=figure, tag=tag if kind >= 3 else "eau")


def background_text(rng: random.Random) -> str:
    return rng.choice(BACKGROUND_TOPICS).format(n=rng.randrange(2, 9999))


def make_item(
    rng: random.Random, text: str, author: int, *, now: datetime, views: int
) -> dict[str, Any]:
    created = now - timedelta(seconds=rng.randrange(0, 600))
    return {
        "platform": "x",
        "external_id": _fake_id(rng),
        "author": {"external_id": str(10**9 + author), "handle": f"lt_user_{author}"},
        "text": text,
        "lang_hint": "fr",
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metrics": {"views": views, "likes": views // 50},
        "observed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def plan_wave(args: argparse.Namespace) -> list[tuple[float, str, int]]:
    """``(offset seconds, text, author index)`` of every post, sorted by time. The rate is
    a ramp (slow start, peak, decline) so that the wave detection sees growth."""
    rng = random.Random(args.seed)
    authors = args.authors or max(50, args.posts // 3)
    duration = max(args.minutes * 60.0, 1.0)
    rumour = int(args.posts * (1 - args.background_share))
    plan: list[tuple[float, str, int]] = []
    for i in range(args.posts):
        # Triangular distribution around 60 % of the duration.
        offset = rng.triangular(0, duration, duration * 0.6)
        if i < rumour:
            variant = rng.randrange(max(1, args.narrative_variants))
            plan.append((offset, rumour_text(rng, variant), rng.randrange(authors)))
        else:
            plan.append((offset, background_text(rng), rng.randrange(authors)))
    plan.sort(key=lambda row: row[0])
    return plan


def installations_needed(args: argparse.Namespace) -> int:
    return args.installations or max(10, math.ceil(args.posts / 400))


# ------------------------------------------------------------------------- measurements


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def fmt(value: float | None, unit: str = "") -> str:
    return "n/a" if value is None else f"{value:.1f}{unit}"


async def sample_backlog(conn: Any, stop: asyncio.Event, period: float) -> list[dict[str, Any]]:
    """Queued jobs per queue over time (SQL on ``jobs``)."""
    series: list[dict[str, Any]] = []
    started = time.monotonic()
    while True:
        rows = await conn.fetch(
            "SELECT queue, count(*) AS n FROM jobs WHERE status IN ('queued', 'running') "
            "GROUP BY queue"
        )
        series.append(
            {"t": time.monotonic() - started, "queues": {r["queue"]: r["n"] for r in rows}}
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=period)
            return series
        except TimeoutError:
            continue


async def watched_delays(conn: Any, since: datetime, handle: str) -> list[float]:
    """Seconds from ``content.ingested`` to ``draft.ready`` for the watched account's
    posts ingested since ``since``."""
    rows = await conn.fetch(
        """
        SELECT extract(epoch FROM (min(d.created_at) - c.first_seen_at)) AS delay
        FROM content_items c
        JOIN platform_accounts a ON a.id = c.author_account_id
        JOIN events d ON d.topic = 'draft.ready'
             AND d.payload->>'external_id' = c.external_id
        WHERE lower(a.handle) = lower($1) AND c.first_seen_at >= $2
        GROUP BY c.id, c.first_seen_at
        """,
        handle,
        since,
    )
    return [float(r["delay"]) for r in rows if r["delay"] is not None]


async def report_database(
    conn: Any, since: datetime, handle: str, backlog: list[dict[str, Any]], ingested: int
) -> dict[str, Any]:
    delays = await watched_delays(conn, since, handle)
    transitions = await conn.fetch(
        "SELECT narrative_id AS id, kind || coalesce(' ' || from_state || ' -> ' || to_state, '') "
        "AS kind, created_at FROM narrative_events WHERE created_at >= $1 "
        "ORDER BY created_at",
        since,
    )
    first_surge = await conn.fetchval(
        "SELECT min(created_at) FROM events WHERE topic = 'narrative.surge_detected' "
        "AND created_at >= $1",
        since,
    )
    first_surge_draft = None
    if first_surge is not None:
        first_surge_draft = await conn.fetchval(
            "SELECT min(created_at) FROM events WHERE topic = 'draft.ready' AND created_at >= $1",
            first_surge,
        )
    statuses = await conn.fetch(
        "SELECT status, count(*) AS n FROM content_extractions WHERE created_at >= $1 "
        "GROUP BY status",
        since,
    )
    by_status = {r["status"]: r["n"] for r in statuses}
    calls = await conn.fetchrow(
        "SELECT count(*) AS n, coalesce(sum(cost_usd_micros), 0) AS cost FROM llm_calls "
        "WHERE created_at >= $1",
        since,
    )
    budgets = await conn.fetch(
        "SELECT scope FROM budget_counters "
        "WHERE current_date >= period_start AND current_date < period_end "
        "AND spent_usd_micros > cap_usd_micros"
    )
    sampled_total = sum(by_status.values())
    return {
        "watched_delays": delays,
        "transitions": [(r["id"], r["kind"], r["created_at"]) for r in transitions],
        "first_surge": first_surge,
        "first_surge_draft": first_surge_draft,
        "extraction_status": by_status,
        "sampled_out_share": (by_status.get("sampled_out", 0) / sampled_total)
        if sampled_total
        else None,
        "llm_calls": calls["n"],
        "cost_per_post_usd": (calls["cost"] / 1_000_000 / ingested) if ingested else None,
        "budgets_over_cap": [r["scope"] for r in budgets],
        "peak_backlog": {
            q: max((s["queues"].get(q, 0) for s in backlog), default=0)
            for q in sorted({q for s in backlog for q in s["queues"]})
        },
    }


# ------------------------------------------------------------------------- injection


def fake_ip(token: str) -> str:
    """One stable private address per installation: gate rate-limits per IP (token bucket),
    and real installations come from many addresses. Local load tests only."""
    n = zlib.crc32(token.encode())
    return f"10.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}"


async def post_batches(
    client: Any,
    url: str,
    plan: list[tuple[float, str, int]],
    tokens: list[str],
    args: argparse.Namespace,
    started: float,
    stats: dict[str, int],
) -> None:
    """Send the plan in real time, batches of 50, rotating over the installation tokens
    (the per-installation quota is respected by spreading the load)."""
    rng = random.Random(args.seed + 1)
    gate = asyncio.Semaphore(args.concurrency)
    tasks: list[asyncio.Task[None]] = []
    turn = 0

    async def send(token: str, items: list[dict[str, Any]]) -> None:
        async with gate:
            try:
                response = await client.post(
                    f"{url}/v1/ingest/observations",
                    headers={"X-Installation-Token": token, "X-Forwarded-For": fake_ip(token)},
                    json={"filter_version": FILTER_VERSION, "items": items},
                    timeout=60,
                )
            except Exception:
                stats["failed_requests"] += 1
                return
        if response.status_code == 202:
            body = response.json()
            stats["accepted"] += body.get("accepted", 0)
            stats["new"] += body.get("new", 0)
            stats["rejected"] += body.get("rejected", 0)
        else:
            stats["failed_requests"] += 1
            stats[f"http_{response.status_code}"] += 1

    index = 0
    while index < len(plan):
        due = plan[index][0]
        wait = started + due - time.monotonic()
        if wait > 0:
            await asyncio.sleep(min(wait, 1.0))
            continue
        chunk = []
        now = datetime.now(UTC)
        while (
            index < len(plan)
            and len(chunk) < BATCH_ITEMS
            and started + plan[index][0] <= time.monotonic()
        ):
            _, text, author = plan[index]
            chunk.append(make_item(rng, text, author, now=now, views=rng.randrange(50, 50000)))
            index += 1
        token = tokens[turn % len(tokens)]
        turn += 1
        tasks.append(asyncio.create_task(send(token, chunk)))
    await asyncio.gather(*tasks)


def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


async def watched_posts(args: argparse.Namespace, client: Any, stop: asyncio.Event) -> int:
    """Regular posts of the watched account, until ``stop``. Through the fake X server
    (radar) when ``--watched-post-file`` is given, else directly with a service token."""
    sent = 0
    while not stop.is_set():
        text = f"Communiqué officiel numéro {sent + 1} : le gouvernement publie {random.randrange(10, 999)} mesures."
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        post_id = str(random.randrange(10**18, 2 * 10**18))
        if args.watched_post_file:
            data = await asyncio.to_thread(_read_json, args.watched_post_file)
            data["posts"].append(
                {
                    "id": post_id,
                    "author_id": args.watched_author_id,
                    "lang": "fr",
                    "text": text,
                    "created_at": stamp,
                }
            )
            await asyncio.to_thread(_write_json, args.watched_post_file, data)
        elif args.watched_service_token:
            item = {
                "platform": "x",
                "external_id": post_id,
                "author": {"external_id": args.watched_author_id, "handle": args.watched_handle},
                "text": text,
                "lang_hint": "fr",
                "created_at": stamp.replace(".000Z", "Z"),
                "observed_at": stamp.replace(".000Z", "Z"),
            }
            await client.post(
                f"{args.gate_url}/v1/ingest/observations",
                headers={"Authorization": f"Bearer {args.watched_service_token}"},
                json={"filter_version": FILTER_VERSION, "source": "radar", "items": [item]},
                timeout=30,
            )
        sent += 1
        try:
            await asyncio.wait_for(stop.wait(), timeout=args.watched_post_every)
        except TimeoutError:
            continue
    return sent


async def run(args: argparse.Namespace) -> int:
    import asyncpg
    import httpx

    if not args.database_url:
        print("--database-url (or TOWER_DATABASE_URL) is required for the measurements")
        return 2
    plan = plan_wave(args)
    tokens = [installation_token() for _ in range(installations_needed(args))]
    since = datetime.now(UTC)
    stats: dict[str, int] = {
        "accepted": 0,
        "new": 0,
        "rejected": 0,
        "failed_requests": 0,
    }
    stats = _counter(stats)
    conn = await asyncpg.connect(args.database_url.replace("postgresql+asyncpg", "postgresql"))
    stop = asyncio.Event()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            backlog_task = asyncio.create_task(sample_backlog(conn, stop, args.sample_seconds))
            watched_stop = asyncio.Event()
            watched_task = (
                asyncio.create_task(watched_posts(args, client, watched_stop))
                if args.watched_post_every > 0
                and (args.watched_post_file or args.watched_service_token)
                else None
            )
            started = time.monotonic()
            await post_batches(client, args.gate_url, plan, tokens, args, started, stats)
            elapsed = time.monotonic() - started
            await asyncio.sleep(args.drain_seconds)
            watched_stop.set()
            watched_sent = await watched_task if watched_task else 0
            stop.set()
            backlog = await backlog_task
        measured = await report_database(
            conn, since, args.watched_handle, backlog, stats["accepted"]
        )
    finally:
        await conn.close()
    print_report(args, stats, elapsed, watched_sent, measured)
    return 0


def _counter(initial: dict[str, int]) -> Any:
    from collections import defaultdict

    counter: defaultdict[str, int] = defaultdict(int)
    counter.update(initial)
    return counter


def print_report(
    args: argparse.Namespace,
    stats: dict[str, int],
    elapsed: float,
    watched_sent: int,
    measured: dict[str, Any],
) -> None:
    delays = measured["watched_delays"]
    print(
        f"Ingestion: {stats['accepted']} accepted ({stats['new']} new, {stats['rejected']} "
        f"rejected, {stats['failed_requests']} failed requests) in {elapsed:.1f}s, "
        f"{stats['accepted'] / max(elapsed, 1e-9):.1f} posts/s"
    )
    print(
        f"Watched posts sent: {watched_sent}, drafts measured: {len(delays)}; "
        f"ingested to draft.ready p50 {fmt(percentile(delays, 0.5), 's')}, "
        f"p95 {fmt(percentile(delays, 0.95), 's')}"
        + (f", mean {statistics.fmean(delays):.1f}s" if delays else "")
    )
    print("Peak backlog per queue: " + (json.dumps(measured["peak_backlog"]) or "{}"))
    print("Narrative transitions:")
    for narrative_id, kind, when in measured["transitions"][:40]:
        print(f"  {when:%H:%M:%S} narrative {narrative_id} {kind}")
    print(
        f"First surge: {measured['first_surge']}; first draft after it: "
        f"{measured['first_surge_draft']}"
    )
    share = measured["sampled_out_share"]
    print(
        f"Extraction statuses: {measured['extraction_status']}; sampled-out share: "
        f"{'n/a' if share is None else f'{share * 100:.1f}%'}"
    )
    cost = measured["cost_per_post_usd"]
    print(
        f"Model and embedding calls: {measured['llm_calls']}; cost per post: "
        f"{'n/a' if cost is None else f'{cost:.8f} $'}"
    )
    over = measured["budgets_over_cap"]
    print("Budget counters over cap: " + (", ".join(over) if over else "none"))


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
