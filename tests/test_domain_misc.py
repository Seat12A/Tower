from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, Unauthenticated
from tower.core.domain.llm import (
    BUDGET_THRESHOLDS,
    FreeGroup,
    ModelPrices,
    TokenUsage,
    cache_key,
    can_use_free,
    cost_usd_micros,
    crossed_thresholds,
    estimate_tokens,
    estimate_usage,
    free_allowance,
    month_period,
)
from tower.core.domain.net import is_public_ip
from tower.core.domain.platforms import Platform, parse_platform, validate_external_id
from tower.core.domain.privacy import (
    PSEUDONYM_BYTES,
    installation_pseudonym,
    is_valid_installation_token,
    new_daily_secret,
    validate_installation_token,
)

# --- privacy ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "valid"),
    [
        ("a" * 21, False),
        ("a" * 22, True),
        ("A-_z09" * 10 + "abcd", True),  # 64 chars
        ("a" * 64, True),
        ("a" * 65, False),
        ("a" * 21 + "!", False),
        ("a" * 21 + "=", False),
        ("a" * 22 + "==", False),
        ("a" * 21 + "+", False),
        ("a" * 21 + "/", False),
        ("a" * 21 + " ", False),
        ("a" * 22 + "\n", False),
        ("é" * 22, False),
        ("", False),
    ],
)
def test_token_format(token: str, valid: bool) -> None:
    assert is_valid_installation_token(token) is valid


def test_validate_returns_token() -> None:
    token = "x" * 30
    assert validate_installation_token(token) == token


@pytest.mark.parametrize(
    "bad", [None, "short", "bad token with spaces!!!!!!!!", "z" * 70 + "SECRETMARK"]
)
def test_validate_rejects_without_leaking_token(bad: str | None) -> None:
    with pytest.raises(Unauthenticated) as info:
        validate_installation_token(bad)
    err = info.value
    assert err.code == ec.ING_INVALID_INSTALLATION_TOKEN.code
    if bad:
        assert bad not in err.message
        assert bad not in repr(err.details)
        assert "SECRETMARK" not in str(err) + repr(err.details)


def test_pseudonym() -> None:
    secret = b"s" * 32
    token = "t" * 30
    pseudo = installation_pseudonym(secret, token)
    assert len(pseudo) == PSEUDONYM_BYTES == 16
    assert pseudo == installation_pseudonym(secret, token)
    assert pseudo != installation_pseudonym(b"u" * 32, token)
    assert pseudo != installation_pseudonym(secret, "u" * 30)


def test_pseudonym_short_secret() -> None:
    with pytest.raises(ValueError, match="Secret"):
        installation_pseudonym(b"x" * 15, "t" * 30)
    assert len(installation_pseudonym(b"x" * 16, "t" * 30)) == 16


def test_new_daily_secret() -> None:
    a, b = new_daily_secret(), new_daily_secret()
    assert len(a) == 32
    assert a != b


# --- platforms -------------------------------------------------------------------------


@pytest.mark.parametrize("platform", list(Platform))
def test_parse_every_platform(platform: Platform) -> None:
    assert parse_platform(platform.value) is platform


@pytest.mark.parametrize("value", ["", "X", "twitter", "facebook", " x"])
def test_parse_unknown_platform(value: str) -> None:
    with pytest.raises(InvalidInput) as info:
        parse_platform(value)
    assert info.value.code == ec.ING_UNKNOWN_PLATFORM.code


@pytest.mark.parametrize("value", ["1", "1234567890123456789", "9223372036854775807", "007"])
def test_x_ids_valid(value: str) -> None:
    assert validate_external_id(Platform.X, value) == value
    assert validate_external_id("x", value) == value


