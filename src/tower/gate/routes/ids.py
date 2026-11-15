"""Short identifiers typed by members (bot): prefix -> full public id."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from tower.core.usecases import ids as uc
from tower.gate.deps import ActorDep, ReadSession

router = APIRouter(prefix="/v1/ids", tags=["ids"])

Kind = Literal["case", "group", "claim", "narrative", "draft"]


class ResolvedId(BaseModel):
    kind: Kind
    id: str


@router.get("/{kind}/{ref}", response_model=ResolvedId)
async def resolve_id(kind: Kind, ref: str, actor: ActorDep, session: ReadSession) -> ResolvedId:
    """Full public id of the item whose id starts with ``ref`` (6 characters or more).
    ``TWR-GEN-002`` when none matches, ``TWR-GEN-005`` when several do."""
    return ResolvedId(kind=kind, id=str(await uc.resolve_id(session, actor, kind, ref)))
