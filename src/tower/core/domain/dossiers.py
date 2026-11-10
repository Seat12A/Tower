"""Types et cycle de vie des dossiers.

Deux types partagent le même socle : ``fact_check`` et ``moderation`` (contenus X).
Chaque type a son propre graphe de transitions, défini ici et nulle part ailleurs.
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
    RECEIVED = "received"  # reçu
    IN_REVIEW = "in_review"  # en revue
    VALIDATED = "validated"  # validé
    PUBLISHED = "published"  # publié (lien de la publication X enregistré)
    REJECTED = "rejected"  # rejeté


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
    # La modération suit pour l'instant le même cycle ; à spécialiser ici au besoin.
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
            f"Transition interdite pour un dossier {kind} : {current} → {target}",
            kind=kind.value,
            current=current.value,
            target=target.value,
            allowed=sorted(s.value for s in WORKFLOWS[kind][current]),
        )


def parse_publication_url(url: str) -> int:
    """Valide le lien de la publication X de l'équipe et renvoie l'ID du post."""
    match = _X_STATUS_URL.match(url.strip())
    if not match:
        raise InvalidInput(
            "Lien de publication X invalide (https://x.com/<compte>/status/<id>)",
            code=ec.DOS_BAD_PUBLICATION_URL,
        )
    return int(match.group(1))