@pytest.mark.parametrize(
    "value",
    [
        "9223372036854775808",
        "12345678901234567890",
        "-1",
        "+1",
        " 12",
        "12 ",
        "12a",
        "",
        "١٢٣",
        "1.5",
    ],
)
def test_x_ids_invalid(value: str) -> None:
    with pytest.raises(InvalidInput) as info:
        validate_external_id(Platform.X, value)
    assert info.value.code == ec.ING_BAD_EXTERNAL_ID.code


def test_other_platform_ids() -> None:
    uri = "at://did:plc:abc/app.bsky.feed.post/3k"
    assert validate_external_id(Platform.BLUESKY, uri) == uri
    assert validate_external_id(Platform.WEB, "a" * 256) == "a" * 256
    assert validate_external_id("telegram", "chat 42") == "chat 42"


@pytest.mark.parametrize(
    "value", ["a" * 257, "", "ab\x00c", "ab\ncd", "ab\tcd", " lead", "trail ", " x"]
)
def test_other_platform_ids_invalid(value: str) -> None:
    with pytest.raises(InvalidInput) as info:
        validate_external_id(Platform.BLUESKY, value)
    assert info.value.code == ec.ING_BAD_EXTERNAL_ID.code
    assert info.value.details["platform"] == "bluesky"
    assert len(str(info.value.details.get("value") or "")) <= 64


def test_validate_unknown_platform() -> None:
    with pytest.raises(InvalidInput) as info:
        validate_external_id("nope", "1")
    assert info.value.code == ec.ING_UNKNOWN_PLATFORM.code


# --- llm -------------------------------------------------------------------------------


def _key(**overrides: object) -> str:
    args: dict[str, object] = {
        "role": "triage",
        "provider": "openai",
        "model": "m1",
        "prompt_version": "v1",
        "params": {"temperature": 0, "max": 10},
        "input": {"text": "héllo", "n": 1},
    }
    args.update(overrides)
    return cache_key(**args)  # type: ignore[arg-type]


def test_cache_key_stable_and_order_independent() -> None:
    base = _key()
    assert len(base) == 64
    assert base == _key()
    assert base == _key(params={"max": 10, "temperature": 0}, input={"n": 1, "text": "héllo"})


@pytest.mark.parametrize(
    "change",
    [
        {"role": "other"},
        {"provider": "anthropic"},
        {"model": "m2"},
        {"prompt_version": "v2"},
        {"params": {"temperature": 1, "max": 10}},
        {"input": {"text": "hello", "n": 1}},
    ],
)
def test_cache_key_changes_with_each_argument(change: dict[str, object]) -> None:
    assert _key(**change) != _key()


def test_cache_key_distinguishes_field_boundaries() -> None:
    assert _key(role="ab", provider="c") != _key(role="a", provider="bc")


def test_cache_key_rejects_non_serialisable() -> None:
    with pytest.raises(TypeError):
        _key(input={"x": object()})


def test_estimate_usage() -> None:
    usage = estimate_usage(300, 100)
    assert usage == TokenUsage(input_tokens=150, cached_input_tokens=0, output_tokens=100)
    assert estimate_usage(301, 0).input_tokens == 101 + 50
    assert estimate_usage(0, 5).input_tokens == 50
    assert estimate_tokens(300, 100) == 250


PRICES = ModelPrices(input=Decimal("2.50"), cached_input=Decimal("0.25"), output=Decimal("10"))


def test_cost_exact() -> None:
    usage = TokenUsage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=100_000)
    assert cost_usd_micros(usage, PRICES) == 2_500_000 + 1_000_000


def test_cost_cached_tokens_priced_separately() -> None:
    usage = TokenUsage(input_tokens=1000, cached_input_tokens=400, output_tokens=10)
    # 600 * 2.5 + 400 * 0.25 + 10 * 10 = 1500 + 100 + 100
    assert cost_usd_micros(usage, PRICES) == 1700


