"""Generate the Markdown page of the error catalog (``docs/errors.md``)."""

from __future__ import annotations

from itertools import groupby

from tower.core.domain.error_codes import CATALOG

_DOMAINS = {
    "GEN": "Général",
    "AUTH": "Authentification et autorisations",
    "DOS": "Dossiers",
    "EVT": "Bus d'événements",
    "JOB": "File de travaux",
    "DB": "Base de données",
    "MNT": "Maintenance",
    "INT": "Interne",
}


def render_markdown() -> str:
    lines = [
        "# Catalogue des codes d'erreur",
        "",
        "> Page générée par `tower errors catalog --markdown > docs/errors.md`.",
        "> Ne pas éditer à la main : la source est `src/tower/core/domain/error_codes.py`.",
        "",
        "Format : `TWR-<DOMAINE>-<NNN>`. Un code n'est jamais renuméroté ni réutilisé.",
        "« Stocké » : l'occurrence est agrégée dans la table `error_reports`.",
        "",
    ]
    entries = sorted(CATALOG.values(), key=lambda c: c.code)
    for domain, items in groupby(entries, key=lambda c: c.code.split("-")[1]):
        lines += [
            f"## {domain} : {_DOMAINS.get(domain, domain)}",
            "",
            "| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |",
            "|---|---|---|---|---|---|---|",
        ]
        for c in items:
            title = f"~~{c.title}~~ (obsolète)" if c.deprecated else c.title
            lines.append(
                f"| `{c.code}` | {title} | {c.http_status} | {c.severity.value} | "
                f"{'oui' if c.store else 'non'} | {'oui' if c.retryable else 'non'} | "
                f"{c.hint.replace('|', '/')} |"
            )
        lines.append("")
    return "\n".join(lines)
