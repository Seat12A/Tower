"""Translations of the bot (fr, en). Dotted English keys; ``t`` falls back to English."""

from __future__ import annotations

from tower.intercom.i18n import en, fr

__all__ = ["DEFAULT_LOCALE", "has", "locale_of", "t"]

DEFAULT_LOCALE = "en"
_CATALOGS: dict[str, dict[str, str]] = {"en": en.MESSAGES, "fr": fr.MESSAGES}


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
        template = en.MESSAGES.get(key)
    if template is None:
        return key
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template
