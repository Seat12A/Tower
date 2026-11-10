"""Capture, journalisation et stockage des erreurs.

Usage, partout dans le code ::

    from tower.core.errors import capture
    from tower.core.domain import error_codes as ec

    try:
        ...
    except Exception as exc:
        capture(ec.JOB_EXHAUSTED, exc, job_id=job.id)

``capture`` est synchrone et ne fait aucune I/O : il journalise immédiatement (avec
``error_code`` et ``fingerprint``, filtrables dans Railway) puis, si le code est
stockable, agrège l'occurrence en mémoire. Le reporter du processus vide ce tampon
en base toutes les quelques secondes, en une transaction.
"""

from tower.core.errors.reporter import ErrorReporter, capture, get_reporter, set_reporter

__all__ = ["ErrorReporter", "capture", "get_reporter", "set_reporter"]
