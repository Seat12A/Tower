"""Translations of the bot (fr, en). Dotted English keys; ``t`` falls back to English."""

from __future__ import annotations

from collections import ChainMap
from collections.abc import Mapping

from tower.intercom.i18n import (
    badge_en,
    badge_fr,
    en,
    fc_en,
    fc_fr,
    fr,
    ops_en,
    ops_fr,
    team_en,
    team_fr,
)

__all__ = ["CATALOGS", "DEFAULT_LOCALE", "has", "locale_of", "t"]

DEFAULT_LOCALE = "en"
# One base catalogue plus one module per area (ops, fc, team, badge); an area key overrides
# the base one.
# Live views (ChainMap): the first mapping holding a key wins.
CATALOGS: dict[str, Mapping[str, str]] = {
    "en": ChainMap(
        badge_en.MESSAGES, team_en.MESSAGES, fc_en.MESSAGES, ops_en.MESSAGES, en.MESSAGES
    ),
    "fr": ChainMap(
        badge_fr.MESSAGES, team_fr.MESSAGES, fc_fr.MESSAGES, ops_fr.MESSAGES, fr.MESSAGES
    ),
}
_CATALOGS = CATALOGS


def locale_of(value: str | None) -> str:
    """Bot locale of a Discord locale string: ``fr``/``fr-FR`` -> ``fr``, otherwise ``en``."""
    if value:
        primary = value.strip().replace("_", "-").split("-", 1)[0].lower()
        if primary in _CATALOGS:
            return primary
    return DEFAULT_LOCALE


def has(locale: str, key: str) -> bool:
    """Whether ``key`` exists in the catalog of ``locale`` (no fallback)."""
    return key in _CATALOGS.get(locale, {})


def t(locale: str, key: str, **kwargs: object) -> str:
    """Translate ``key`` and format it. Missing key: English, then the key itself."""
    template = _CATALOGS.get(locale, {}).get(key)
    if template is None:
        template = CATALOGS["en"].get(key)
    if template is None:
        return key
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template
