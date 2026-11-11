"""Strict output schemas, one per model role (docs/fact-check.md, "Rôles").

JSON Schema keywords the providers do not support in structured outputs (string
lengths, numeric bounds, array sizes) are never emitted: limits are enforced by
pydantic validators after the call (an invalid output is retried by the router, then
falls back). Every field is required in the JSON Schema; optional fields are nullable.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict

from tower.core.domain.factcheck.values import (
    AbsenceReason,
    ClaimType,
    ContextNeed,
    ContextType,
    EntityRole,
    ObjectionDirection,
    ObjectionType,
    Polarity,
    Veracity,
)

__all__ = [
    "ClaimEntity",
    "ContextPoint",
    "CriticOutput",
    "Definition",
    "ExtractOutput",
    "ExtractedClaim",
    "FrameOutput",
    "JudgeAssertion",
    "JudgeCitation",
    "JudgeOutput",
    "ObjectionOutput",
    "PassageOrigin",
    "QueryItem",
    "QueryOutput",
    "SecondOpinionOutput",
    "Signature",
    "SubQuestion",
    "TimeFrame",
    "TranslateOutput",
    "WriterNote",
    "WriterOutput",
]


def _max_len(limit: int) -> AfterValidator:
    def check(value: str) -> str:
        if len(value) > limit:
            raise ValueError(f"longer than {limit} characters")
        return value

    return AfterValidator(check)


def _max_items(limit: int) -> AfterValidator:
    def check(value: list[object]) -> list[object]:
        if len(value) > limit:
            raise ValueError(f"more than {limit} items")
        return value

    return AfterValidator(check)


def _unit(value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError("must be between 0 and 1")
    return value


Short = Annotated[str, _max_len(300)]
Line = Annotated[str, _max_len(500)]
Para = Annotated[str, _max_len(1500)]
Unit = Annotated[float, AfterValidator(_unit)]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ----------------------------------------------------------------------- E2 extract


class Signature(_Out):
    """Language-neutral fields, taken from the original text (never translated)."""

    subject: str | None
    """Main subject as written (entity, indicator, population)."""
    subject_qid: str | None
    """Wikidata QID when certain (``Q142``), else null."""
    predicate: str | None
    """Measured quantity or predicate, short snake_case English (``unemployment_rate``)."""
    value_low: float | None
    value_high: float | None
    """Interval derived from the stated precision (``7 %`` → 6.5..7.5)."""
    unit: str | None
    unit_scale: str | None
    """``unit``, ``thousand``, ``million``, ``billion``."""
    currency: str | None
    """ISO 4217."""
    value_date: str | None
    period_start: str | None
    period_end: str | None
    """ISO 8601 dates (``YYYY-MM-DD``)."""
    place: str | None
    """ISO 3166-1/2 code, ``EU`` or ``INT``."""
    polarity: Polarity
    invoked_source: str | None
    definition_variant: str | None
    comparison_base: str | None


class ClaimEntity(_Out):
    name: Short
    role: EntityRole
    qid: str | None


class ExtractedClaim(_Out):
    text_original: Short
    """Canonical neutral claim in the post language (≤ 300 characters)."""
    text_fr: Short
    """The same claim in French (identical to ``text_original`` for French posts)."""
    claim_type: ClaimType
    signature: Signature
    jurisdictions: Annotated[list[str], _max_items(6)]
    primary_jurisdiction: str | None
    entities: Annotated[list[ClaimEntity], _max_items(10)]
    theme: str | None
    verifiability: Unit
    context_hints: Annotated[list[ContextType], _max_items(4)]
    source_modality: str
    """``text``, ``image`` or ``video``."""


class ExtractOutput(_Out):
    post_id: str
    """Identifier of the post, copied from the input."""
    claims: Annotated[list[ExtractedClaim], _max_items(5)]
    absence_reason: AbsenceReason | None
    """Required when ``claims`` is empty, null otherwise."""
    thread_context: Short | None
    """Neutral one-sentence rephrasing of the parent/quoted post, or null."""


# ------------------------------------------------------------------------ E6 frame


class Definition(_Out):
    term: Short
    candidates: Annotated[list[Short], _max_items(5)]
    note: Line


class TimeFrame(_Out):
    stated: Short | None
    resolved_start: str | None
    resolved_end: str | None
    """ISO dates."""
    confidence: str
    """``high``, ``medium`` or ``low``."""


class SubQuestion(_Out):
    question: Line
    expected_source_type: str
    """``statistic``, ``legal_text``, ``official_record``, ``scientific``, ``news``,
    ``encyclopedia``, ``fact_check``, ``transcript``."""
    jurisdiction: str | None


class FrameOutput(_Out):
    literal_claim: Line
    implied_claims: Annotated[list[Line], _max_items(4)]
    charitable_reading: Line
    key_definitions: Annotated[list[Definition], _max_items(4)]
    time_frame: TimeFrame
    jurisdiction: str | None
    true_if: Annotated[list[Line], _max_items(5)]
    false_if: Annotated[list[Line], _max_items(5)]
    known_pitfalls: Annotated[list[ContextType], _max_items(5)]
    sub_questions: Annotated[list[SubQuestion], _max_items(6)]
    verifiable: bool
    verifiability_reason: Line
    context_hypotheses: Annotated[list[ContextType], _max_items(5)]


# ------------------------------------------------------------------ E7 query writer


class QueryItem(_Out):
    query: Short
    lang: str
    """Language of the query = language of the targeted sources."""
    source_type: str
    """Same vocabulary as ``SubQuestion.expected_source_type``."""
    target: str
    """``encyclopedia`` (Wikipedia/Wikidata lookup), ``fact_check`` or ``web``."""


class QueryOutput(_Out):
    confirm: Annotated[list[QueryItem], _max_items(8)]
    """Queries that would find evidence the claim is true."""
    refute: Annotated[list[QueryItem], _max_items(8)]
    """Symmetric queries that would find evidence the claim is false."""
    entity_lookups: Annotated[list[Short], _max_items(4)]
    """Entity names to resolve in Wikidata (statements with references)."""


# ------------------------------------------------------------------------ E8 judge


class JudgeCitation(_Out):
    passage_id: str
    """``P1``…"""
    quote: Para
    """Exact words copied from the passage (shortest span that supports the assertion)."""


class JudgeAssertion(_Out):
    assertion: Line
    """One statement of the justification, in French."""
    citations: Annotated[list[JudgeCitation], _max_items(4)]


class PassageOrigin(_Out):
    passage_id: str
    origin: Short
    """Entity the information is attributed to (ministry, agency, study, court…)."""
    primary: bool
    """The passage is the producer of the data or text itself (not a report of it)."""


class ContextPoint(_Out):
    context_type: ContextType
    element: Line
    """The missing information, in French, supported by the cited passages."""
    passage_ids: Annotated[list[str], _max_items(4)]


class JudgeOutput(_Out):
    veracity: Veracity
    context_need: ContextNeed
    context: Annotated[list[ContextPoint], _max_items(4)]
    rationale: Annotated[list[JudgeAssertion], _max_items(8)]
    origins: Annotated[list[PassageOrigin], _max_items(40)]
    conflicting_sources: bool
    """Credible passages point to different outcomes."""
    not_in_sources: Annotated[list[Line], _max_items(5)]
    review_points: Annotated[list[Line], _max_items(5)]
    needs_more_research: Line | None
    """What is missing and where to look, or null."""


class SecondOpinionOutput(_Out):
    veracity: Veracity
    context_need: ContextNeed
    key_passage_ids: Annotated[list[str], _max_items(6)]
    reason: Para


class ObjectionOutput(_Out):
    objection_type: ObjectionType
    passage_id: str
    direction: ObjectionDirection
    explanation: Para
    """In French, for the reviewer."""


# ----------------------------------------------------------------------- E9 writer


class WriterNote(_Out):
    lang: str
    text: Annotated[str, _max_len(600)]
    """≤ 280 characters excluding links (checked deterministically)."""
    fact_sheet: Annotated[str, _max_len(2000)]
    """Short fact sheet (3 to 6 sentences) in the same language."""


class WriterOutput(_Out):
    notes: Annotated[list[WriterNote], _max_items(3)]
    """Post language, then the reference languages requested in the input."""
    links: Annotated[list[str], _max_items(4)]
    """URLs of retrieved sources supporting the note (copied from the input)."""
    primary_context_type: ContextType | None


class CriticOutput(_Out):
    useful_for_agreeing_reader: bool
    useful_for_disagreeing_reader: bool
    contains_opinion: bool
    contains_generalization: bool
    loaded_words: Annotated[list[Short], _max_items(8)]
    unsourced_facts: Annotated[list[Line], _max_items(5)]
    judges_author: bool
    comments: Para
    """In French, for the reviewer. The critic flags; it never rewrites."""


# ------------------------------------------------------------------- translation


class TranslateOutput(_Out):
    lang: str
    text: Annotated[str, _max_len(4000)]
