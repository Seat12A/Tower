from __future__ import annotations

import string

import pytest

from tower.intercom.i18n import en, fr, has, locale_of, t


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def test_catalogs_have_identical_keys() -> None:
    assert set(en.MESSAGES) == set(fr.MESSAGES)


def test_placeholders_are_identical_per_key() -> None:
    mismatched = {
        key: (_fields(en.MESSAGES[key]), _fields(fr.MESSAGES[key]))
        for key in en.MESSAGES
        if _fields(en.MESSAGES[key]) != _fields(fr.MESSAGES[key])
    }
    assert mismatched == {}


def test_error_messages_show_the_code() -> None:
    for catalog in (en.MESSAGES, fr.MESSAGES):
        for key, template in catalog.items():
            if key.startswith("error.TWR-"):
                assert "{code}" in template, key


def test_error_keys_are_in_the_catalog() -> None:
    from tower.core.domain.error_codes import CATALOG

    for key in en.MESSAGES:
        if key.startswith("error.TWR-"):
            assert key.removeprefix("error.") in CATALOG


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("fr", "fr"),
        ("fr-FR", "fr"),
        ("FR", "fr"),
        ("fr_CA", "fr"),
        ("en-US", "en"),
        ("de", "en"),
        ("", "en"),
        (None, "en"),
    ],
)
def test_locale_of(value: str | None, expected: str) -> None:
    assert locale_of(value) == expected


def test_translation_and_fallbacks() -> None:
    assert t("fr", "dossier.taken", short_id="abc") == "Dossier `abc` pris en revue."
    assert t("en", "dossier.taken", short_id="abc") == "Dossier `abc` taken into review."
    assert t("de", "dossier.taken", short_id="abc") == "Dossier `abc` taken into review."
    assert t("fr", "no.such.key") == "no.such.key"
    # a key only present in English falls back to English
    en.MESSAGES["test.only_en"] = "english only"
    try:
        assert t("fr", "test.only_en") == "english only"
        assert not has("fr", "test.only_en")
    finally:
        del en.MESSAGES["test.only_en"]
    # a missing placeholder never raises
    assert "{short_id}" in t("en", "dossier.taken")


def test_french_has_accents() -> None:
    assert "é" in fr.MESSAGES["error.TWR-AUTH-004"]
