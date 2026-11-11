"""Processing profiles (niveaux.md, "Réglages par niveau"): schema, defaults, safety
bounds and version comparison. Pure: standard library only.

A profile is stored as JSON in ``processing_profiles.settings``::

    {
      "levels": {
        "N0": {"e1_threshold": 1.0, "direct_reuse": true,
               "unit_cap_usd": 0.0, "monthly_budget_usd": 1.0},
        "N1": {...}, "N2": {...}, "N3": {...},
        "N4": {"e1_threshold": 0.0, "direct_reuse": false, ...}
      },
      "match_same_threshold": 0.92,
      "match_grey_threshold": 0.80,
      "draft_daily_quota": 50,
      "e1_sample_rate": 0.02
    }
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "LEVELS",
    "LevelSettings",
    "Profile",
    "bounds_violations",
    "diff_profiles",
    "profile_from_json",
    "profile_to_json",
]

LEVELS = ("N0", "N1", "N2", "N3", "N4")

# Safety bounds (checked by the use case before any activation).
MIN_SAME_THRESHOLD = 0.85
MAX_SAME_THRESHOLD = 0.999
MIN_GREY_THRESHOLD = 0.5
MAX_SAMPLE_RATE = 0.2
MIN_SAMPLE_RATE = 0.005
MAX_DRAFT_QUOTA = 500
MAX_UNIT_CAP_USD = 5.0


@dataclass(frozen=True, slots=True)
class LevelSettings:
    e1_threshold: float
    """E1 score below which a content is discarded at this level (0 = everything)."""
    direct_reuse: bool
    """Reuse of a verdict at identical signature or validated band (never in N4)."""
    unit_cap_usd: float
    """Maximum cost of one investigation at this level."""
    monthly_budget_usd: float
    """Monthly cap of the budget scope ``level:<N>``."""


@dataclass(frozen=True, slots=True)
class Profile:
    version: int
    """0 = the default profile built from the settings (no row in the database)."""
    levels: Mapping[str, LevelSettings]
    match_same_threshold: float
    match_grey_threshold: float
    draft_daily_quota: int
    e1_sample_rate: float
    extra: Mapping[str, Any] = field(default_factory=dict)
    """Unknown keys kept as-is (forward compatibility with newer code)."""

    def level(self, level: str) -> LevelSettings:
        return self.levels[level]


_KNOWN = {
    "levels",
    "match_same_threshold",
    "match_grey_threshold",
    "draft_daily_quota",
    "e1_sample_rate",
}


def profile_from_json(version: int, data: Mapping[str, Any]) -> Profile:
    """Parse stored settings. Raises ``ValueError`` (with the field path) on a missing
    or mistyped field; bounds are checked separately by :func:`bounds_violations`."""
    try:
        raw_levels = data["levels"]
        levels = {
            name: LevelSettings(
                e1_threshold=float(raw_levels[name]["e1_threshold"]),
                direct_reuse=bool(raw_levels[name]["direct_reuse"]),
                unit_cap_usd=float(raw_levels[name]["unit_cap_usd"]),
                monthly_budget_usd=float(raw_levels[name]["monthly_budget_usd"]),
            )
            for name in LEVELS
        }
        return Profile(
            version=version,
            levels=levels,
            match_same_threshold=float(data["match_same_threshold"]),
            match_grey_threshold=float(data["match_grey_threshold"]),
            draft_daily_quota=int(data["draft_daily_quota"]),
            e1_sample_rate=float(data["e1_sample_rate"]),
            extra={k: v for k, v in data.items() if k not in _KNOWN},
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid profile settings: {exc!r}") from exc


def profile_to_json(profile: Profile) -> dict[str, Any]:
    out: dict[str, Any] = dict(profile.extra)
    out["levels"] = {name: asdict(s) for name, s in profile.levels.items()}
    out["match_same_threshold"] = profile.match_same_threshold
    out["match_grey_threshold"] = profile.match_grey_threshold
    out["draft_daily_quota"] = profile.draft_daily_quota
    out["e1_sample_rate"] = profile.e1_sample_rate
    return out


def bounds_violations(profile: Profile) -> list[str]:
    """Every safety bound broken by ``profile`` (empty list = acceptable)."""
    errors: list[str] = []
    if not MIN_SAME_THRESHOLD <= profile.match_same_threshold <= MAX_SAME_THRESHOLD:
        errors.append(
            f"match_same_threshold must be in [{MIN_SAME_THRESHOLD}, {MAX_SAME_THRESHOLD}]"
        )
    if not MIN_GREY_THRESHOLD <= profile.match_grey_threshold < profile.match_same_threshold:
        errors.append(
            f"match_grey_threshold must be in [{MIN_GREY_THRESHOLD}, match_same_threshold["
        )
    if not 0 <= profile.draft_daily_quota <= MAX_DRAFT_QUOTA:
        errors.append(f"draft_daily_quota must be in [0, {MAX_DRAFT_QUOTA}]")
    if not MIN_SAMPLE_RATE <= profile.e1_sample_rate <= MAX_SAMPLE_RATE:
        errors.append(f"e1_sample_rate must be in [{MIN_SAMPLE_RATE}, {MAX_SAMPLE_RATE}]")
    for name in LEVELS:
        s = profile.levels.get(name)
        if s is None:
            errors.append(f"levels.{name} is missing")
            continue
        if not 0.0 <= s.e1_threshold <= 1.0:
            errors.append(f"levels.{name}.e1_threshold must be in [0, 1]")
        if s.monthly_budget_usd < 0:
            errors.append(f"levels.{name}.monthly_budget_usd must be >= 0")
        if not 0 <= s.unit_cap_usd <= MAX_UNIT_CAP_USD:
            errors.append(f"levels.{name}.unit_cap_usd must be in [0, {MAX_UNIT_CAP_USD}]")
        if s.unit_cap_usd > s.monthly_budget_usd:
            errors.append(f"levels.{name}.unit_cap_usd must not exceed monthly_budget_usd")
    n4 = profile.levels.get("N4")
    if n4 is not None:
        if n4.direct_reuse:
            errors.append("levels.N4.direct_reuse must be false (sources reloaded and rejudged)")
        if n4.e1_threshold != 0.0:
            errors.append("levels.N4.e1_threshold must be 0 (everything passes)")
    n3 = profile.levels.get("N3")
    n1 = profile.levels.get("N1")
    if n3 is not None and n1 is not None and n3.e1_threshold > n1.e1_threshold:
        errors.append("levels.N3.e1_threshold must not exceed levels.N1.e1_threshold")
    return errors


def diff_profiles(old: Profile, new: Profile) -> dict[str, tuple[Any, Any]]:
    """Flat ``{"path": (old, new)}`` of the changed settings (journal, bot display)."""
    a, b = _flatten(profile_to_json(old)), _flatten(profile_to_json(new))
    return {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}


def _flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            out.update(_flatten(value, f"{path}."))
        else:
            out[path] = value
    return out