def test_cost_rounds_up() -> None:
    prices = ModelPrices(Decimal("0.15"), Decimal("0.075"), Decimal("0.6"))
    assert cost_usd_micros(TokenUsage(1, 0, 0), prices) == 1  # 0.15 -> 1
    assert cost_usd_micros(TokenUsage(7, 0, 0), prices) == 2  # 1.05 -> 2
    assert cost_usd_micros(TokenUsage(0, 0, 0), prices) == 0
    assert cost_usd_micros(TokenUsage(20, 0, 0), prices) == 3  # exactly 3.0


@pytest.mark.parametrize(
    ("tier", "small", "large"),
    [
        (1, 2_500_000, 250_000),
        (2, 2_500_000, 250_000),
        (3, 10_000_000, 1_000_000),
        (4, 10_000_000, 1_000_000),
        (5, 10_000_000, 1_000_000),
    ],
)
def test_free_allowance(tier: int, small: int, large: int) -> None:
    assert free_allowance(tier, FreeGroup.SMALL) == small
    assert free_allowance(tier, FreeGroup.LARGE) == large


@pytest.mark.parametrize("tier", [0, 6, -1])
def test_free_allowance_bad_tier(tier: int) -> None:
    with pytest.raises(ValueError, match="tier"):
        free_allowance(tier, FreeGroup.SMALL)


def test_can_use_free_boundary() -> None:
    assert can_use_free(0, 1000, 1100) is True  # exactly at the margin
    assert can_use_free(0, 1000, 1099) is False
    assert can_use_free(500, 1000, 1600) is True
    assert can_use_free(500, 1000, 1599) is False
    assert can_use_free(0, 1000, 1000, margin=0.0) is True
    assert can_use_free(0, 0, 0) is True
    assert can_use_free(100, 10, 50) is False


def test_crossed_thresholds() -> None:
    assert BUDGET_THRESHOLDS == (50, 80, 100)
    assert crossed_thresholds(0, 49, 100) == []
    assert crossed_thresholds(0, 50, 100) == [50]  # exactly at the threshold
    assert crossed_thresholds(50, 79, 100) == []  # already crossed before
    assert crossed_thresholds(49, 85, 100) == [50, 80]
    assert crossed_thresholds(0, 500, 100) == [50, 80, 100]
    assert crossed_thresholds(79, 100, 100) == [80, 100]
    assert crossed_thresholds(100, 200, 100) == []
    assert crossed_thresholds(10, 10, 100) == []


def test_crossed_thresholds_cap_zero_or_negative() -> None:
    assert crossed_thresholds(0, 100, 0) == []
    assert crossed_thresholds(0, 100, -5) == []


def test_month_period() -> None:
    assert month_period(datetime(2026, 3, 15, 12, tzinfo=UTC)) == (
        date(2026, 3, 1),
        date(2026, 4, 1),
    )
    assert month_period(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == (
        date(2026, 12, 1),
        date(2027, 1, 1),
    )
    assert month_period(datetime(2026, 1, 1, tzinfo=UTC)) == (date(2026, 1, 1), date(2026, 2, 1))


def test_month_period_uses_utc() -> None:
    late = datetime(2026, 3, 31, 23, 30, tzinfo=timezone(timedelta(hours=-5)))  # April 1st UTC
    assert month_period(late) == (date(2026, 4, 1), date(2026, 5, 1))
    assert month_period(datetime(2026, 5, 10)) == (date(2026, 5, 1), date(2026, 6, 1))


# --- net -------------------------------------------------------------------------------


@pytest.mark.parametrize("ip", ["8.8.8.8", "2606:4700::1111", "::ffff:8.8.8.8", "1.1.1.1"])
def test_public_ips(ip: str) -> None:
    assert is_public_ip(ip) is True


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.1.1",
        "100.64.0.1",
        "0.0.0.0",  # noqa: S104
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "not an ip",
        "",
        "999.1.1.1",
    ],
)
def test_non_public_ips(ip: str) -> None:
    assert is_public_ip(ip) is False
