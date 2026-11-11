"""Deep investigation service (pipeline.md E6 to E9): framing, collection, judgement,
second opinion, objection, escalation and draft, one job per stage on the ``fc-deep`` queue.

Only ``tower.core`` is imported. Triggered by ``investigation.requested``; publishes
``investigation.completed`` (see ``core/usecases/investigations.py``).
"""

from __future__ import annotations

from tower.core.factcheck import names
from tower.core.services.base import JobSpec, Service, Subscription
from tower.services.investigate.jobs import (
    collect,
    draft,
    escalate,
    frame,
    judge,
    objection,
    on_requested,
    second_opinion,
)

TIMEOUT_S = 240

service = Service(
    name="investigate",
    description="Deep fact-check investigation (framing, evidence, judgement, draft)",
    jobs=tuple(
        JobSpec(kind=kind, handler=handler, queue=names.FC_DEEP, timeout=TIMEOUT_S)
        for kind, handler in (
            (names.JOB_FRAME, frame),
            (names.JOB_COLLECT, collect),
            (names.JOB_JUDGE, judge),
            (names.JOB_SECOND_OPINION, second_opinion),
            (names.JOB_OBJECTION, objection),
            (names.JOB_ESCALATE, escalate),
            (names.JOB_DRAFT, draft),
        )
    ),
    subscriptions=(
        Subscription(
            name="requests", topics=(names.INVESTIGATION_REQUESTED,), handler=on_requested
        ),
    ),
)
