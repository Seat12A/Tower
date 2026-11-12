"""Narratives and waves (pure, standard library only): window scores, tiers and state
machine, medoid election, attachment rule, novelty fingerprints and surge sampling.

See docs/factcheck/vagues.md. Nothing here knows an author, an account or a political side:
accounts are only ever counted.
"""

from __future__ import annotations

import json
import math
import random
import re
import unicodedata
from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "AttachAnchors",
    "AttachSignals",
    "WaveThresholds",
    "WindowStats",
    "elect_medoids",
    "engagement_of",
    "entity_key",
    "evaluate_attachment",
    "expected_count",
    "extract_links",
    "find_bridge",
    "growth",
    "next_state",
    "novelty_fingerprints",
    "novelty_takes",
    "parse_thresholds",
    "percentile",
    "poisson_score",
    "sampling_rate",
    "stratified_sample",
    "tier_of",
]

TIERS = ("active", "watch", "surge", "alert")
"""Tier names in increasing order (``active`` = below the watch tier)."""
WAVE_STATES = ("surge", "alert")
COOCCURRENCE_MIN_POSTS = 3
COOCCURRENCE_MIN_ACCOUNTS = 4  # "more than 3 accounts"
SPECIFIC_MEDIA_MAX_NARRATIVES = 3
NOVELTY_PERCENTILE_MIN_SAMPLES = 10


# ----------------------------------------------------------------------- scores


def expected_count(previous_posts: int, flow: int, previous_flow: int) -> float:
    """Posts expected in a window: the previous equal window scaled by the flow ratio."""
    if previous_flow <= 0:
        return float(previous_posts)
    return previous_posts * (flow / previous_flow)


def poisson_score(observed: int, expected: float) -> float:
    """``(observed - expected) / sqrt(expected + 5)``: stays defined on sparse counts
    (most 5-minute windows are empty), where a robust z-score degenerates."""
    return (observed - expected) / math.sqrt(max(expected, 0.0) + 5.0)


def growth(posts: int, previous_posts: int) -> float:
    """Ratio to the previous equal window (a previous window below 1 post counts as 1)."""
    return posts / max(previous_posts, 1)


# ------------------------------------------------------------------------ tiers


@dataclass(frozen=True, slots=True)
class WaveThresholds:
    watch_share: float = 0.01
    watch_accounts: int = 10
    surge_share: float = 0.03
    surge_accounts: int = 30
    surge_growth: float = 2.0
    alert_share: float = 0.08
    alert_accounts: int = 80


