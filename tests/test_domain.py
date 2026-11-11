from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from tower.core.domain import error_codes as ec
from tower.core.domain.dossiers import (
    WORKFLOWS,
    DossierKind,
    DossierStatus,
    check_transition,
    parse_publication_url,
)
from tower.core.domain.errors import InvalidInput, InvalidTransition, PermissionDenied
from tower.core.domain.security import Actor, Permission, Role

ROOT = Path(__file__).resolve().parent.parent
S = DossierStatus


def test_domain_is_pure() -> None:
    """core/domain imports only the standard library and itself."""
    allowed_prefixes = ("tower.core.domain", "__future__")
    stdlib = sys.stdlib_module_names
    for path in (ROOT / "src/tower/core/domain").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.startswith(allowed_prefixes) or name.split(".")[0] in stdlib, (
                    f"{path.name} imports {name}"
                )


@pytest.mark.parametrize("kind", list(DossierKind))
def test_happy_path(kind: DossierKind) -> None:
    for a, b in [(S.RECEIVED, S.IN_REVIEW), (S.IN_REVIEW, S.VALIDATED), (S.VALIDATED, S.PUBLISHED)]:
        check_transition(kind, a, b)
    check_transition(kind, S.IN_REVIEW, S.REJECTED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.RECEIVED, S.PUBLISHED),
        (S.RECEIVED, S.VALIDATED),
        (S.VALIDATED, S.REJECTED),
        (S.PUBLISHED, S.IN_REVIEW),
        (S.REJECTED, S.IN_REVIEW),
    ],
)
def test_forbidden_transitions(current: DossierStatus, target: DossierStatus) -> None:
    with pytest.raises(InvalidTransition) as info:
        check_transition(DossierKind.FACT_CHECK, current, target)
    assert info.value.code == "TWR-DOS-002"


def test_every_status_has_a_workflow_entry() -> None:
    for kind, flow in WORKFLOWS.items():
        assert set(flow) == set(DossierStatus), kind


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/seat12a/status/1850000000000000001",
        "https://twitter.com/seat12a/status/1850000000000000001?s=20",
        "https://mobile.x.com/Seat_12A/status/1850000000000000001/photo/1",
    ],
)
def test_publication_url_ok(url: str) -> None:
    assert parse_publication_url(url) == 1850000000000000001


@pytest.mark.parametrize(
    "url", ["http://x.com/a/status/1", "https://evil.com/a/status/1", "https://x.com/a", ""]
)
def test_publication_url_rejected(url: str) -> None:
    with pytest.raises(InvalidInput) as info:
        parse_publication_url(url)
    assert info.value.code == "TWR-DOS-003"


def test_staff_permissions_are_capped_by_service_scopes() -> None:
    bot = Actor.service("intercom", frozenset({Permission.DOSSIER_READ, Permission.DOSSIER_REVIEW}))
    editor = Actor.staff(42, frozenset({Role.EDITOR}), via=bot)
    assert editor.can(Permission.DOSSIER_REVIEW)
    assert not editor.can(Permission.DOSSIER_PUBLISH)  # the bot does not have this scope
    assert editor.label == "staff:42@intercom"
    assert editor.discord_id == 42
    with pytest.raises(PermissionDenied) as info:
        editor.require(Permission.DOSSIER_PUBLISH)
    assert info.value.code == "TWR-AUTH-005"


def test_anonymous_is_read_public_only() -> None:
    anon = Actor.anonymous()
    assert anon.permissions == frozenset({Permission.DOSSIER_READ_PUBLIC})


def test_error_codes_format_and_uniqueness() -> None:
    import re

    pattern = re.compile(r"^TWR-[A-Z]{2,4}-\d{3}$")
    for code, entry in ec.CATALOG.items():
        assert pattern.match(code), code
        assert entry.code == code
        assert entry.title
        if entry.http_status < 500:
            assert not entry.store or entry.severity.rank >= ec.Severity.WARNING.rank


def test_errors_doc_is_up_to_date() -> None:
    from tower.core.errors.catalog_doc import render_markdown

    doc = (ROOT / "docs/errors.md").read_text()
    assert doc.strip() == render_markdown().strip(), (
        "docs/errors.md is stale: run tower errors catalog --markdown > docs/errors.md"
    )
