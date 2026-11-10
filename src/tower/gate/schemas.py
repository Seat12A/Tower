"""Schémas HTTP. Les IDs X et Discord sont toujours des chaînes (précision JavaScript)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from tower.core.domain.dossiers import DossierKind, DossierStatus
from tower.core.models.dossiers import Dossier, DossierEvent


class DossierOut(BaseModel):
    public_id: uuid.UUID
    kind: DossierKind
    status: DossierStatus
    x_post_id: str | None
    title: str | None
    priority: int
    assigned_to: str | None
    published_url: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, d: Dossier) -> DossierOut:
        return cls(
            public_id=d.public_id,
            kind=DossierKind(d.kind),
            status=DossierStatus(d.status),
            x_post_id=None if d.x_post_id is None else str(d.x_post_id),
            title=d.title,
            priority=d.priority,
            assigned_to=None if d.assigned_to is None else str(d.assigned_to),
            published_url=d.published_url,
            version=d.version,
            created_at=d.created_at,
            updated_at=d.updated_at,
        )


class DossierPublicOut(BaseModel):
    """Vue du site public : uniquement les dossiers publiés, sans données internes."""

    public_id: uuid.UUID
    kind: DossierKind
    x_post_id: str | None
    title: str | None
    published_url: str | None
    updated_at: datetime


class DossierEventOut(BaseModel):
    from_status: DossierStatus | None
    to_status: DossierStatus
    actor: str
    reason: str | None
    created_at: datetime

    @classmethod
    def of(cls, ev: DossierEvent) -> DossierEventOut:
        return cls(
            from_status=None if ev.from_status is None else DossierStatus(ev.from_status),
            to_status=DossierStatus(ev.to_status),
            actor=ev.actor,
            reason=ev.reason,
            created_at=ev.created_at,
        )


class DossierCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: DossierKind
    x_post_id: str | None = Field(default=None, pattern=r"^\d{1,20}$")
    title: str | None = Field(default=None, max_length=300)
    priority: int = Field(default=0, ge=-100, le=100)
    reason: str | None = Field(default=None, max_length=1000)


class TransitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: DossierStatus
    reason: str | None = Field(default=None, max_length=1000)
    expected_version: int | None = None
    published_url: str | None = Field(default=None, max_length=300)


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=300)
    expected_version: int | None = None
