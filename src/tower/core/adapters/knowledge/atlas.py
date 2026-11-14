"""Atlas: source selection and collection for one investigation (pipeline.md E7).

Order (connaissances.md): existing fact-checks → structured sources chosen by the
jurisdiction of the claim (statistics: INSEE, Eurostat, World Bank, OECD, IMF; law: LEGI
mirror, then Légifrance, EUR-Lex, Fedlex, Justice Laws; plus Wikidata) → Wikipedia (then
its references to primary sources) → news (local ``news_index``, GDELT for ongoing
situations) → web search (Brave: primary and official domains of the jurisdiction first,
then broadened). The country choice comes from ``jurisdiction_sources`` (defaults below
when a country has no row). Every call goes through ``source_cache`` (or PostgreSQL for
the local mirrors); an unavailable source is captured (``TWR-ATL-*``) and skipped.

Pure orchestration: the caller (service ``investigate``) chunks, ranks and stores.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tower.core.adapters.knowledge.html_text import extract_text
from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.core.logging import get_logger
from tower.core.ports.knowledge import (
    Evidence,
    KnowledgeSource,
    SourceCache,
    SourceQuery,
    SourceUnavailable,
)
from tower.core.ports.web import FetchedPage, FetchError, SearchClient, SearchResult, WebFetcher

log = get_logger(__name__)

__all__ = [
    "Atlas",
    "AtlasDocument",
    "AtlasQuery",
    "AtlasRequest",
    "AtlasResult",
    "RegistryInfo",
    "canonical_url",
    "registrable_domain",
]


@dataclass(frozen=True, slots=True)
class RegistryInfo:
    """One ``source_registry`` row."""

    domain: str
    publisher: str
    tier: str
    kind: str
    country: str | None


@dataclass(frozen=True, slots=True)
class AtlasQuery:
    query: str
    lang: str
    source_type: str
    target: str
    """``encyclopedia``, ``fact_check`` or ``web``."""
    side: str
    """``confirm`` or ``refute`` (logged: both series must be non-empty)."""


@dataclass(frozen=True, slots=True)
class AtlasRequest:
    claim_text: str
    """Canonical claim, original language (for fact-check search)."""
    claim_lang: str
    claim_type: str
    jurisdictions: tuple[str, ...]
    queries: tuple[AtlasQuery, ...]
    entity_lookups: tuple[str, ...]
    max_web_searches: int
    """4 at the standard depth, +4 at the reinforced depth (round 2)."""
    as_of: date | None = None
    max_documents: int = 24
    max_reference_fetches: int = 6
    """Wikipedia references followed to primary sources, at most."""
    fact_checks_only: bool = False
    """Query only the existing fact-checks (E3 lookup for a new group): no structured
    source, no encyclopedia, no web search."""
    skip_encyclopedia: bool = False
    """Skip the encyclopedia (and its followed references): light check (E5) only reads
    existing fact-checks and structured sources."""


@dataclass(frozen=True, slots=True)
class AtlasDocument:
    adapter: str
    canonical_url: str
    original_url: str
    domain: str
    tier: str
    lang: str
    text: str
    """Plain text (HTML already stripped); never kept beyond the investigation unless cited."""
    title: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    stable_id: str | None = None
    sha256: str | None = None
    license: str | None = None
    side: str | None = None
    """Query side that found it (``confirm``, ``refute``) or ``None`` (structured lookup)."""


@dataclass(slots=True)
class AtlasResult:
    documents: list[AtlasDocument] = field(default_factory=list)
    web_searches: int = 0
    paid_cost_usd_micros: int = 0
    adapters_used: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    """Adapter → ``TWR-ATL-*`` code of the failure."""


_TWO_LEVEL_SUFFIXES = frozenset(
    {
        "gouv.fr",
        "gov.uk",
        "co.uk",
        "org.uk",
        "ac.uk",
        "gob.es",
        "gov.au",
        "com.au",
        "gov.br",
        "com.br",
        "co.jp",
        "co.za",
        "gc.ca",
        "gouv.qc.ca",
        "fgov.be",
        "admin.ch",
    }
)
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src"})
_JURISDICTION_LANGS: dict[str, tuple[str, ...]] = {
    "FR": ("fr",),
    "BE": ("fr", "nl"),
    "CH": ("fr", "de"),
    "LU": ("fr", "de"),
    "CA": ("fr", "en"),
    "DE": ("de",),
    "AT": ("de",),
    "ES": ("es",),
    "IT": ("it",),
    "PT": ("pt",),
    "NL": ("nl",),
}
STAT_ADAPTERS_INTERNATIONAL = ("eurostat", "worldbank", "oecd", "imf")
"""Free statistics adapters always tried for a statistic (they answer only for a
configured indicator of the claim's territory, so a miss costs no HTTP call)."""
DEFAULT_COUNTRY_ADAPTERS: dict[tuple[str, str], tuple[str, ...]] = {
    ("FR", "statistics"): ("insee",),
    ("EU", "statistics"): ("eurostat",),
    ("FR", "law"): ("legi_mirror", "legifrance"),
    ("EU", "law"): ("eurlex",),
    ("CH", "law"): ("fedlex",),
    ("CA", "law"): ("canada_laws",),
}
"""Used when ``jurisdiction_sources`` has no adapter row for (country, type)."""
LEGAL_FALLBACKS = frozenset({"legifrance"})
"""Paid-quota or rate-limited legal API asked only when the free mirror found nothing."""
_TEXT_TYPES = frozenset({"text/plain", "application/json", "application/xml", "text/xml"})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_FETCH_PER_SEARCH = 2
_MIN_RESTRICTED_RESULTS = 2
_SEARCH_LIMIT = 5


def _host(host_or_url: str) -> str:
    value = host_or_url.strip()
    parts = urlsplit(value if "//" in value else f"//{value}")
    try:
        host = parts.hostname or ""
    except ValueError:
        host = ""
    host = host.lower().rstrip(".")
    return host.removeprefix("www.")


def registrable_domain(host_or_url: str) -> str:
    """``https://www.insee.fr/fr/x`` → ``insee.fr``; keeps known two-level public suffixes
    (``gouv.fr``, ``gov.uk``, ``co.uk``…: ``legifrance.gouv.fr`` stays whole). Lowercase,
    no port, no ``www.``. The registry lookup itself is a suffix match (:meth:`Atlas.tier_of`).
    """
    host = _host(host_or_url)
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def canonical_url(url: str, *, keep_fragment: bool = False) -> str:
    """Dedupe form of a URL: lowercase scheme and host, no ``www.``, no default port, no
    tracking parameters (``utm_*``, ``fbclid``…), no fragment (unless asked)."""
    parts = urlsplit(url.strip())
    try:
        host = (parts.hostname or "").lower().removeprefix("www.")
        port = parts.port
    except ValueError:
        return url.strip()
    scheme = parts.scheme.lower() or "https"
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
        ]
    )
    path = parts.path or "/"
    return urlunsplit((scheme, host, path, query, parts.fragment if keep_fragment else ""))


