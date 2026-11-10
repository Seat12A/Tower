"""Error capture, logging and storage.

Usage, anywhere in the code::

    from tower.core.errors import capture
    from tower.core.domain import error_codes as ec

    try:
        ...
    except Exception as exc:
        capture(ec.JOB_EXHAUSTED, exc, job_id=job.id)

``capture`` is synchronous and performs no I/O: it logs immediately (with ``error_code``
and ``fingerprint``, filterable in Railway) then, if the code is storable, aggregates
the occurrence in memory. The process's reporter flushes this buffer to the database
every few seconds, in one transaction.
"""

from tower.core.errors.reporter import ErrorReporter, capture, get_reporter, set_reporter

__all__ = ["ErrorReporter", "capture", "get_reporter", "set_reporter"]
