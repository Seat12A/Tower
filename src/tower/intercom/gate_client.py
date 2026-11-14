"""HTTP client of gate for the bot: service token + ``X-Acting-Discord-Id`` of the member
who acts. Permissions are always checked by gate. httpx is imported lazily."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    import httpx

_UNREACHABLE = "TWR-BOT-001"

__all__ = ["GateClient", "GateError", "GateUnavailable", "SseEvent"]


class GateError(Exception):
    """Error answered by gate: ``code`` is the ``TWR-*`` code of the response body."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class GateUnavailable(GateError):
    """Network error, timeout or 5xx without a body (``TWR-BOT-001``)."""


@dataclass(frozen=True, slots=True)
class SseEvent:
    id: str
    """Cursor ``<xid>-<id>``."""
    topic: str
    data: dict[str, Any] = field(default_factory=dict)
    """Public event: ``{"id", "topic", "aggregate_type", "aggregate_id", "payload", ...}``."""


def _error_from(status: int, response: httpx.Response) -> GateError:
    """Build the exception for a non-2xx response (gate error body or unavailable)."""
    try:
        err = response.json()["error"]
        code, message = str(err["code"]), str(err.get("message", ""))
        details = err.get("details")
    except (ValueError, KeyError, TypeError):
        if status >= 500:
            return GateUnavailable(status, _UNREACHABLE, f"Gate answered HTTP {status}")
        return GateError(status, _UNREACHABLE, f"Unexpected answer from gate (HTTP {status})")
    return GateError(status, code, message, details if isinstance(details, dict) else None)


class GateClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        import httpx

        self._httpx = httpx
        self._base_url = base_url
        self._timeout = timeout
        self._auth = f"Bearer {token}"
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=timeout)

    def __repr__(self) -> str:
        return f"GateClient(base_url={self._base_url!r})"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _headers(self, acting: int | None) -> dict[str, str]:
        headers = {"Authorization": self._auth}
        if acting is not None:
            headers["X-Acting-Discord-Id"] = str(acting)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        acting: int | None,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._http.request(
                method, path, params=params, json=body, headers=self._headers(acting)
            )
        except self._httpx.HTTPError as exc:
            raise GateUnavailable(
                0, _UNREACHABLE, f"Gate unreachable ({type(exc).__name__})"
            ) from None
        if response.status_code >= 400:
            raise _error_from(response.status_code, response)
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise GateUnavailable(
                response.status_code, _UNREACHABLE, "Gate answered a non-JSON body"
            ) from None

    # --- acting as a staff member (X-Acting-Discord-Id) --------------------------
    async def list_watched(
        self,
        acting: int,
        *,
        active: bool | None = True,
        platform: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if active is not None:
            params["active"] = active
        if platform is not None:
            params["platform"] = platform
        rows: list[dict[str, Any]] = await self._request(
            "GET", "/v1/watched-accounts", acting, params=params
        )
        return rows

    async def add_watched(
        self,
        acting: int,
        *,
        platform: str,
        category: str,
        handle: str | None = None,
        external_id: str | None = None,
        priority: int = 0,
        note: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"platform": platform, "category": category, "priority": priority}
        if handle is not None:
            body["handle"] = handle
        if external_id is not None:
            body["external_id"] = external_id
        if note is not None:
            body["note"] = note
        created: dict[str, Any] = await self._request(
            "POST", "/v1/watched-accounts", acting, body=body
        )
        return created

    async def update_watched(
        self, acting: int, public_id: str, *, version: int, **changes: Any
    ) -> dict[str, Any]:
        updated: dict[str, Any] = await self._request(
            "PATCH",
            f"/v1/watched-accounts/{quote(public_id, safe='')}",
            acting,
            body={"version": version, **changes},
        )
        return updated

    async def get_dossier(self, acting: int, public_id: str) -> dict[str, Any]:
        dossier: dict[str, Any] = await self._request(
            "GET", f"/v1/dossiers/{quote(public_id, safe='')}", acting
        )
        return dossier

    async def transition_dossier(
        self, acting: int, public_id: str, *, to: str, version: int, note: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"to": to, "expected_version": version}
        if note is not None:
            body["reason"] = note
        dossier: dict[str, Any] = await self._request(
            "POST", f"/v1/dossiers/{quote(public_id, safe='')}/transitions", acting, body=body
        )
        return dossier

    async def publish_dossier(
        self, acting: int, public_id: str, *, url: str, version: int
    ) -> dict[str, Any]:
        dossier: dict[str, Any] = await self._request(
            "POST",
            f"/v1/dossiers/{quote(public_id, safe='')}/publish",
            acting,
            body={"url": url, "expected_version": version},
        )
        return dossier

    async def list_incidents(self, acting: int, *, limit: int = 10) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await self._request(
            "GET", "/v1/errors", acting, params={"limit": limit}
        )
        return rows

    async def resolve_incident(
        self, acting: int, fingerprint: str, *, note: str | None = None
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = await self._request(
            "POST",
            f"/v1/errors/{quote(fingerprint, safe='')}/resolve",
            acting,
            body={"note": note},
        )
        return resolved

    async def get_usage(self, acting: int) -> dict[str, Any]:
        usage: dict[str, Any] = await self._request("GET", "/v1/llm/usage", acting)
        return usage

    # --- fact-check drafts (acting=None: the service itself, e.g. notifier) -------------
    async def request_factcheck(
        self,
        acting: int | None,
        *,
        url: str | None = None,
        platform: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        body = {
            k: v
            for k, v in {"url": url, "platform": platform, "external_id": external_id}.items()
            if v is not None
        }
        accepted: dict[str, Any] = await self._request(
            "POST", "/v1/factcheck/requests", acting, body=body
        )
        return accepted

    async def list_drafts(
        self,
        acting: int | None,
        *,
        notified: bool | None = None,
        limit: int = 25,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"status": "pending", "limit": limit}
        if notified is not None:
            params["notified"] = notified
        if before_id is not None:
            params["before_id"] = before_id
        rows: list[dict[str, Any]] = await self._request("GET", "/v1/drafts", acting, params=params)
        return rows

    async def get_draft(self, acting: int | None, draft_id: str) -> dict[str, Any]:
        draft: dict[str, Any] = await self._request(
            "GET", f"/v1/drafts/{quote(draft_id, safe='')}", acting
        )
        return draft

    async def review_draft(
        self,
        acting: int | None,
        draft_id: str,
        *,
        decision: str,
        veracity: str | None = None,
        note_text: str | None = None,
        note_lang: str | None = None,
        labels: Sequence[str] = (),
        reason: str | None = None,
        expected_version: int | None = None,
        validate_band: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"decision": decision, "labels": list(labels)}
        if validate_band:
            body["validate_band"] = True
        for key, value in (
            ("veracity", veracity),
            ("note_text", note_text),
            ("note_lang", note_lang),
            ("reason", reason),
            ("expected_version", expected_version),
        ):
            if value is not None:
                body[key] = value
        result: dict[str, Any] = await self._request(
            "POST", f"/v1/drafts/{quote(draft_id, safe='')}/review", acting, body=body
        )
        return result

    async def mark_draft_notified(self, acting: int | None, draft_id: str) -> None:
        await self._request("POST", f"/v1/drafts/{quote(draft_id, safe='')}/notified", acting)

    # --- groups, reference facts and bands ----------------------------------------------
    async def get_group(self, acting: int, group_id: str) -> dict[str, Any]:
        group: dict[str, Any] = await self._request(
            "GET", f"/v1/groups/{quote(group_id, safe='')}", acting
        )
        return group

    async def merge_groups(
        self, acting: int, target_id: str, *, source_id: str, reason: str
    ) -> dict[str, Any]:
        merged: dict[str, Any] = await self._request(
            "POST",
            f"/v1/groups/{quote(target_id, safe='')}/merge",
            acting,
            body={"source_group_id": source_id, "reason": reason},
        )
        return merged

    async def split_group(
        self, acting: int, group_id: str, *, claim_ids: Sequence[str], reason: str
    ) -> dict[str, Any]:
        new: dict[str, Any] = await self._request(
            "POST",
            f"/v1/groups/{quote(group_id, safe='')}/split",
            acting,
            body={"claim_ids": list(claim_ids), "reason": reason},
        )
        return new

    # --- reliability registry ------------------------------------------------------------
    async def get_registry_entry(self, acting: int, ref: str) -> dict[str, Any]:
        entry: dict[str, Any] = await self._request(
            "GET", f"/v1/registry/{quote(ref, safe='')}", acting
        )
        return entry

    async def update_registry_entry(
        self, acting: int, ref: str, *, version: int, reason: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        done: dict[str, Any] = await self._request(
            "PATCH",
            f"/v1/registry/{quote(ref, safe='')}",
            acting,
            body={"version": version, "reason": reason, **changes},
        )
        return done

    async def registry_history(self, acting: int, ref: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await self._request(
            "GET", f"/v1/registry/{quote(ref, safe='')}/history", acting
        )
        return rows

    async def seed_registry_control(self, acting: int, ref: str) -> dict[str, Any]:
        done: dict[str, Any] = await self._request(
            "POST", f"/v1/registry/{quote(ref, safe='')}/seed-control", acting
        )
        return done

    # --- narratives --------------------------------------------------------------------
    async def list_narratives(
        self, acting: int, *, state: str | None = None, limit: int = 15
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if state:
            params["state"] = state
        rows: list[dict[str, Any]] = await self._request(
            "GET", "/v1/narratives", acting, params=params
        )
        return rows

    async def get_narrative(self, acting: int, narrative_id: str) -> dict[str, Any]:
        narrative: dict[str, Any] = await self._request(
            "GET", f"/v1/narratives/{quote(narrative_id, safe='')}", acting
        )
        return narrative

    async def merge_narratives(
        self, acting: int, target_id: str, *, source_id: str, reason: str
    ) -> dict[str, Any]:
        merged: dict[str, Any] = await self._request(
            "POST",
            f"/v1/narratives/{quote(target_id, safe='')}/merge",
            acting,
            body={"source_id": source_id, "reason": reason},
        )
        return merged

    async def split_narrative(
        self, acting: int, narrative_id: str, *, group_ids: Sequence[str], reason: str
    ) -> dict[str, Any]:
        new: dict[str, Any] = await self._request(
            "POST",
            f"/v1/narratives/{quote(narrative_id, safe='')}/split",
            acting,
            body={"group_ids": list(group_ids), "reason": reason},
        )
        return new

    async def set_narrative_title(
        self, acting: int, narrative_id: str, *, title: str | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"title": title} if title else {}
        done: dict[str, Any] = await self._request(
            "POST", f"/v1/narratives/{quote(narrative_id, safe='')}/title", acting, body=body
        )
        return done

    async def list_facts(
        self,
        acting: int,
        *,
        status: str | None = None,
        indicator: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        if indicator is not None:
            params["indicator"] = indicator
        rows: list[dict[str, Any]] = await self._request(
            "GET", "/v1/reference-facts", acting, params=params
        )
        return rows

    async def get_fact(self, acting: int, fact_id: str) -> dict[str, Any]:
        fact: dict[str, Any] = await self._request(
            "GET", f"/v1/reference-facts/{quote(fact_id, safe='')}", acting
        )
        return fact

    async def create_fact_from_verdict(
        self, acting: int, *, verdict_id: str, **fields: Any
    ) -> dict[str, Any]:
        body = {k: v for k, v in {"verdict_id": verdict_id, **fields}.items() if v is not None}
        created: dict[str, Any] = await self._request(
            "POST", "/v1/reference-facts/from-verdict", acting, body=body
        )
        return created

    async def validate_fact(self, acting: int, fact_id: str) -> dict[str, Any]:
        fact: dict[str, Any] = await self._request(
            "POST", f"/v1/reference-facts/{quote(fact_id, safe='')}/validate", acting
        )
        return fact

    async def reject_fact(self, acting: int, fact_id: str, *, reason: str) -> dict[str, Any]:
        fact: dict[str, Any] = await self._request(
            "POST",
            f"/v1/reference-facts/{quote(fact_id, safe='')}/reject",
            acting,
            body={"reason": reason},
        )
        return fact

    async def validate_band_from_verdict(self, acting: int, verdict_id: str) -> dict[str, Any]:
        band: dict[str, Any] = await self._request(
            "POST", "/v1/verdict-bands/from-verdict", acting, body={"verdict_id": verdict_id}
        )
        return band

    # --- processing profiles and level overrides ----------------------------------------
    async def list_profiles(self, acting: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await self._request("GET", "/v1/profiles", acting)
        return rows

    async def get_active_profile(self, acting: int) -> dict[str, Any]:
        profile: dict[str, Any] = await self._request("GET", "/v1/profiles/active", acting)
        return profile

    async def create_profile(
        self, acting: int, *, changes: dict[str, Any], reason: str, activate: bool = True
    ) -> dict[str, Any]:
        created: dict[str, Any] = await self._request(
            "POST",
            "/v1/profiles",
            acting,
            body={"changes": changes, "reason": reason, "activate": activate},
        )
        return created

    async def rollback_profile(self, acting: int, *, reason: str) -> dict[str, Any]:
        profile: dict[str, Any] = await self._request(
            "POST", "/v1/profiles/rollback", acting, body={"reason": reason}
        )
        return profile

    async def create_level_override(
        self,
        acting: int,
        *,
        target_type: str,
        target: str,
        level: str,
        hours: float,
        reason: str,
    ) -> dict[str, Any]:
        created: dict[str, Any] = await self._request(
            "POST",
            "/v1/level-overrides",
            acting,
            body={
                "target_type": target_type,
                "target": target,
                "level": level,
                "hours": hours,
                "reason": reason,
            },
        )
        return created

    async def list_level_overrides(
        self, acting: int, *, active: bool = True, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await self._request(
            "GET", "/v1/level-overrides", acting, params={"active": active, "limit": limit}
        )
        return rows

    async def revoke_level_override(
        self, acting: int, override_id: str, *, reason: str
    ) -> dict[str, Any]:
        revoked: dict[str, Any] = await self._request(
            "POST",
            f"/v1/level-overrides/{quote(override_id, safe='')}/revoke",
            acting,
            body={"reason": reason},
        )
        return revoked

    # --- as the service itself --------------------------------------------------------
    async def get_cursor(self, name: str) -> str | None:
        body = await self._request("GET", f"/v1/events/cursors/{quote(name, safe='')}", None)
        cursor = body.get("cursor")
        return str(cursor) if cursor is not None else None

    async def put_cursor(self, name: str, cursor: str) -> None:
        await self._request(
            "PUT", f"/v1/events/cursors/{quote(name, safe='')}", None, body={"cursor": cursor}
        )

    async def stream_events(
        self, *, after: str | None, topics: Sequence[str]
    ) -> AsyncIterator[SseEvent]:
        """``GET /v1/events?after=…&topics=…``; yields until the connection drops (then
        raises ``GateUnavailable``)."""
        params: list[tuple[str, str | int | float | bool | None]] = [("topics", t) for t in topics]
        if after is not None:
            params.append(("after", after))
        timeout = self._httpx.Timeout(self._timeout, read=None)
        try:
            async with self._http.stream(
                "GET",
                "/v1/events",
                params=params,
                headers={**self._headers(None), "Accept": "text/event-stream"},
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _error_from(response.status_code, response)
                event_id = topic = None
                data: list[str] = []
                async for line in response.aiter_lines():
                    if line:
                        if line.startswith(":"):
                            continue
                        name, _, value = line.partition(":")
                        value = value.removeprefix(" ")
                        if name == "id":
                            event_id = value
                        elif name == "event":
                            topic = value
                        elif name == "data":
                            data.append(value)
                        continue
                    # blank line: end of frame
                    frame_id, frame_topic, frame_data = event_id, topic, data
                    event_id = topic = None
                    data = []
                    if frame_id is None or not frame_data:
                        continue
                    try:
                        payload = json.loads("\n".join(frame_data))
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    yield SseEvent(frame_id, frame_topic or str(payload.get("topic", "")), payload)
        except self._httpx.HTTPError as exc:
            raise GateUnavailable(
                0, _UNREACHABLE, f"Event stream broken ({type(exc).__name__})"
            ) from None
        raise GateUnavailable(0, _UNREACHABLE, "Event stream closed by gate")
