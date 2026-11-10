"""Point d'entrée unique : ``tower <commande>``.

Les imports sont faits dans chaque commande : ``tower hangar`` ne charge ni FastAPI
ni uvicorn, ce qui réduit la mémoire de chaque processus.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence


def _settings(process: str):  # type: ignore[no-untyped-def]
    os.environ.setdefault("TOWER_PROCESS", process)
    from tower.core.config import get_settings
    from tower.core.logging import configure_logging

    settings = get_settings()
    configure_logging(settings)
    return settings


def cmd_gate(_: argparse.Namespace) -> None:
    settings = _settings("gate")
    from tower.gate.server import serve

    serve(settings)


def cmd_hangar(_: argparse.Namespace) -> None:
    settings = _settings("hangar")
    from tower.core import runtime
    from tower.hangar.main import serve

    runtime.run(lambda stop: serve(settings, stop))


def cmd_migrate(args: argparse.Namespace) -> None:
    settings = _settings("migrate")
    from tower.core.migrations import downgrade, upgrade

    if args.downgrade:
        downgrade(settings, args.downgrade)
    else:
        upgrade(settings, args.revision)


def cmd_create_service_token(args: argparse.Namespace) -> None:
    settings = _settings("cli")
    from tower.core.admin import create_service_client

    token = asyncio.run(create_service_client(settings, args.name, args.scopes))
    sys.stdout.write(f"{token}\n")
    sys.stderr.write("Conservez ce token : il ne sera plus jamais affiché.\n")


def cmd_staff(args: argparse.Namespace) -> None:
    settings = _settings("cli")
    from tower.core.admin import upsert_staff

    asyncio.run(upsert_staff(settings, args.discord_id, args.roles, args.name, not args.disable))


def cmd_errors(args: argparse.Namespace) -> None:
    if args.action == "catalog":
        from tower.core.errors.catalog_doc import render_markdown

        sys.stdout.write(render_markdown() + "\n")
        return
    settings = _settings("cli")
    from tower.core.admin import list_errors, resolve_error

    if args.action == "list":
        asyncio.run(list_errors(settings, include_resolved=args.all))
    else:
        if not args.fingerprint:
            raise SystemExit("Empreinte requise : tower errors resolve <empreinte>")
        asyncio.run(resolve_error(settings, args.fingerprint, args.note))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tower", description="12A Tower")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("gate", help="API publique et interne (FastAPI)").set_defaults(func=cmd_gate)
    sub.add_parser("hangar", help="Worker des services").set_defaults(func=cmd_hangar)

    p = sub.add_parser("migrate", help="Applique les migrations Alembic")
    p.add_argument("revision", nargs="?", default="head")
    p.add_argument("--downgrade", metavar="REV")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("create-service-token", help="Crée un client de service")
    p.add_argument("name")
    p.add_argument("--scopes", nargs="+", required=True, help="ex. dossiers:read events:read")
    p.set_defaults(func=cmd_create_service_token)

    p = sub.add_parser("staff", help="Ajoute ou met à jour un membre de l'équipe")
    p.add_argument("discord_id", type=int)
    p.add_argument("--roles", nargs="+", required=True, help="reviewer, editor, admin")
    p.add_argument("--name")
    p.add_argument("--disable", action="store_true")
    p.set_defaults(func=cmd_staff)

    p = sub.add_parser("errors", help="Erreurs stockées et catalogue des codes")
    p.add_argument("action", choices=["list", "resolve", "catalog"])
    p.add_argument("fingerprint", nargs="?")
    p.add_argument("--all", action="store_true", help="inclure les erreurs résolues")
    p.add_argument("--note", help="note de résolution")
    p.add_argument("--markdown", action="store_true", help="(catalog) sortie Markdown")
    p.set_defaults(func=cmd_errors)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
