"""hangar: generic worker that runs the pluggable services.

One replica = one asyncio loop, one LISTEN connection, a small SQL pool. Scale
by adding replicas (same queue) or dedicated Railway services (``TOWER_QUEUES``).
"""
