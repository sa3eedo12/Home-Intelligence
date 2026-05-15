"""Tests for orchestrator.profile_parser."""
from __future__ import annotations

from datetime import time

from orchestrator.profile_parser import (
    parse_dietary_restrictions,
    parse_profile_value,
    parse_wake_or_sleep_time,
    parse_work_hours,
)

# ── parse_wake_or_sleep_time ────────────────────────────────────────────


def test_simple_time_returns_same_for_both_buckets() -> None:
    out = parse_wake_or_sleep_time("9 AM")
    assert out == {"weekday": time(9, 0), "weekend": time(9, 0)}


def test_weekday_weekend_variants_split() -> None:
    """REGRESSION: this is the user's actual answer. Used to lose the
    weekend variant entirely."""
    out = parse_wake_or_sleep_time(
        "I wake up no later than 9:00 AM on weekdays, 11:00 AM weekends"
    )
    assert out is not None
    assert out["weekday"] == time(9, 0)
    assert out["weekend"] == time(11, 0)


def test_weekend_only_specified_falls_back_to_default() -> None:
    out = parse_wake_or_sleep_time("I sleep at 11 PM, weekends a bit later")
    assert out is not None
    assert out["weekday"] == time(23, 0)


def test_pm_correctly_offset() -> None:
    out = parse_wake_or_sleep_time("11:30 PM")
    assert out == {"weekday": time(23, 30), "weekend": time(23, 30)}


def test_returns_none_for_unparseable() -> None:
    assert parse_wake_or_sleep_time("dunno really") is None
    assert parse_wake_or_sleep_time("") is None
    assert parse_wake_or_sleep_time(None) is None  # type: ignore[arg-type]


def test_around_keyword_promotes_bare_integer() -> None:
    out = parse_wake_or_sleep_time("I wake up around 7")
    assert out is not None
    assert out["weekday"] == time(7, 0)


# ── parse_dietary_restrictions ──────────────────────────────────────────


def test_halal_with_avoid() -> None:
    """REGRESSION: actual user answer that until now sat in DB unused."""
    out = parse_dietary_restrictions(
        "I only eat Halal food, not a big fan of seafood"
    )
    assert out is not None
    assert out.get("halal") is True
    assert "seafood" in (out.get("avoid") or [])


def test_vegetarian_with_multiple_avoids() -> None:
    out = parse_dietary_restrictions("Vegetarian, no nuts and no dairy")
    assert out is not None
    assert out.get("vegetarian") is True
    assert "nuts" in out.get("avoid", [])
    assert "dairy" in out.get("avoid", [])


def test_only_avoidance_returns_avoid_list() -> None:
    out = parse_dietary_restrictions("I avoid dairy and gluten")
    assert out is not None
    assert "dairy" in (out.get("avoid") or [])
    assert "gluten" in (out.get("avoid") or [])
    assert "halal" not in out


def test_returns_none_when_no_signals() -> None:
    assert parse_dietary_restrictions("I eat anything") is None
    assert parse_dietary_restrictions("") is None


# ── parse_work_hours ────────────────────────────────────────────────────


def test_work_hours_with_meridiem_and_remote() -> None:
    """REGRESSION: actual user answer."""
    out = parse_work_hours(
        "9:00 AM to 6:00 PM officially, I work remotely at the moment"
    )
    assert out is not None
    assert out.get("start") == time(9, 0)
    assert out.get("end") == time(18, 0)
    assert out.get("remote") is True


def test_work_hours_dash_range_assumes_pm_for_end() -> None:
    out = parse_work_hours("9-5 weekdays")
    assert out is not None
    assert out.get("start") == time(9, 0)
    assert out.get("end") == time(17, 0)


def test_work_hours_24h_format() -> None:
    out = parse_work_hours("9:00 to 17:00")
    assert out is not None
    assert out.get("end") == time(17, 0)


def test_work_hours_returns_none_for_no_range() -> None:
    assert parse_work_hours("I work whenever") is None


# ── parse_profile_value top-level dispatcher ────────────────────────────


def test_dispatcher_routes_wake_time() -> None:
    out = parse_profile_value(
        "wake_time",
        '"I wake up no later than 9:00 AM on weekdays, 11:00 AM weekends"',
    )
    assert out is not None
    assert "weekday" in out and "weekend" in out


def test_dispatcher_routes_dietary_restrictions() -> None:
    out = parse_profile_value(
        "dietary_restrictions", '"I only eat Halal food, not a fan of seafood"'
    )
    assert out is not None
    assert out.get("halal") is True


def test_dispatcher_unknown_key_returns_none() -> None:
    assert parse_profile_value("favorite_color", "blue") is None


def test_dispatcher_passes_through_dict() -> None:
    """If the value is already structured (jsonb), return as-is."""
    out = parse_profile_value(
        "wake_time", {"weekday": "09:00", "weekend": "11:00"}
    )
    assert out == {"weekday": "09:00", "weekend": "11:00"}
