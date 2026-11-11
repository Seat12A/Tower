"""Closed values of the fact-check pipeline (see docs/factcheck/contexte.md, pipeline.md).

Pure domain: standard library only. Every value here is stored as ``TEXT`` + ``CHECK``
and exposed as-is in the API and the events.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CLAIM_TYPES_CAUSAL",
    "CONTEXT_REGIME",
    "PROVISIONAL_NOTE_TTL_S",
    "STAKEHOLDER_CLAIM_TYPES",
    "UNVERIFIABLE_CLAIM_TYPES",
    "AbsenceReason",
    "Agreement",
    "ClaimGroupState",
    "ClaimType",
    "ContextNeed",
    "ContextType",
    "Depth",
    "EntityRole",
    "InvestigationStage",
    "NoteStatus",
    "ObjectionDirection",
    "ObjectionType",
    "Polarity",
    "Regime",
    "ReviewDecision",
    "ReviewLabel",
    "ReviewOutcome",
    "Side",
    "TranslationOrigin",
    "Veracity",
    "VerdictStatus",
    "side_of",
]


class Veracity(StrEnum):
    TRUE = "true"
    MOSTLY_TRUE = "mostly_true"
    MISLEADING = "misleading"
    FALSE = "false"
    UNVERIFIABLE = "unverifiable"


class Side(StrEnum):
    """Side of an outcome, used to compare the judge and the second opinion."""

    SUPPORTS = "supports"  # true, mostly_true
    REFUTES = "refutes"  # misleading, false
    ABSTAINS = "abstains"  # unverifiable


def side_of(veracity: Veracity) -> Side:
    if veracity in (Veracity.TRUE, Veracity.MOSTLY_TRUE):
        return Side.SUPPORTS
    if veracity in (Veracity.MISLEADING, Veracity.FALSE):
        return Side.REFUTES
    return Side.ABSTAINS


class ContextNeed(StrEnum):
    NONE = "none"
    USEFUL = "useful"
    NECESSARY = "necessary"


class Regime(StrEnum):
    """Reuse regime of a context note."""

    OBJECT = "object"
    FORMULATION = "formulation"
    PROVISIONAL = "provisional"


class ContextType(StrEnum):
    """The 15 context types of docs/factcheck/contexte.md."""

    OUTDATED = "outdated"
    RECYCLED_MEDIA = "recycled_media"
    MANIPULATED_MEDIA = "manipulated_media"
    CHERRY_PICKING = "cherry_picking"
    MISSING_BASELINE = "missing_baseline"
    QUOTE_INTEGRITY = "quote_integrity"
    MISREPRESENTED_SOURCE = "misrepresented_source"
    CAUSATION = "causation"
    DEFINITION = "definition"
    SCOPE = "scope"
    PROCEDURE = "procedure"
    SCIENTIFIC_UNCERTAINTY = "scientific_uncertainty"
    SATIRE = "satire"
    MISSING_EVENT_CONTEXT = "missing_event_context"
    DEVELOPING = "developing"


CONTEXT_REGIME: dict[ContextType, Regime] = {
    ContextType.OUTDATED: Regime.OBJECT,
    ContextType.RECYCLED_MEDIA: Regime.OBJECT,
    ContextType.MANIPULATED_MEDIA: Regime.OBJECT,
    ContextType.CHERRY_PICKING: Regime.FORMULATION,
    ContextType.MISSING_BASELINE: Regime.FORMULATION,
    ContextType.QUOTE_INTEGRITY: Regime.OBJECT,
    ContextType.MISREPRESENTED_SOURCE: Regime.OBJECT,
    ContextType.CAUSATION: Regime.FORMULATION,
    ContextType.DEFINITION: Regime.OBJECT,
    ContextType.SCOPE: Regime.FORMULATION,
    ContextType.PROCEDURE: Regime.OBJECT,
    ContextType.SCIENTIFIC_UNCERTAINTY: Regime.FORMULATION,
    ContextType.SATIRE: Regime.OBJECT,
    ContextType.MISSING_EVENT_CONTEXT: Regime.OBJECT,
    ContextType.DEVELOPING: Regime.PROVISIONAL,
}
"""A provisional note expires after at most 6 hours (``PROVISIONAL_NOTE_TTL_S``)."""

PROVISIONAL_NOTE_TTL_S = 6 * 3600


class ClaimType(StrEnum):
    STATISTIC = "statistic"
    HISTORICAL_FACT = "historical_fact"
    QUOTE = "quote"
    LEGAL_TEXT = "legal_text"
    ONGOING_SITUATION = "ongoing_situation"
    PROCEDURE = "procedure"
    RECORD = "record"  # "bilan": an actor's own track record
    CONDUCT = "conduct"
    ACCUSATION = "accusation"
    PREDICTION = "prediction"
    OPINION = "opinion"
    CAUSAL = "causal"
    OTHER = "other"


STAKEHOLDER_CLAIM_TYPES = frozenset({ClaimType.RECORD, ClaimType.CONDUCT, ClaimType.ACCUSATION})
"""Claim types for which a stakeholder source is not independent (Q11)."""

CLAIM_TYPES_CAUSAL = frozenset({ClaimType.CAUSAL})

UNVERIFIABLE_CLAIM_TYPES = frozenset({ClaimType.PREDICTION, ClaimType.OPINION})


class Polarity(StrEnum):
    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"


class EntityRole(StrEnum):
    """Role of an entity in a claim (stakeholder rule, Q11)."""

    ACTOR = "actor"  # author of the assessed conduct
    ACCUSER = "accuser"
    ACCUSED = "accused"
    SOURCE = "source"  # source invoked by the claim
    SUBJECT = "subject"
    OTHER = "other"


class AbsenceReason(StrEnum):
    """Why a post yields no claim (closed list of E2)."""

    OPINION = "opinion"
    QUESTION = "question"
    SATIRE = "satire"
    NO_CLAIM = "no_claim"
    UNREADABLE = "unreadable"


class ObjectionType(StrEnum):
    MISREAD_EVIDENCE = "misread_evidence"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_INDEPENDENT = "not_independent"
    WRONG_PERIOD_OR_SCOPE = "wrong_period_or_scope"
    DIFFERENT_DEFINITION = "different_definition"
    UNFAITHFUL_FRAMING = "unfaithful_framing"


class ObjectionDirection(StrEnum):
    MORE_SEVERE = "more_severe"
    LESS_SEVERE = "less_severe"
    ABSTAIN = "abstain"


class Agreement(StrEnum):
    """Comparison of the judge and the blind second opinion (Q3)."""

    IDENTICAL = "identical"
    STYLE = "style"  # one notch apart on the same side
    SUBSTANTIVE = "substantive"  # side change, or verdict against abstention


class Depth(StrEnum):
    STANDARD = "standard"
    REINFORCED = "reinforced"


class InvestigationStage(StrEnum):
    FRAMING = "framing"
    COLLECTING = "collecting"
    JUDGING = "judging"
    SECOND_OPINION = "second_opinion"
    OBJECTION = "objection"
    ESCALATING = "escalating"
    DRAFTING = "drafting"
    COMPLETED = "completed"  # E9 done, waiting for E10 (assembly)
    ASSEMBLED = "assembled"  # E10 done (dossier opened, or kept off the review queue)
    FAILED = "failed"


class ClaimGroupState(StrEnum):
    DORMANT = "dormant"
    QUEUED = "queued"
    INVESTIGATING = "investigating"
    VERIFIED = "verified"
    TO_REVALIDATE = "to_revalidate"
    PASSIVE = "passive"


class VerdictStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    REJECTED = "rejected"


class NoteStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TranslationOrigin(StrEnum):
    ORIGINAL = "original"  # model version read and validated by the reviewer
    HUMAN = "human"  # written or corrected by a team member
    MACHINE = "machine"  # automatic translation, flagged to readers


class ReviewDecision(StrEnum):
    ACCEPT = "accept"
    EDIT = "edit"
    REJECT = "reject"


class ReviewOutcome(StrEnum):
    """Effect of a review on the draft."""

    ENDORSED = "endorsed"  # reviewer approval, the draft waits for an editor
    VALIDATED = "validated"  # editor decision: verdict active, verdict.published
    REJECTED = "rejected"


class ReviewLabel(StrEnum):
    """Learning labels (E11)."""

    TRIAGE_FALSE_POSITIVE = "triage_false_positive"
    WRONG_GROUPING = "wrong_grouping"
    WRONG_SOURCE = "wrong_source"
    WRONG_FRAMING = "wrong_framing"
    WRONG_VERACITY = "wrong_veracity"
    WRONG_CONTEXT = "wrong_context"
    NOTE_REWRITTEN = "note_rewritten"
    NOT_WORTH_CHECKING = "not_worth_checking"
