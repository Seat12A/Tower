"""Case types and lifecycle.

Two types share the same foundation: ``fact_check`` and ``moderation`` (X content).
Each type has its own transition graph, defined here and nowhere else.
"""

from __future__ import annotations

import re
from enum import StrEnum

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, InvalidTransition
from tower.core.domain.security import Permission

LEGACY_TOPICS: dict[str, str] = {
    "dossier.opened": "case.opened",
    "dossier.transitioned": "case.transitioned",
}
"""Topics published before the dossier -> case rename (expand step: still understood;
contract: drop once no stored event or client uses them)."""


def current_topic(topic: str) -> str:
    """Current name of a topic (a legacy name is translated)."""
    return LEGACY_TOPICS.get(topic, topic)


def with_legacy_topics(topics: list[str]) -> list[str]:
    """A topic filter completed with the old and new names of the renamed topics."""
    out = list(topics)
    for old, new in LEGACY_TOPICS.items():
        if old in out and new not in out:
            out.append(new)
        if new in out and old not in out:
            out.append(old)
    return out


class CaseKind(StrEnum):
    FACT_CHECK = "fact_check"
    MODERATION = "moderation"


class CaseStatus(StrEnum):
    RECEIVED = "received"  # received
    IN_REVIEW = "in_review"  # in review
    VALIDATED = "validated"  # validated
    PUBLISHED = "published"  # published (X publication link recorded)
    REJECTED = "rejected"  # rejected


S = CaseStatus

_EDITORIAL_FLOW: dict[CaseStatus, frozenset[CaseStatus]] = {
    S.RECEIVED: frozenset({S.IN_REVIEW}),
    S.IN_REVIEW: frozenset({S.VALIDATED, S.REJECTED}),
    S.VALIDATED: frozenset({S.PUBLISHED}),
    S.PUBLISHED: frozenset(),
    S.REJECTED: frozenset(),
}

WORKFLOWS: dict[CaseKind, dict[CaseStatus, frozenset[CaseStatus]]] = {
    CaseKind.FACT_CHECK: _EDITORIAL_FLOW,
    # Moderation currently follows the same cycle; specialize it here if needed.
    CaseKind.MODERATION: _EDITORIAL_FLOW,
}

INITIAL_STATUS = S.RECEIVED

TRANSITION_PERMISSION: dict[CaseStatus, Permission] = {
    S.IN_REVIEW: Permission.CASE_REVIEW,
    S.VALIDATED: Permission.CASE_DECIDE,
    S.REJECTED: Permission.CASE_DECIDE,
    S.PUBLISHED: Permission.CASE_PUBLISH,
}

_X_STATUS_URL = re.compile(
    r"^https://(?:www\.|mobile\.)?(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/(\d{1,20})(?:[/?#].*)?$"
)


def allowed_transitions(kind: CaseKind, current: CaseStatus) -> frozenset[CaseStatus]:
    return WORKFLOWS[kind][current]


def is_terminal(kind: CaseKind, status: CaseStatus) -> bool:
    return not WORKFLOWS[kind][status]


def check_transition(kind: CaseKind, current: CaseStatus, target: CaseStatus) -> None:
    if target not in WORKFLOWS[kind][current]:
        raise InvalidTransition(
            f"Transition not allowed for a case {kind}: {current} → {target}",
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
