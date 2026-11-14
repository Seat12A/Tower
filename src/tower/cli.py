"""Single entry point: ``tower <command>``.

Imports are done inside each command: ``tower hangar`` loads neither FastAPI
nor uvicorn, which reduces the memory of each process.
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


def cmd_intercom(args: argparse.Namespace) -> None:
    settings = _settings("intercom")
    from tower.core import runtime
    from tower.intercom.app import serve

    runtime.run(lambda stop: serve(settings, stop, dry_run=args.dry_run))


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
    sys.stderr.write("Store this token now: it will never be shown again.\n")


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
            raise SystemExit("Fingerprint required: tower errors resolve <fingerprint>")
        asyncio.run(resolve_error(settings, args.fingerprint, args.note))


def cmd_llm(args: argparse.Namespace) -> None:
    settings = _settings("cli")
    from tower.core.llm.admin import run_budget, run_seed, run_usage

    if args.action == "seed":
        asyncio.run(run_seed(settings))
    elif args.action == "usage":
        asyncio.run(run_usage(settings))
    else:
        if args.scope is None or args.usd is None:
            raise SystemExit("usage: tower llm budget <scope> <usd>")
        asyncio.run(run_budget(settings, args.scope, args.usd))


def cmd_ingest(args: argparse.Namespace) -> None:
    settings = _settings("cli")
    from tower.core.admin import publish_filters_from_file

    asyncio.run(publish_filters_from_file(settings, args.file, args.version))


def cmd_factcheck(args: argparse.Namespace) -> None:
    settings = _settings("cli")
    if args.action == "seed-registry":
        from tower.core.factcheck.registry_seed import run_seed_registry

        asyncio.run(run_seed_registry(settings))
        return
    if args.action == "train-router":
        from tower.core.factcheck.training import run_train

        code = asyncio.run(
            run_train(
                settings,
                days=args.days,
                min_samples=args.min_samples,
                holdout=args.holdout,
                per_stratum_cap=args.per_stratum_cap,
                seed=args.seed,
                dry_run=args.dry_run,
            )
        )
        if code:
            raise SystemExit(code)
        return
    if args.action == "activate-router":
        from tower.core.factcheck.training import run_activate

        code = asyncio.run(
            run_activate(settings, version=args.version, force=args.force, reason=args.reason)
        )
        if code:
            raise SystemExit(code)
        return
    if args.batch:
        from tower.services.investigate.evaluation_batch import run_batch_command

        code = asyncio.run(
            run_batch_command(
                settings,
                set_path=args.set,
                limit=args.limit,
                report=args.report,
                batch_size=args.batch_size,
            )
        )
        if code:
            raise SystemExit(code)
        return
    from tower.services.investigate.evaluation import run_command

    code = asyncio.run(
        run_command(
            settings,
            set_path=args.set,
            limit=args.limit,
            report=args.report,
            level=args.level,
            concurrency=args.concurrency,
            e3_only=args.e3_only,
        )
    )
    if code:
        raise SystemExit(code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tower", description="12A Tower")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("gate", help="Public and internal API (FastAPI)").set_defaults(func=cmd_gate)
    sub.add_parser("hangar", help="Service worker").set_defaults(func=cmd_hangar)
    p = sub.add_parser("intercom", help="Discord bot (gate client)")
    p.add_argument("--dry-run", action="store_true", help="start without connecting to Discord")
    p.set_defaults(func=cmd_intercom)

    p = sub.add_parser("migrate", help="Apply Alembic migrations")
    p.add_argument("revision", nargs="?", default="head")
    p.add_argument("--downgrade", metavar="REV")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("create-service-token", help="Create a service client")
    p.add_argument("name")
    p.add_argument("--scopes", nargs="+", required=True, help="e.g. dossiers:read events:read")
    p.set_defaults(func=cmd_create_service_token)

    p = sub.add_parser("staff", help="Add or update a staff member")
    p.add_argument("discord_id", type=int)
    p.add_argument("--roles", nargs="+", required=True, help="reviewer, editor, admin")
    p.add_argument("--name")
    p.add_argument("--disable", action="store_true")
    p.set_defaults(func=cmd_staff)

    p = sub.add_parser("errors", help="Stored errors and the error code catalog")
    p.add_argument("action", choices=["list", "resolve", "catalog"])
    p.add_argument("fingerprint", nargs="?")
    p.add_argument("--all", action="store_true", help="include resolved errors")
    p.add_argument("--note", help="resolution note")
    p.add_argument("--markdown", action="store_true", help="(catalog) Markdown output")
    p.set_defaults(func=cmd_errors)

    p = sub.add_parser("llm", help="Model router: seed models, routes and budgets; usage")
    p.add_argument("action", choices=["seed", "usage", "budget"])
    p.add_argument("scope", nargs="?", help="(budget) e.g. global, provider:x")
    p.add_argument("usd", nargs="?", type=float, help="(budget) monthly cap in USD")
    p.set_defaults(func=cmd_llm)

    p = sub.add_parser("ingest", help="Ingestion administration")
    isub = p.add_subparsers(dest="action", required=True)
    pf = isub.add_parser("publish-filters", help="Publish and activate extension filters")
    pf.add_argument("file", help="JSON file: {keywords, accounts, thresholds}")
    pf.add_argument("--version", required=True)
    pf.set_defaults(func=cmd_ingest)

    p = sub.add_parser("factcheck", help="Fact-check tools: evaluation and registry seed")
    fsub = p.add_subparsers(dest="action", required=True)
    pe = fsub.add_parser("eval", help="Run an evaluation set (JSONL) through the pipeline")
    pe.add_argument("--set", required=True, metavar="PATH", help="JSONL evaluation set")
    pe.add_argument("--limit", type=int, metavar="N", help="only the first N cases")
    pe.add_argument("--report", metavar="PATH", help="write the Markdown report here")
    pe.add_argument("--level", choices=["N3", "N4"], default="N3")
    pe.add_argument("--concurrency", type=int, default=2, metavar="K")
    pe.add_argument("--e3-only", action="store_true", help="matching layer only (skip E6-E10)")
    pe.add_argument(
        "--batch", action="store_true", help="E2 micro-batch contamination and omission instead"
    )
    pe.add_argument("--batch-size", type=int, default=10, metavar="N", help="(--batch) 1 to 12")
    pe.set_defaults(func=cmd_factcheck)
    ps = fsub.add_parser("seed-registry", help="Seed the source registry")
    ps.set_defaults(func=cmd_factcheck)
    pt = fsub.add_parser("train-router", help="Train the E1 router (inactive model, offline)")
    pt.add_argument("--days", type=int, default=14, metavar="N", help="window of embeddings")
    pt.add_argument("--min-samples", type=int, default=200, metavar="N")
    pt.add_argument("--holdout", type=float, default=0.2, metavar="FRACTION")
    pt.add_argument("--per-stratum-cap", type=int, default=2000, metavar="N")
    pt.add_argument("--seed", type=int, default=0)
    pt.add_argument("--dry-run", action="store_true", help="print the metrics only")
    pt.set_defaults(func=cmd_factcheck)
    pa = fsub.add_parser("activate-router", help="Activate a trained E1 router model")
    pa.add_argument("--version", type=int, required=True)
    pa.add_argument("--force", action="store_true", help="ignore the held-out recall target")
    pa.add_argument("--reason", required=True)
    pa.set_defaults(func=cmd_factcheck)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