def jurisdiction_languages(jurisdictions: Sequence[str]) -> list[str]:
    """Wikipedia language editions of the jurisdictions (default ``en``)."""
    langs: list[str] = []
    for code in jurisdictions:
        for lang in _JURISDICTION_LANGS.get(code.upper(), ("en",)):
            if lang not in langs:
                langs.append(lang)
    return langs or ["en"]


def _iso_country(jurisdictions: Sequence[str]) -> str | None:
    for code in jurisdictions:
        if re.fullmatch(r"[A-Za-z]{2}", code) and code.upper() not in ("EU", "INT"):
            return code.upper()
    return None


class _Run:
    """Mutable state of one ``collect`` call."""

    def __init__(self, request: AtlasRequest) -> None:
        self.request = request
        self.result = AtlasResult()
        self.seen: set[str] = set()
        self.confirm = 0
        self.refute = 0
        self.fetch_failures = 0

    @property
    def full(self) -> bool:
        return len(self.result.documents) >= self.request.max_documents

    def count(self, query: AtlasQuery) -> None:
        if query.side == "confirm":
            self.confirm += 1
        elif query.side == "refute":
            self.refute += 1


class Atlas:
    """Collects documents for one request. Stateless apart from injected clients."""

    def __init__(
        self,
        *,
        sources: Mapping[str, KnowledgeSource],
        search: SearchClient | None,
        fetcher: WebFetcher | None,
        cache: SourceCache | None,
        registry: Mapping[str, RegistryInfo],
        search_cost_usd_micros: int = 5000,
        country_adapters: Mapping[tuple[str, str], Sequence[str]] | None = None,
        official_domains: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        """``sources`` by name: ``factcheck_tools``, ``wikidata``, ``wikipedia``,
        ``insee``, ``legifrance``, ``legi_mirror``, ``eurostat``, ``worldbank``, ``oecd``,
        ``imf``, ``eurlex``, ``fedlex``, ``canada_laws``, ``news_index``, ``gdelt``
        (absent = not configured).

        ``country_adapters``: ``(country, source_type)`` → adapter names, from
        ``jurisdiction_sources`` (``DEFAULT_COUNTRY_ADAPTERS`` fills the gaps).
        ``official_domains``: country → official domains (``jurisdiction_sources``), used
        with the registry's primary domains to restrict the web search.

        ``cache`` is kept for callers that share one cache with the adapters; fetched
        pages are deliberately never cached (full texts are not kept, see
        connaissances.md, "Droit d'auteur et conservation").
        """
        self._sources = dict(sources)
        self._search = search
        self._fetcher = fetcher
        self._cache = cache
        self._registry = {k.lower(): v for k, v in registry.items()}
        self._search_cost = search_cost_usd_micros
        self._country_adapters = {
            (c.upper(), t): tuple(names) for (c, t), names in (country_adapters or {}).items()
        }
        self._official = {c.upper(): tuple(d) for c, d in (official_domains or {}).items()}

    # --- registry --------------------------------------------------------------------

    def tier_of(self, url_or_domain: str) -> tuple[str, str | None]:
        """``(tier, publisher)`` from the registry (suffix match on the host labels, the
        most specific entry wins); ``("tertiary", None)`` when unknown."""
        labels = _host(url_or_domain).split(".")
        for i in range(len(labels)):
            info = self._registry.get(".".join(labels[i:]))
            if info is not None:
                return info.tier, info.publisher
        return "tertiary", None

    def primary_domains(self, jurisdictions: Sequence[str]) -> list[str]:
        """Primary registry domains of the jurisdictions (plus ``EU`` and ``INT``), the
        jurisdictions first. A domain already covered by a listed suffix is dropped
        (``legifrance.gouv.fr`` under ``gouv.fr``)."""
        order = [j.upper() for j in jurisdictions]
        for extra in ("EU", "INT"):
            if extra not in order:
                order.append(extra)
        picked: list[str] = []
        for country in order:
            group = sorted(
                (
                    info
                    for info in self._registry.values()
                    if info.tier == "primary" and (info.country or "").upper() == country
                ),
                key=lambda info: (info.domain.count("."), info.domain),
            )
            domains = [info.domain.lower() for info in group]
            domains += [d.lower() for d in self._official.get(country, ())]
            for domain in domains:
                if domain in picked or any(domain.endswith("." + p) for p in picked):
                    continue
                picked.append(domain)
        return picked

    def adapters_for(self, country: str, source_type: str) -> tuple[str, ...]:
        """Adapters of ``jurisdiction_sources`` for (country, type), else the defaults."""
        key = (country.upper(), source_type)
        return self._country_adapters.get(key) or DEFAULT_COUNTRY_ADAPTERS.get(key, ())

    # --- collection ------------------------------------------------------------------

    async def collect(self, request: AtlasRequest) -> AtlasResult:
        run = _Run(request)
        await self._fact_checks(run)
        if not request.fact_checks_only:
            await self._structured(run)
            if not request.skip_encyclopedia:
                await self._encyclopedia(run)
                await self._news(run)
            await self._web(run)
        run.result.paid_cost_usd_micros = run.result.web_searches * self._search_cost
        log.info(
            "atlas.collected",
            documents=len(run.result.documents),
            web_searches=run.result.web_searches,
            confirm=run.confirm,
            refute=run.refute,
            fetch_failures=run.fetch_failures,
            adapters=run.result.adapters_used,
            skipped=sorted(run.result.skipped),
        )
        return run.result

    def _fail(self, run: _Run, name: str, exc: SourceUnavailable) -> None:
        capture(exc.code, exc, source=name)
        run.result.skipped[name] = exc.code.code

    @staticmethod
    def _used(run: _Run, name: str) -> None:
        if name not in run.result.adapters_used:
            run.result.adapters_used.append(name)

    def _not_configured(self, run: _Run, name: str) -> None:
        run.result.skipped.setdefault(name, ec.ATL_NOT_CONFIGURED.code)

    def _query(
        self,
        run: _Run,
        text: str,
        lang: str,
        *,
        limit: int = 5,
        jurisdiction: str | None = None,
    ) -> SourceQuery:
        req = run.request
        return SourceQuery(
            text=text,
            lang=lang,
            claim_type=req.claim_type,
            jurisdiction=jurisdiction or (req.jurisdictions[0] if req.jurisdictions else None),
            limit=limit,
            as_of=datetime.combine(req.as_of, time.min, tzinfo=UTC) if req.as_of else None,
        )

    def _evidence_document(self, evidence: Evidence, side: str | None = None) -> AtlasDocument:
        tier, publisher = self.tier_of(evidence.url)
        return AtlasDocument(
            adapter=evidence.source,
            canonical_url=canonical_url(evidence.url, keep_fragment=True),
            original_url=evidence.url,
            domain=registrable_domain(evidence.url),
            tier=(evidence.tier.value if evidence.tier is not None else tier),
            lang=evidence.lang,
            text=evidence.text,
            title=evidence.title,
            publisher=evidence.publisher or publisher,
            published_at=evidence.published_at,
            stable_id=evidence.stable_id,
            sha256=evidence.sha256,
            license=evidence.license,
            side=side,
        )

    def _add(self, run: _Run, doc: AtlasDocument) -> bool:
        key = (
            doc.canonical_url
            if doc.adapter != "wikidata"
            else f"{doc.canonical_url}|{doc.stable_id}"
        )
        if run.full or key in run.seen:
            return False
        run.seen.add(key)
        run.result.documents.append(doc)
        return True

    async def _lookup(
        self, run: _Run, name: str, queries: Sequence[tuple[SourceQuery, str | None]]
    ) -> list[Evidence]:
        """Run ``queries`` on one adapter; the first failure skips the rest of that adapter."""
        source = self._sources.get(name)
        if source is None:
            self._not_configured(run, name)
            return []
        found: list[Evidence] = []
        ok = True
        for query, side in queries:
            if run.full:
                break
            try:
                evidences = await source.lookup(query)
            except SourceUnavailable as exc:
                self._fail(run, name, exc)
                ok = False
                break
            for evidence in evidences:
                if self._add(run, self._evidence_document(evidence, side)):
                    found.append(evidence)
        if ok:
            self._used(run, name)
        return found

    async def _fact_checks(self, run: _Run) -> None:
        req = run.request
        queries: list[tuple[SourceQuery, str | None]] = [
            (self._query(run, req.claim_text, req.claim_lang), None)
        ]
        for q in req.queries:
            if q.target == "fact_check":
                run.count(q)
                queries.append((self._query(run, q.query, q.lang), q.side))
        await self._lookup(run, "factcheck_tools", queries)

    async def _structured(self, run: _Run) -> None:
        req = run.request
        if req.entity_lookups:
            await self._lookup(
                run,
                "wikidata",
                [(self._query(run, e, req.claim_lang, limit=1), None) for e in req.entity_lookups],
            )
        countries = [j.upper() for j in req.jurisdictions] or ["INT"]
        if req.claim_type == "statistic":
            for country in countries:
                names = [*self.adapters_for(country, "statistics"), *STAT_ADAPTERS_INTERNATIONAL]
                for name in dict.fromkeys(names):
                    if name not in self._sources:
                        continue  # international adapters are optional
                    lang = "fr" if name == "insee" else req.claim_lang
                    await self._lookup(
                        run,
                        name,
                        [(self._query(run, req.claim_text, lang, jurisdiction=country), None)],
                    )
        if req.claim_type in ("legal_text", "procedure"):
            for country in countries:
                found_free = False
                for name in self.adapters_for(country, "law"):
                    if name in LEGAL_FALLBACKS and found_free:
                        continue
                    lang = "fr" if country == "FR" else req.claim_lang
                    hits = await self._lookup(
                        run,
                        name,
                        [(self._query(run, req.claim_text, lang, jurisdiction=country), None)],
                    )
                    found_free = found_free or bool(hits)

    async def _encyclopedia(self, run: _Run) -> None:
        req = run.request
        queries = [q for q in req.queries if q.target == "encyclopedia"]
        if not queries:
            return
        default_lang = jurisdiction_languages(req.jurisdictions)[0]
        wiki: list[Evidence] = []
        for q in queries:
            run.count(q)
            found = await self._lookup(
                run,
                "wikipedia",
                [(self._query(run, q.query, q.lang or default_lang, limit=1), q.side)],
            )
            wiki.extend(found)
            if "wikipedia" in run.result.skipped:
                return
        await self._follow_references(run, wiki)

    async def _news(self, run: _Run) -> None:
        """Level 5: recent reference headlines (local index, one evidence per wire story),
        and GDELT for an ongoing situation (world coverage, rate-limited)."""
        req = run.request
        if "news_index" in self._sources:
            await self._lookup(
                run, "news_index", [(self._query(run, req.claim_text, req.claim_lang), None)]
            )
        if req.claim_type == "ongoing_situation" and "gdelt" in self._sources:
            await self._lookup(
                run, "gdelt", [(self._query(run, req.claim_text, req.claim_lang), None)]
            )

    async def _follow_references(self, run: _Run, articles: Sequence[Evidence]) -> None:
        if self._fetcher is None or run.request.max_reference_fetches <= 0:
            return
        ranked: list[tuple[int, str, str, str]] = []
        seen_urls: set[str] = set()
        for article in articles:
            for url in article.references:
                key = canonical_url(url)
                tier, publisher = self.tier_of(url)
                if key in seen_urls or tier not in ("primary", "secondary"):
                    continue
                seen_urls.add(key)
                ranked.append((0 if tier == "primary" else 1, url, tier, publisher or ""))
        ranked.sort(key=lambda r: r[0])  # stable: primary first, article order kept
        for _rank, url, _tier, _publisher in ranked[: run.request.max_reference_fetches]:
            if run.full:
                break
            doc = await self._fetch_document(
                run, url, adapter="wikipedia_reference", lang=None, side=None
            )
            if doc is not None:
                self._add(run, doc)

    async def _fetch_document(
        self,
        run: _Run,
        url: str,
        *,
        adapter: str,
        lang: str | None,
        side: str | None,
        fallback: SearchResult | None = None,
    ) -> AtlasDocument | None:
        if self._fetcher is None:
            return None
        try:
            page = await self._fetcher.fetch(url)
        except FetchError as exc:
            run.fetch_failures += 1
            capture(exc.code, exc, domain=registrable_domain(url))
            return None
        text, title, published = self._page_text(page)
        if not text:
            return None
        final = page.final_url or url
        tier, publisher = self.tier_of(final)
        return AtlasDocument(
            adapter=adapter,
            canonical_url=canonical_url(final),
            original_url=url,
            domain=registrable_domain(final),
            tier=tier,
            lang=lang or run.request.claim_lang,
            text=text,
            title=title or (fallback.title if fallback else None),
            publisher=publisher,
            published_at=published or (fallback.published_at if fallback else None),
            sha256=page.sha256,
            side=side,
        )

    @staticmethod
    def _page_text(page: FetchedPage) -> tuple[str, str | None, datetime | None]:
        """HTML → visible text; plain text types as-is; PDF and others → nothing."""
        ctype = page.content_type.lower()
        if ctype in _HTML_TYPES:
            extracted = extract_text(page.text)
            return extracted.text, extracted.title, extracted.published_at
        if ctype in _TEXT_TYPES:
            return page.text.strip(), None, None
        return "", None, None

    async def _web(self, run: _Run) -> None:
        req = run.request
        queries = [q for q in req.queries if q.target == "web"]
        if not queries:
            return
        if self._search is None:
            self._not_configured(run, "brave")
            return
        if req.max_web_searches <= 0:
            run.result.skipped["brave"] = ec.ATL_SEARCH_BUDGET.code
            return
        confirms = [q for q in queries if q.side == "confirm"]
        refutes = [q for q in queries if q.side == "refute"]
        others = [q for q in queries if q.side not in ("confirm", "refute")]
        ordered: list[AtlasQuery] = []
        for i in range(max(len(confirms), len(refutes))):
            ordered.extend(group[i] for group in (confirms, refutes) if i < len(group))
        ordered.extend(others)
        primary = self.primary_domains(req.jurisdictions)
        country = _iso_country(req.jurisdictions)
        budget = req.max_web_searches
        for index, q in enumerate(ordered):
            if budget <= 0 or run.full:
                break
            remaining_after = len(ordered) - index - 1
            try:
                restricted = bool(primary)
                results = await self._search.search(
                    q.query,
                    limit=_SEARCH_LIMIT,
                    lang=q.lang,
                    country=country,
                    include_domains=primary,
                )
                budget -= 1
                run.result.web_searches += 1
                searched = [results]
                if (
                    restricted
                    and len(results) < _MIN_RESTRICTED_RESULTS
                    and budget - 1 >= remaining_after
                ):
                    broadened = await self._search.search(
                        q.query, limit=_SEARCH_LIMIT, lang=q.lang, country=country
                    )
                    budget -= 1
                    run.result.web_searches += 1
                    searched.append(broadened)
            except SourceUnavailable as exc:
                self._fail(run, "brave", exc)
                return
            run.count(q)
            self._used(run, "brave")
            merged: list[SearchResult] = []
            urls: set[str] = set()
            for batch in searched:
                for item in batch:
                    key = canonical_url(item.url)
                    if key not in urls:
                        urls.add(key)
                        merged.append(item)
            fetched = 0
            for item in merged:
                if fetched >= _FETCH_PER_SEARCH or run.full:
                    break
                if canonical_url(item.url) in run.seen:
                    continue
                doc = await self._fetch_document(
                    run,
                    item.url,
                    adapter="brave",
                    lang=item.lang or q.lang,
                    side=q.side,
                    fallback=item,
                )
                fetched += 1
                if doc is not None:
                    self._add(run, doc)