def parse_thresholds(raw: str) -> WaveThresholds:
    """Parse the ``fc_wave_thresholds`` JSON; missing keys keep their default.
    Raises ``ValueError`` on invalid JSON or non-numeric values."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid wave thresholds: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("wave thresholds must be a JSON object")
    base = WaveThresholds()

    def num(tier: str, key: str, default: float) -> float:
        section = data.get(tier, {})
        if not isinstance(section, dict):
            raise ValueError(f"wave thresholds: {tier!r} must be an object")
        value = section.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"wave thresholds: {tier}.{key} must be a number")
        return float(value)

    return WaveThresholds(
        watch_share=num("watch", "share", base.watch_share),
        watch_accounts=int(num("watch", "accounts", base.watch_accounts)),
        surge_share=num("surge", "share", base.surge_share),
        surge_accounts=int(num("surge", "accounts", base.surge_accounts)),
        surge_growth=num("surge", "growth", base.surge_growth),
        alert_share=num("alert", "share", base.alert_share),
        alert_accounts=int(num("alert", "accounts", base.alert_accounts)),
    )


@dataclass(frozen=True, slots=True)
class WindowStats:
    """One window (normally the last hour) of a narrative."""

    posts: int
    flow: int
    accounts: int
    platforms_at_watch: int = 0
    """Platforms on which the narrative separately reaches the watch tier."""
    growth: float = 0.0

    @property
    def share(self) -> float:
        return self.posts / self.flow if self.flow > 0 else 0.0


def tier_of(
    stats: WindowStats,
    thresholds: WaveThresholds,
    *,
    has_n3_group: bool = False,
    watched_theme: bool = False,
    growth_frozen: bool = False,
) -> str:
    """``active`` (below watch), ``watch``, ``surge`` or ``alert``.

    - watch: share and accounts floors;
    - surge: share, accounts and growth; or watch reached separately on 2 platforms; or a
      group of the narrative at N3 (the last two need the watch floors too: a lone post
      is never a wave);
    - alert: share and accounts floors; or a watched theme at watch or above.

    While the comparison bases are frozen (filter version change), the growth condition
    cannot be met: a changed extension filter looks like a wave, so growth is not trusted.
    """
    watch = stats.share >= thresholds.watch_share and stats.accounts >= thresholds.watch_accounts
    if (stats.share >= thresholds.alert_share and stats.accounts >= thresholds.alert_accounts) or (
        watch and watched_theme
    ):
        return "alert"
    growing = (not growth_frozen) and stats.growth >= thresholds.surge_growth
    if (
        stats.share >= thresholds.surge_share
        and stats.accounts >= thresholds.surge_accounts
        and growing
    ) or (watch and (stats.platforms_at_watch >= 2 or has_n3_group)):
        return "surge"
    return "watch" if watch else "active"


def next_state(
    current: str,
    computed: str,
    below_since: datetime | None,
    now: datetime,
    *,
    end_hours: int,
) -> tuple[str, datetime | None]:
    """State machine: up immediately; ``watch`` falls back at once; ``surge``/``alert``
    end only after ``end_hours`` below the watch tier. Returns ``(state, below_since)``."""
    rank = {name: i for i, name in enumerate(TIERS)}
    if rank[computed] > rank[current]:
        return computed, None
    if current in WAVE_STATES:
        if computed != "active":
            return current, None
        since = below_since or now
        if now - since >= timedelta(hours=end_hours):
            return "active", None
        return current, since
    if current == "watch" and computed == "active":
        return "active", None
    return current, None


# ----------------------------------------------------------------------- medoids


def elect_medoids(
    founder: int, candidates: Iterable[tuple[int, int]], *, size: int = 3
) -> list[int]:
    """Founder (fixed) then the groups of highest reach (ties: lowest id) up to ``size``."""
    others = sorted(
        {gid: reach for gid, reach in candidates if gid != founder}.items(),
        key=lambda item: (-item[1], item[0]),
    )
    return [founder, *(gid for gid, _ in others[: max(size - 1, 0)])]


# -------------------------------------------------------------------- attachment


@dataclass(frozen=True, slots=True)
class AttachSignals:
    semantic: bool = False
    cooccurrence: bool = False
    entity: bool = False
    media: bool = False

    def count(self) -> int:
        return sum((self.semantic, self.cooccurrence, self.entity, self.media))

    def non_semantic(self) -> int:
        return sum((self.cooccurrence, self.entity, self.media))

    def as_dict(self) -> dict[str, bool]:
        return {
            "semantic": self.semantic,
            "cooccurrence": self.cooccurrence,
            "entity": self.entity,
            "media": self.media,
        }


@dataclass(frozen=True, slots=True)
class AttachAnchors:
    rare_entity: bool = False
    specific_media: bool = False
    shared_link: bool = False

    def any(self) -> bool:
        return self.rare_entity or self.specific_media or self.shared_link

    def as_dict(self) -> dict[str, bool]:
        return {
            "rare_entity": self.rare_entity,
            "specific_media": self.specific_media,
            "shared_link": self.shared_link,
        }


def cooccurrence_met(posts: int, accounts: int) -> bool:
    """At least 3 posts from more than 3 accounts carry a claim of both groups."""
    return posts >= COOCCURRENCE_MIN_POSTS and accounts >= COOCCURRENCE_MIN_ACCOUNTS


def evaluate_attachment(
    signals: AttachSignals,
    anchors: AttachAnchors,
    *,
    medoid_similarity: float,
    medoid_floor: float,
) -> bool:
    """A group joins a narrative only when ALL hold: at least 2 of the 4 signals with at
    least one non-semantic, at least one specific anchor, and a cosine to a medoid of at
    least ``medoid_floor``. When in doubt, do not attach."""
    return (
        signals.count() >= 2
        and signals.non_semantic() >= 1
        and anchors.any()
        and medoid_similarity >= medoid_floor
    )


def entity_key(name: str, qid: str | None) -> str:
    """Wikidata QID, else the name folded (case, accents, spaces)."""
    if qid:
        return qid.strip().upper()
    folded = unicodedata.normalize("NFKD", name.casefold())
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", stripped).split())


_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}»…"


def extract_links(text: str) -> set[str]:
    """Normalized links of a text: no scheme, no ``www.``, no query or fragment, no
    trailing slash."""
    out: set[str] = set()
    for match in _URL.findall(text):
        raw = match.rstrip(_TRAILING)
        body = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
        body = re.split(r"[?#]", body, maxsplit=1)[0].rstrip("/")
        host, _, path = body.partition("/")
        host = host.lower().removeprefix("www.")
        if "." not in host:
            continue
        out.add(f"{host}/{path}" if path else host)
    return out


def find_bridge(nodes: Sequence[int], edges: Iterable[tuple[int, int]]) -> bool:
    """True when removing a single node splits the rest into at least two connected
    subsets of at least two nodes each (one group bridging two otherwise disconnected
    stories). Meant for small graphs (a few hundred nodes)."""
    adjacency: dict[int, set[int]] = {n: set() for n in nodes}
    for a, b in edges:
        if a in adjacency and b in adjacency and a != b:
            adjacency[a].add(b)
            adjacency[b].add(a)
    if len(nodes) < 5:
        return False
    for removed in nodes:
        seen: set[int] = {removed}
        sizes: list[int] = []
        for start in nodes:
            if start in seen:
                continue
            stack, size = [start], 0
            seen.add(start)
            while stack:
                node = stack.pop()
                size += 1
                for nxt in adjacency[node]:
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            sizes.append(size)
        if sum(1 for s in sizes if s >= 2) >= 2:
            return True
    return False


# ---------------------------------------------------------------- sampling, novelty

_NUMBER = re.compile(r"\d[\d\s.,]*\d|\d")
_MENTION = re.compile(r"@(\w{2,30})")
_WORD = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (``q`` in 0..1); 0.0 on an empty input."""
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    low = math.floor(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def engagement_of(metrics: Mapping[str, Any] | None) -> int:
    """Popularity of a post: its impressions/views when known, else the sum of its
    interaction counters."""
    data = metrics or {}

    def num(key: str) -> int:
        value = data.get(key)
        return int(value) if isinstance(value, int | float) and value > 0 else 0

    views = max(num("impressions"), num("views"), num("impression_count"))
    if views:
        return views
    return sum(
        num(k)
        for k in (
            "likes",
            "reposts",
            "retweets",
            "replies",
            "quotes",
            "like_count",
            "retweet_count",
            "reply_count",
            "quote_count",
        )
    )


def _normalize_number(raw: str) -> str | None:
    cleaned = re.sub(r"\s", "", raw).replace(",", ".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    cleaned = cleaned.strip(".")
    digits = cleaned.replace(".", "")
    if len(digits) < 2:  # lone digits are noise
        return None
    return cleaned.lstrip("0") or "0"


def novelty_fingerprints(text: str, *, media_hashes: Iterable[str] = ()) -> list[tuple[str, str]]:
    """``(kind, fingerprint)`` pairs of a post, by code only: normalized numbers, links,
    mentions, media hashes and capitalized entities (mid-sentence). Deterministic order,
    duplicates removed."""
    found: dict[tuple[str, str], None] = {}
    for link in sorted(extract_links(text)):
        found[("link", f"link:{link}")] = None
    body = _URL.sub(" ", text)
    for raw in _NUMBER.findall(body):
        number = _normalize_number(raw)
        if number is not None:
            found[("number", f"number:{number}")] = None
    for mention in _MENTION.findall(body):
        found[("mention", f"mention:{mention.casefold()}")] = None
    for digest in sorted({h.strip().lower() for h in media_hashes if h.strip()}):
        found[("media", f"media:{digest}")] = None
    plain = _MENTION.sub(" ", body)
    sentence_start = True
    run: list[str] = []

    def flush() -> None:
        if run:
            found[("entity", f"entity:{entity_key(' '.join(run), None)}")] = None
            run.clear()

    for token in re.finditer(r"[.!?…]+|[,;:()\n]|" + _WORD.pattern, plain):
        piece = token.group(0)
        if piece[0] in ".!?…\n":
            flush()
            sentence_start = True
            continue
        if piece in ",;:()":
            flush()
            continue
        if piece[0].isupper() and len(piece) >= 3 and not sentence_start:
            run.append(piece)
        else:
            flush()
        sentence_start = False
    flush()
    return list(found)


def novelty_takes(
    posts: int,
    accounts: int,
    *,
    min_posts: int,
    min_accounts: int,
) -> bool:
    """A fingerprint "takes" with enough posts from enough distinct accounts."""
    return posts >= min_posts and accounts >= min_accounts


def sampling_rate(posts_per_minute: float, limit_per_minute: int) -> float:
    """Share of the posts to extract: 1 up to the limit, then ``limit / rate``."""
    if posts_per_minute <= limit_per_minute or posts_per_minute <= 0:
        return 1.0
    return limit_per_minute / posts_per_minute


def stratified_sample(
    items: Sequence[tuple[int, Hashable]],
    rate: float,
    rng: random.Random,
) -> tuple[list[int], list[int]]:
    """Split ``(id, stratum)`` items into ``(kept, dropped)``: each stratum (platform,
    hour) keeps ``round(n * rate)`` items, at least one, picked at random. Order of the
    input is preserved in both lists."""
    if rate >= 1.0:
        return [i for i, _ in items], []
    strata: dict[Hashable, list[int]] = defaultdict(list)
    for item_id, stratum in items:
        strata[stratum].append(item_id)
    kept: set[int] = set()
    for key in sorted(strata, key=repr):
        members = strata[key]
        take = min(len(members), max(1, round(len(members) * max(rate, 0.0))))
        kept.update(rng.sample(members, take))
    return (
        [i for i, _ in items if i in kept],
        [i for i, _ in items if i not in kept],
    )
