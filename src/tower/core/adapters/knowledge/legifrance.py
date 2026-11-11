"""Légifrance through PISTE (``legifrance``, primary, Licence Ouverte 2.0).

OAuth2 client-credentials token kept in memory until expiry, a ``/search`` call to find
article ids, then ``/consult/getArticle`` for the text with its validity dates.

Verified (2026-11-11), partially (PISTE needs an account; no public OpenAPI file reachable):
- Token: ``POST https://oauth.piste.gouv.fr/api/oauth/token`` (sandbox:
  ``https://sandbox-oauth.piste.gouv.fr/api/oauth/token``), form body
  ``grant_type=client_credentials&client_id=&client_secret=&scope=openid``; the sandbox
  endpoint answered HTTP 401 to an anonymous call (live-confirmed it exists). Token life
  about 3600 s (``expires_in`` read from the response).
- API base: ``https://api.piste.gouv.fr/dila/legifrance/lf-engine-app`` (sandbox:
  ``https://sandbox-api.piste.gouv.fr/dila/legifrance/lf-engine-app``), header
  ``Authorization: Bearer <token>``; the API must be added to the PISTE application and its
  terms accepted (otherwise HTTP 403 → ``ATL_AUTH_FAILED``).
- ``POST /consult/getArticle`` body ``{"id": "LEGIARTI…"}`` and the fonds ``CODE_DATE`` /
  ``LODA_DATE`` (search at a date), article fields ``dateDebut`` / ``dateFin`` (epoch ms):
  confirmed by third-party client documentation only.
- NOT confirmed against official documentation: the exact ``/search`` request body
  (``recherche.champs[].criteres[]``, ``typePagination: ARTICLE``), the ``DATE_VERSION`` /
  ``singleDate`` filter (used for ``as_of``), the response field names (``results[].titles[]``
  with ``id``, ``texte`` / ``texteHtml``, ``num``, ``etat``). Parsing is tolerant, and the
  evidence carries ``extra["as_of_match"]`` computed from ``dateDebut``/``dateFin`` so a
  wrong date filter is visible. Quotas: see the PISTE console (not confirmed).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from tower.core.adapters.knowledge.html_text import extract_text
from tower.core.adapters.knowledge.http import SourceHttp, cached
from tower.core.domain import error_codes as ec
from tower.core.ports.knowledge import (
    Evidence,
    SourceCache,
    SourceCapabilities,
    SourceQuery,
    SourceUnavailable,
    Tier,
)

__all__ = ["LegifranceSource"]

NAME = "legifrance"
LICENSE = "Licence Ouverte 2.0"
PROD = ("https://oauth.piste.gouv.fr/api/oauth/token", "https://api.piste.gouv.fr")
SANDBOX = (
    "https://sandbox-oauth.piste.gouv.fr/api/oauth/token",
    "https://sandbox-api.piste.gouv.fr",
)
APP_PATH = "/dila/legifrance/lf-engine-app"
SEARCH_TTL_S = 86_400
_ARTICLE_ID = re.compile(r"[A-Z]{4}ARTI[0-9]{12}")
_FONDS = (("CODE_DATE", "codes"), ("LODA_DATE", "loda"))
_OPEN_ENDED_MS = 32_000_000_000_000  # dateFin 2999-01-01 means "no end"
_TOKEN_MARGIN_S = 60.0


def _ms_to_dt(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


class LegifranceSource:
    name = NAME
    capabilities = SourceCapabilities(
        claim_types=frozenset({"legal_text", "procedure"}), jurisdictions=frozenset({"FR"})
    )
    reliability_tier = Tier.PRIMARY
    cost_per_call_usd_micros = 0
    license = LICENSE

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        user_agent: str,
        timeout: float,
        sandbox: bool = False,
        cache: SourceCache | None = None,
        cache_ttl_s: int = 7 * 86_400,
        transport: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url, host = SANDBOX if sandbox else PROD
        self._api = host + APP_PATH
        self._http = SourceHttp(
            NAME, timeout=timeout, user_agent=user_agent, transport=transport, sleep=sleep
        )
        self._cache = cache
        self._ttl = cache_ttl_s
        self._monotonic = monotonic
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock: asyncio.Lock | None = None

    def __repr__(self) -> str:
        return "LegifranceSource()"

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- token -----------------------------------------------------------------------

    async def _bearer(self, *, force: bool = False) -> str:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not force and self._token and self._monotonic() < self._token_expires:
                return self._token
            body = await self._http.json_dict(
                "POST",
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": "openid",
                },
            )
            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                raise self._http.fail("legifrance: token response", ec.ATL_BAD_RESPONSE)
            try:
                lifetime = float(body.get("expires_in", 3600))
            except (TypeError, ValueError):
                lifetime = 3600.0
            self._token = token
            self._token_expires = self._monotonic() + max(lifetime - _TOKEN_MARGIN_S, 0.0)
            return token

    async def _api_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in (1, 2):
            token = await self._bearer(force=attempt == 2)
            try:
                return await self._http.json_dict(
                    "POST",
                    self._api + path,
                    json_body=payload,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except SourceUnavailable as exc:
                if exc.code is ec.ATL_AUTH_FAILED and attempt == 1:
                    continue  # expired or revoked token: one refresh
                raise
        raise self._http.fail(
            "legifrance: unreachable", ec.ATL_SOURCE_UNAVAILABLE
        )  # pragma: no cover

    # --- lookup ----------------------------------------------------------------------

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        limit = max(1, min(query.limit, 3))
        at = query.as_of or datetime.now(UTC)
        at_ms = int(at.replace(tzinfo=at.tzinfo or UTC).timestamp() * 1000)
        found: list[tuple[str, str]] = []
        for fond, area in _FONDS:
            for article_id in await self._search(query.text, fond, at_ms, limit):
                if all(article_id != known for known, _ in found):
                    found.append((article_id, area))
            if len(found) >= limit:
                break
        out: list[Evidence] = []
        for article_id, area in found[:limit]:
            evidence = await self._article(article_id, area, query)
            if evidence is not None:
                out.append(evidence)
        return out

    async def _search(self, text: str, fond: str, at_ms: int, limit: int) -> list[str]:
        day_ms = at_ms - at_ms % 86_400_000
        request = {"op": "search", "q": text, "fond": fond, "date": day_ms, "limit": limit}

        async def fetch() -> dict[str, Any]:
            payload = {
                "fond": fond,
                "recherche": {
                    "champs": [
                        {
                            "typeChamp": "ALL",
                            "criteres": [
                                {
                                    "typeRecherche": "TOUS_LES_MOTS_DANS_UN_CHAMP",
                                    "valeur": text,
                                    "operateur": "ET",
                                }
                            ],
                            "operateur": "ET",
                        }
                    ],
                    "filtres": [{"facette": "DATE_VERSION", "singleDate": day_ms}],
                    "pageNumber": 1,
                    "pageSize": limit,
                    "operateur": "ET",
                    "sort": "PERTINENCE",
                    "typePagination": "ARTICLE",
                },
            }
            body = await self._api_post("/search", payload)
            results = body.get("results", [])
            if not isinstance(results, list):
                raise self._http.fail("legifrance: search results", ec.ATL_BAD_RESPONSE)
            ids: list[str] = []
            for result in results:
                if not isinstance(result, dict):
                    continue
                for title in result.get("titles") or []:
                    if isinstance(title, dict) and _ARTICLE_ID.fullmatch(str(title.get("id", ""))):
                        ids.append(str(title["id"]))
                        break
            return {"ids": ids}

        result = await cached(self._cache, NAME, request, SEARCH_TTL_S, fetch)
        return [i for i in result.get("ids", []) if isinstance(i, str)]

    async def _article(self, article_id: str, area: str, query: SourceQuery) -> Evidence | None:
        request = {"op": "article", "id": article_id}

        async def fetch() -> dict[str, Any]:
            body = await self._api_post("/consult/getArticle", {"id": article_id})
            article = body.get("article")
            if not isinstance(article, dict):
                raise self._http.fail("legifrance: article response", ec.ATL_BAD_RESPONSE)
            text = article.get("texte")
            if not isinstance(text, str) or not text.strip():
                html = article.get("texteHtml")
                text = extract_text(html).text if isinstance(html, str) else ""
            else:
                text = extract_text(text).text or text
            context = article.get("context")
            code = ""
            if isinstance(context, dict):
                titles = context.get("titreTxt")
                if isinstance(titles, list) and titles and isinstance(titles[0], dict):
                    code = str(titles[0].get("titre") or "")
            return {
                "text": text,
                "num": str(article.get("num") or ""),
                "code": code,
                "etat": str(article.get("etat") or ""),
                "start": article.get("dateDebut"),
                "end": article.get("dateFin"),
            }

        data = await cached(self._cache, NAME, request, self._ttl, fetch)
        text = str(data.get("text") or "").strip()
        if not text:
            return None
        start = _ms_to_dt(data.get("start"))
        end_raw = data.get("end")
        end = (
            _ms_to_dt(end_raw)
            if isinstance(end_raw, (int, float)) and end_raw < _OPEN_ENDED_MS
            else None
        )
        label = " — ".join(
            p for p in (data.get("code"), f"Article {data['num']}" if data.get("num") else "") if p
        )
        first = start.date() if start else "unknown"
        last = end.date() if end else "open end"
        window = f"in force from {first} to {last}"
        full = f"{label or article_id} ({window}): {text}"
        extra = {
            "state": str(data.get("etat") or ""),
            "valid_from": start.date().isoformat() if start else "",
        }
        if end is not None:
            extra["valid_to"] = end.date().isoformat()
        if query.as_of is not None:
            when = query.as_of if query.as_of.tzinfo else query.as_of.replace(tzinfo=UTC)
            ok = (start is None or start <= when) and (end is None or when < end)
            extra["as_of_match"] = "true" if ok else "false"
        return Evidence(
            source=NAME,
            stable_id=article_id,
            url=f"https://www.legifrance.gouv.fr/{area}/article_lc/{article_id}",
            text=full,
            lang="fr",
            license=LICENSE,
            sha256=hashlib.sha256(full.encode()).hexdigest(),
            published_at=start,
            version=article_id,
            extra=extra,
            title=label or None,
            publisher="Légifrance (DILA)",
            tier=Tier.PRIMARY,
        )
