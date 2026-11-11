"""``/dossier`` commands. Publication on X stays manual: ``publish`` only records the URL."""

from __future__ import annotations

from typing import Any

from tower.intercom.gate_client import GateClient, GateError
from tower.intercom.handlers import Reply, guarded, ok, short
from tower.intercom.i18n import has, t

__all__ = ["publish", "reject", "show", "take", "validate"]


def _status(locale: str, status: object) -> str:
    key = f"status.{status}"
    return t(locale, key) if has(locale, key) else str(status)


def _version(dossier: dict[str, Any]) -> int:
    version = dossier.get("version")
    if version is None:  # public view: the member lacks dossiers:read
        raise GateError(403, "TWR-AUTH-005", "Missing permission")
    return int(version)


@guarded
async def show(gate: GateClient, acting: int, locale: str, public_id: str) -> Reply:
    d = await gate.get_dossier(acting, public_id)
    dash = "-"
    return ok(
        t(
            locale,
            "dossier.show",
            public_id=d["public_id"],
            status=_status(locale, d.get("status", dash)),
            kind=d.get("kind") or dash,
            priority=d.get("priority", dash),
            title=d.get("title") or dash,
            x_post_id=d.get("x_post_id") or dash,
            assigned_to=d.get("assigned_to") or dash,
            published_url=d.get("published_url") or dash,
            version=d.get("version", dash),
        )
    )


async def _transition(
    gate: GateClient, acting: int, locale: str, public_id: str, to: str, key: str, note: str | None
) -> Reply:
    current = await gate.get_dossier(acting, public_id)
    updated = await gate.transition_dossier(
        acting, public_id, to=to, version=_version(current), note=note
    )
    return ok(
        t(
            locale,
            key,
            short_id=short(updated["public_id"]),
            from_status=_status(locale, current.get("status")),
            to_status=_status(locale, updated.get("status")),
        )
    )


@guarded
async def take(gate: GateClient, acting: int, locale: str, public_id: str) -> Reply:
    return await _transition(gate, acting, locale, public_id, "in_review", "dossier.taken", None)


@guarded
async def validate(
    gate: GateClient, acting: int, locale: str, public_id: str, note: str | None = None
) -> Reply:
    return await _transition(
        gate, acting, locale, public_id, "validated", "dossier.validated", note
    )


@guarded
async def reject(
    gate: GateClient, acting: int, locale: str, public_id: str, note: str | None = None
) -> Reply:
    return await _transition(gate, acting, locale, public_id, "rejected", "dossier.rejected", note)


@guarded
async def publish(gate: GateClient, acting: int, locale: str, public_id: str, url: str) -> Reply:
    current = await gate.get_dossier(acting, public_id)
    updated = await gate.publish_dossier(
        acting, public_id, url=url.strip(), version=_version(current)
    )
    return ok(
        t(
            locale,
            "dossier.published",
            short_id=short(updated["public_id"]),
            url=updated.get("published_url") or url,
        )
    )
