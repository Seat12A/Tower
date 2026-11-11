"""Fake model provider (``TOWER_LLM_FAKE=1``): schema-valid outputs per role, for
development, smoke tests and the evaluation demo. Never used in production."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from tower.core.domain.factcheck.datamark import unmark
from tower.core.domain.llm import TokenUsage
from tower.core.logging import get_logger
from tower.core.ports.llm import ProviderInvalidOutput, ProviderResponse

__all__ = ["FakeEmbedder", "FakeLLMProvider"]

log = get_logger(__name__)

_INPUT = re.compile(r"<input>\n(.*)\n</input>", re.DOTALL)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_NOISE = re.compile(r"https?://\S+|@\w+")
_SPACE_THOUSANDS = re.compile(r"(?<![0-9])([0-9]{1,3})((?:[   ][0-9]{3}(?![0-9]))+)")
_NUMBER = re.compile(r"[0-9]+(?:[.,][0-9]+)*")
_FIRST_WORDS = re.compile(r"(?:\S+\s+){0,11}\S+")
_CAPITALIZED = re.compile(r"\b[A-ZÀ-Ý][\w'-]{2,}")
_warned = False


class FakeLLMProvider:
    """``LLMProvider`` producing deterministic outputs from the input (by schema name)."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Mapping[str, str]],
        json_schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
        params: Mapping[str, Any],
    ) -> ProviderResponse:
        global _warned
        if not _warned:
            _warned = True
            log.warning("llm.fake_provider", provider=self.name)
        from tower.core.factcheck import schemas

        schema = getattr(schemas, schema_name, None)
        builder = _BUILDERS.get(schema_name)
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)) or builder is None:
            raise ProviderInvalidOutput(f"Fake provider: unknown schema {schema_name!r}")
        message = messages[-1]["content"] if messages else ""
        match = _INPUT.search(message)
        if match is None:
            raise ProviderInvalidOutput("Fake provider: no <input> block in the message")
        try:
            data = json.loads(match.group(1))
        except ValueError as exc:
            raise ProviderInvalidOutput("Fake provider: the input is not valid JSON") from exc
        output = schema.model_validate(builder(_unmark_all(data)))
        payload = output.model_dump(mode="json")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha1(  # noqa: S324 (request id, not a security use)
            (schema_name + message).encode()
        ).hexdigest()
        return ProviderResponse(
            output_json=payload,
            usage=TokenUsage(
                input_tokens=len(system + message) // 4, output_tokens=len(encoded) // 4
            ),
            request_id=f"fake-{digest[:12]}",
            stop_reason="end_turn",
        )


def _unmark_all(value: Any) -> Any:
    if isinstance(value, str):
        return unmark(value)
    if isinstance(value, list):
        return [_unmark_all(v) for v in value]
    if isinstance(value, dict):
        return {k: _unmark_all(v) for k, v in value.items()}
    return value


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _host(url: str) -> str:
    match = re.match(r"https?://([^/\s]+)", url)
    return match.group(1) if match else url


# ------------------------------------------------------------------------- builders


def _first_number(sentence: str) -> float | None:
    """First number of ``sentence``, read like the fidelity check reads numbers."""
    from tower.core.domain.factcheck.fidelity import extract_numbers

    cleaned = _NOISE.sub(" ", sentence)
    cleaned = _SPACE_THOUSANDS.sub(lambda m: m.group(1) + re.sub(r"\D", "", m.group(2)), cleaned)
    for token in _NUMBER.finditer(cleaned):
        numbers = sorted(extract_numbers(token.group(0)), key=float)
        if numbers:
            return float(numbers[0])
    return None


