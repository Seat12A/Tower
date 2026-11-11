"""Names shared by the fact-check services, gate, the bot and the docs: queues, job
kinds, topics, dedupe keys, budget scopes, model roles and prompt ids."""

from __future__ import annotations

# Queues
FC_FAST = "fc-fast"
FC_DEEP = "fc-deep"

# Jobs of the service ``factcheck`` (queue fc-fast)
JOB_EXTRACT = "factcheck.extract"
JOB_ASSEMBLE = "factcheck.assemble"
JOB_REQUEST = "factcheck.request"
JOB_TRANSLATE = "factcheck.translate"

# Jobs of the service ``investigate`` (queue fc-deep), one per stage
JOB_FRAME = "investigate.frame"
JOB_COLLECT = "investigate.collect"
JOB_JUDGE = "investigate.judge"
JOB_SECOND_OPINION = "investigate.second_opinion"
JOB_OBJECTION = "investigate.objection"
JOB_ESCALATE = "investigate.escalate"
JOB_DRAFT = "investigate.draft"

# Topics
CONTENT_INGESTED = "content.ingested"
CLAIM_EXTRACTED = "claim.extracted"
INVESTIGATION_REQUESTED = "investigation.requested"
INVESTIGATION_COMPLETED = "investigation.completed"
DRAFT_READY = "draft.ready"
DRAFT_REVIEWED = "draft.reviewed"
VERDICT_PUBLISHED = "verdict.published"

# Actors
FACTCHECK_ACTOR = "system:factcheck"
INVESTIGATE_ACTOR = "system:investigate"

# Model roles (routes in llm_routes) and prompt ids
ROLE_EXTRACT = "extract"
ROLE_FRAME = "frame"
ROLE_FRAME_REINFORCED = "frame_reinforced"
ROLE_QUERY_WRITER = "query_writer"
ROLE_JUDGE = "judge"
ROLE_JUDGE_REINFORCED = "judge_reinforced"
ROLE_SECOND_OPINION = "second_opinion"
ROLE_OBJECTION = "objection"
ROLE_WRITER = "writer"
ROLE_CRITIC = "critic"
ROLE_TRANSLATE = "translate"
"""Prompt id = role, except the reinforced roles which reuse ``frame`` and ``judge``."""

PROMPT_OF_ROLE = {
    ROLE_FRAME_REINFORCED: ROLE_FRAME,
    ROLE_JUDGE_REINFORCED: ROLE_JUDGE,
}


def prompt_id(role: str) -> str:
    return PROMPT_OF_ROLE.get(role, role)


def level_scope(level: str) -> str:
    """Budget scope of a level (``level:N3``), reserved in addition to ``global``."""
    return f"level:{level}"


def extract_key(content_id: int, level: str) -> str:
    return f"ext:{content_id}:{level}"


def stage_key(stage: str, group_id: int, investigation_id: int, round_: int) -> str:
    return f"{stage}:{group_id}:{investigation_id}:{round_}"


def assemble_key(investigation_id: int) -> str:
    return f"asm:{investigation_id}"


def request_key(platform: str, external_id: str) -> str:
    return f"req:{platform}:{external_id}"


def translate_key(verdict_id: int, version: int) -> str:
    return f"tr:{verdict_id}:{version}"
