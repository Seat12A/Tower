"""Radar service: reads the watched accounts through the official platform API.

See docs/radar.md. Only ``tower.core`` is imported; the platform client (and httpx) is
loaded lazily by ``client.get_client``.
"""

from __future__ import annotations

from tower.core.services.base import JobSpec, PeriodicTask, Service, Subscription
from tower.services.radar.jobs import on_watchlist_changed, poll_account, resolve_account
from tower.services.radar.schedule import QUEUE, schedule_polls

service = Service(
    name="radar",
    description="Reads watched accounts through the official platform API",
    jobs=(
        JobSpec(kind="radar.poll_account", handler=poll_account, queue=QUEUE, timeout=120),
        JobSpec(kind="radar.resolve_account", handler=resolve_account, queue=QUEUE, timeout=60),
    ),
    subscriptions=(Subscription("watchlist", ("watchlist.changed",), on_watchlist_changed),),
    periodic=(PeriodicTask("radar:schedule", 60, schedule_polls),),
)
