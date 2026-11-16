"""Organisation space: a badged organisation manages its affiliated accounts.

Authentication: ``X-Organization-Token: t12o_…`` (key issued by the team). The shared
endpoints ``/v1/badges/affiliations/{id}/validate|reconfirm|reject|revoke`` also accept
this token for the organisation's own affiliations.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from tower.core.usecases import badge_affiliations as aff
from tower.core.usecases import badges as uc
from tower.gate.deps import ActorDep, ReadSession, WriteSession
from tower.gate.routes.badges import AccountIn

router = APIRouter(prefix="/v1/org", tags=["badges"])


@router.get("/me")
async def me(actor: ActorDep, session: ReadSession) -> dict[str, Any]:
    """The organisation behind the token and its badge."""
    badge, account = await uc.org_badge_of_actor(session, actor)
    return uc.badge_payload(badge, account, internal=False)


@router.get("/affiliations")
async def list_affiliations(
    actor: ActorDep,
    session: ReadSession,
    status: Literal["pending", "active", "rejected", "revoked", "expired"] | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return await aff.list_affiliations(session, actor, status=status, limit=limit, offset=offset)


class OrgAffiliationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member: AccountIn
    title: str | None = Field(default=None, max_length=120)


@router.post("/affiliations", status_code=201)
async def add_affiliation(
    body: OrgAffiliationIn, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    """Affiliate an account: active at once (validated by the organisation), one year."""
    a = await aff.create_affiliation(
        session, actor, body.member.ref(), title=body.title, handle=body.member.handle
    )
    return await aff.payload_of(session, a)