def _extract(data: Mapping[str, Any]) -> dict[str, Any]:
    post = data["post"]
    text = str(post["text"]).strip()
    lang = str(post.get("lang") or "")
    out: dict[str, Any] = {"post_id": str(post["id"]), "thread_context": None}
    if post.get("context"):
        out["thread_context"] = "Relates to another post."
    if len(text.split()) < 4:
        return {**out, "claims": [], "absence_reason": "no_claim"}
    sentence = _cut(_SENTENCE_END.split(text, maxsplit=1)[0], 280)
    number = _first_number(sentence)
    signature = {
        "subject": None,
        "subject_qid": None,
        "predicate": None,
        "value_low": number,
        "value_high": number,
        "unit": None,
        "unit_scale": None,
        "currency": None,
        "value_date": None,
        "period_start": None,
        "period_end": None,
        "place": None,
        "polarity": "affirmative",
        "invoked_source": None,
        "definition_variant": None,
        "comparison_base": None,
    }
    claim = {
        "text_original": sentence,
        "text_fr": sentence,
        "claim_type": "statistic" if any(c.isdigit() for c in sentence) else "other",
        "signature": signature,
        "jurisdictions": ["FR"] if lang == "fr" else [],
        "primary_jurisdiction": "FR" if lang == "fr" else None,
        "entities": [],
        "theme": None,
        "verifiability": 0.8,
        "context_hints": [],
        "source_modality": "text",
    }
    return {**out, "claims": [claim], "absence_reason": None}


def _frame(data: Mapping[str, Any]) -> dict[str, Any]:
    text = str(data["claim"]["text_original"])
    return {
        "literal_claim": _cut(text, 500),
        "implied_claims": [],
        "charitable_reading": _cut(text, 500),
        "key_definitions": [],
        "time_frame": {
            "stated": None,
            "resolved_start": None,
            "resolved_end": None,
            "confidence": "low",
        },
        "jurisdiction": None,
        "true_if": ["The cited sources state the same thing as the claim."],
        "false_if": ["The cited sources state something different from the claim."],
        "known_pitfalls": [],
        "sub_questions": [
            {
                "question": _cut(f"Do reliable sources establish that: {text}", 500),
                "expected_source_type": "news",
                "jurisdiction": None,
            }
        ],
        "verifiable": True,
        "verifiability_reason": "Public sources can settle this claim.",
        "context_hypotheses": [],
    }


def _query(data: Mapping[str, Any]) -> dict[str, Any]:
    claim = data["claim"]
    text = str(claim["text_original"])
    lang = str(claim.get("lang") or "fr")
    head = " ".join(text.split()[:6])

    def item(prefix: str, source_type: str, target: str) -> dict[str, str]:
        return {
            "query": _cut(f"{prefix} {head}".strip(), 300),
            "lang": lang,
            "source_type": source_type,
            "target": target,
        }

    return {
        "confirm": [item("", "encyclopedia", "encyclopedia"), item("confirmed", "news", "web")],
        "refute": [item("false", "fact_check", "fact_check"), item("debunked", "news", "web")],
        "entity_lookups": [_cut(w, 300) for w in _CAPITALIZED.findall(text)[:2]],
    }


