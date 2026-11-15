"""``/dossier`` cards. Publication on X stays manual: ``publish`` only records the URL.

The dossier card carries persistent action buttons (take / validate / reject / publish);
each click re-fetches the dossier from gate and refreshes the card in place.
"""

from __future__ import annotations

from typing import Any

from tower.intercom.gate_client import GateClient, GateError
from tower.intercom.handlers import Accent, Button, Reply, card, code, guarded, short
from tower.intercom.i18n import has, t
from tower.intercom.ui import emojis

__all__ = ["cid", "publish", "reject", "show", "take", "validate"]

_ACCENTS: dict[str, Accent] = {"published": "success", "rejected": "neutral"}


def cid(action: str, public_id: str) -> str:
    """Custom id of a dossier button: ``t12a:dos:<action>:<public id>``."""
    return f"t12a:dos:{action}:{public_id}"


def _status(locale: str, status: object) -> str:
    key = f"status.{status}"
    return t(locale, key) if has(locale, key) else str(status)


def _version(dossier: dict[str, Any]) -> int:
    version = dossier.get("version")
    if version is None:  # public view: the member lacks dossiers:read
        raise GateError(403, "TWR-AUTH-005", "Missing permission")
    return int(version)


def _card(locale: str, d: dict[str, Any], footer: str = "") -> Reply:
    status = str(d.get("status", "-"))
    pid = str(d["public_id"])
    dash = "-"
    details = [
        (t(locale, "ops.d.status"), code(_status(locale, status))),
        (t(locale, "ops.d.kind"), code(d.get("kind") or dash)),
        (t(locale, "ops.d.priority"), code(d.get("priority", dash))),
        (t(locale, "ops.d.post"), code(d.get("x_post_id") or dash)),
        (t(locale, "ops.d.assigned"), code(d.get("assigned_to") or dash)),
    ]
    if d.get("published_url"):
        details.append((t(locale, "ops.d.published"), str(d["published_url"])))
    buttons = [
        Button(
            t(locale, "dossier.btn.take"),
            cid("take", pid),
            emojis.CLAIM,
            "primary",
            disabled=status != "received",
        ),
        Button(
            t(locale, "dossier.btn.validate"),
            cid("validate", pid),
            emojis.DONE,
            "success",
            disabled=status != "in_review",
        ),
        Button(
            t(locale, "dossier.btn.reject"),
            cid("reject", pid),
            emojis.UNDONE,
            "danger",
            disabled=status not in ("received", "in_review"),
        ),
        Button(
            t(locale, "dossier.btn.publish"),
            cid("publish", pid),
            emojis.LINK,
            disabled=status != "validated",
        ),
    ]
    if d.get("x_post_id"):
        buttons.append(
            Button(
                t(locale, "dossier.btn.open_post"),
                style="link",
                url=f"https://x.com/i/status/{d['x_post_id']}",
            )
        )
    return card(
        t(locale, "dossier.title", short_id=short(pid)),
        d.get("title") or "",
        icon=emojis.FOLDER,
        accent=_ACCENTS.get(status, "primary"),
        details=tuple(details),
        footer=footer,
        buttons=tuple(buttons),
    )


@guarded
async def show(gate: GateClient, acting: int, locale: str, public_id: str) -> Reply:
    return _card(locale, await gate.get_dossier(acting, public_id))


async def _transition(
    gate: GateClient, acting: int, locale: str, public_id: str, to: str, key: str, note: str | None
) -> Reply:
    current = await gate.get_dossier(acting, public_id)
    updated = await gate.transition_dossier(
        acting, public_id, to=to, version=_version(current), note=note or None
    )
    # Show the refreshed dossier, with the outcome in the footer.
    return _card(locale, updated, t(locale, key, short_id=short(updated["public_id"])))


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
    return _card(
        locale,
        updated,
        t(
            locale,
            "dossier.published",
            short_id=short(updated["public_id"]),
            url=updated.get("published_url") or url,
        ),
    )
