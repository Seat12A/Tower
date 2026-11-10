"""Dossier types and lifecycle.

Two types share the same foundation: ``fact_check`` and ``moderation`` (X content).
Each type has its own transition graph, defined here and nowhere else.
"""

from __future__ import annotations

import re
from enum import StrEnum

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, InvalidTransition
from tower.core.domain.security import Permission


class DossierKind(StrEnum):
    FACT_CHECK = "fact_check"
    MODERATION = "moderation"


class DossierStatus(StrEnum):
    RECEIVED = "received"  # received
    IN_REVIEW = "in_review"  # in review
    VALIDATED = "validated"  # validated
    PUBLISHED = "published"  # published (X publication link recorded)
    REJECTED = "rejected"  # rejected


S = DossierStatus

_EDITORIAL_FLOW: dict[DossierStatus, frozenset[DossierStatus]] = {
    S.RECEIVED: frozenset({S.IN_REVIEW}),
    S.IN_REVIEW: frozenset({S.VALIDATED, S.REJECTED}),
    S.VALIDATED: frozenset({S.PUBLISHED}),
    S.PUBLISHED: frozenset(),
    S.REJECTED: frozenset(),
}

WORKFLOWS: dict[DossierKind, dict[DossierStatus, frozenset[DossierStatus]]] = {
    DossierKind.FACT_CHECK: _EDITORIAL_FLOW,
    # Moderation currently follows the same cycle; specialize it here if needed.
    DossierKind.MODERATION: _EDITORIAL_FLOW,
}

INITIAL_STATUS = S.RECEIVED

TRANSITION_PERMISSION: dict[DossierStatus, Permission] = {
    S.IN_REVIEW: Permission.DOSSIER_REVIEW,
    S.VALIDATED: Permission.DOSSIER_DECIDE,
    S.REJECTED: Permission.DOSSIER_DECIDE,
    S.PUBLISHED: Permission.DOSSIER_PUBLISH,
}

_X_STATUS_URL = re.compile(
    r"^https://(?:www\.|mobile\.)?(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/(\d{1,20})(?:[/?#].*)?$"
)


def allowed_transitions(kind: DossierKind, current: DossierStatus) -> frozenset[DossierStatus]:
    return WORKFLOWS[kind][current]


def is_terminal(kind: DossierKind, status: DossierStatus) -> bool:
    return not WORKFLOWS[kind][status]


def check_transition(kind: DossierKind, current: DossierStatus, target: DossierStatus) -> None:
    if target not in WORKFLOWS[kind][current]:
        raise InvalidTransition(
            f"Transition not allowed for a dossier {kind}: {current} → {target}",
            kind=kind.value,
            current=current.value,
            target=target.value,
            allowed=sorted(s.value for s in WORKFLOWS[kind][current]),
        )


def parse_publication_url(url: str) -> int:
    """Validate the team's X publication link and return the post ID."""
    match = _X_STATUS_URL.match(url.strip())
    if not match:
        raise InvalidInput(
            "Invalid X publication link (https://x.com/<account>/status/<id>)",
            code=ec.DOS_BAD_PUBLICATION_URL,
        )
    return int(match.group(1))