def _passage_ids(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(data.get("passages") or [])


def _judge_core(data: Mapping[str, Any]) -> tuple[str, str]:
    return ("mostly_true", "useful") if _passage_ids(data) else ("unverifiable", "none")


def _judge(data: Mapping[str, Any]) -> dict[str, Any]:
    passages = _passage_ids(data)
    veracity, need = _judge_core(data)
    out: dict[str, Any] = {
        "veracity": veracity,
        "context_need": need,
        "context": [],
        "rationale": [],
        "origins": [],
        "conflicting_sources": False,
        "not_in_sources": [],
        "review_points": [],
        "needs_more_research": None,
    }
    if not passages:
        return out
    cited = passages[:2]
    citations = []
    for p in cited:
        match = _FIRST_WORDS.match(str(p["text"]).strip())
        citations.append({"passage_id": p["id"], "quote": match.group(0) if match else ""})
    out["rationale"] = [
        {
            "assertion": "Les sources consultées vont dans le sens de l'affirmation.",
            "citations": citations,
        }
    ]
    out["context"] = [
        {
            "context_type": "missing_baseline",
            "element": "Un point de comparaison manque pour situer le chiffre.",
            "passage_ids": [p["id"] for p in cited],
        }
    ]
    for p in passages[:40]:
        source = p.get("source") or {}
        out["origins"].append(
            {
                "passage_id": p["id"],
                "origin": _cut(
                    str(source.get("publisher") or source.get("domain") or "unknown"), 300
                ),
                "primary": source.get("tier") == "primary",
            }
        )
    return out


def _second_opinion(data: Mapping[str, Any]) -> dict[str, Any]:
    passages = _passage_ids(data)
    veracity, need = _judge_core(data)
    return {
        "veracity": veracity,
        "context_need": need,
        "key_passage_ids": [p["id"] for p in passages[:2]],
        "reason": "Independent reading of the same passages.",
    }


def _objection(data: Mapping[str, Any]) -> dict[str, Any]:
    passages = _passage_ids(data)
    return {
        "objection_type": "insufficient_evidence",
        "passage_id": passages[0]["id"] if passages else "P1",
        "direction": "abstain",
        "explanation": "Les passages ne suffisent pas à établir clairement l'issue.",
    }


def _writer(data: Mapping[str, Any]) -> dict[str, Any]:
    sources = list(data.get("sources") or [])
    claim = str(data["claim"]["text_original"])
    first = sources[0] if sources else None
    who = (
        str(first.get("publisher") or _host(str(first["url"])))
        if first
        else "les sources consultées"
    )
    prefix = f"[démo] Selon {who}, "
    note = prefix + _cut(claim, max(20, 250 - len(prefix) - 1)) + "."
    notes = [
        {
            "lang": lang,
            "text": note,
            "fact_sheet": _cut(
                f"Fiche générée en mode démonstration. Affirmation vérifiée : {claim}",
                2000,
            ),
        }
        for lang in data.get("languages") or []
    ]
    points = (data.get("verdict") or {}).get("context") or []
    context_type = points[0].get("context_type") if points else None
    return {
        "notes": notes,
        "links": [str(first["url"])] if first else [],
        "primary_context_type": context_type,
    }


def _critic(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "useful_for_agreeing_reader": True,
        "useful_for_disagreeing_reader": True,
        "contains_opinion": False,
        "contains_generalization": False,
        "loaded_words": [],
        "unsourced_facts": [],
        "judges_author": False,
        "comments": "",
    }


def _translate(data: Mapping[str, Any]) -> dict[str, Any]:
    target = str(data["target_lang"])
    text = f"[{target}] {data['text']}"
    if data.get("kind") == "context_note":
        text = _cut(text, 280)
    return {"lang": target, "text": text}


_BUILDERS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "ExtractOutput": _extract,
    "FrameOutput": _frame,
    "QueryOutput": _query,
    "JudgeOutput": _judge,
    "SecondOpinionOutput": _second_opinion,
    "ObjectionOutput": _objection,
    "WriterOutput": _writer,
    "CriticOutput": _critic,
    "TranslateOutput": _translate,
}


# ------------------------------------------------------------------- fake embeddings

_EMBED_DIMS = 512
_EMBED_MODEL = "fake-embedding-512"


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


class FakeEmbedder:
    """Deterministic embedder for tests and ``TOWER_LLM_FAKE=1`` (no network).

    Features: accent-stripped, casefolded word unigrams (weight 1.0) and character
    trigrams of each word (weight 0.5), hashed to 512 signed buckets, L2-normalized.
    """

    model = _EMBED_MODEL
    version = _EMBED_MODEL

    def __init__(self) -> None:
        self.calls: list[int] = []
        """Size of each ``embed`` call (tests)."""

    @staticmethod
    def _bucket(feature: str) -> tuple[int, float]:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        number = int.from_bytes(digest, "big")
        return number % _EMBED_DIMS, 1.0 if (number >> 63) & 1 else -1.0

    def _vector(self, text: str) -> list[float]:
        words = re.findall(r"\w+", _strip_accents(text))
        vec = [0.0] * _EMBED_DIMS
        for word in words:
            idx, sign = self._bucket("w:" + word)
            vec[idx] += sign
            padded = f"^{word}$"
            for i in range(len(padded) - 2):
                idx, sign = self._bucket("t:" + padded[i : i + 3])
                vec[idx] += 0.5 * sign
        norm = math.sqrt(math.fsum(v * v for v in vec))
        if norm == 0.0:
            vec = [0.0] * _EMBED_DIMS
            vec[0] = 1.0
        else:
            vec = [v / norm for v in vec]
        return vec

    async def embed(self, texts: Sequence[str], *, dimensions: int) -> list[list[float]]:
        if dimensions != _EMBED_DIMS:
            raise ValueError(f"fake embedder only produces {_EMBED_DIMS} dimensions")
        self.calls.append(len(texts))
        return [self._vector(t) for t in texts]
