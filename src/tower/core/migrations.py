"""Drives Alembic without alembic.ini: migrations ship inside the package."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from tower.core.config import Settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def alembic_config(settings: Settings) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["settings"] = settings
    return cfg


def upgrade(settings: Settings, revision: str = "head") -> None:
    command.upgrade(alembic_config(settings), revision)


def downgrade(settings: Settings, revision: str) -> None:
    command.downgrade(alembic_config(settings), revision)
