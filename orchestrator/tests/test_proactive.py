"""Tests for orchestrator.proactive — the every-15-min "you usually do X
around now" scanner.

Covers the bucketing/slot logic, quiet-hours suppression, dedup window,
empty-state behavior, and the actual proposal that gets emitted.
"""
from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.proactive import (
    _bucket_inferences,
    _is_in_sleep_window,
    _last_local_time_for_slot,
    _slot_for,
    scan_for_opportunities,
)

# ── Pure helpers ─────────────────────────────────────────────────────────


def test_slot_for_buckets_into_60min_windows() -> None:
    dt = datetime(2026, 5, 14, 18, 42, tzinfo=UTC)
    assert _slot_for(dt, 60) == 18


def test_slot_for_buckets_into_15min_windows() -> None:
    dt = datetime(2026, 5, 14, 18, 47, tzinfo=UTC)
    # 18:47 → 1127 minutes / 15 = slot 75
    assert _slot_for(dt, 15) == 75


def test_last_local_time_for_slot_renders_range() -> None:
    assert _last_local_time_for_slot(18, 60) == "18:00–19:00"
    assert _last_local_time_for_slot(0, 60) == "00:00–01:00"
    assert _last_local_time_for_slot(23, 60) == "23:00–00:00"


def test_is_in_sleep_window_handles_after_midnight_bedtime() -> None:
    # Saeed-style: sleep_time=00:30, wake_time=09:00
    sleep, wake = time(0, 30), time(9, 0)
    assert _is_in_sleep_window(time(2, 0), sleep, wake) is True
    assert _is_in_sleep_window(time(8, 59), sleep, wake) is True
    assert _is_in_sleep_window(time(9, 0), sleep, wake) is False
    assert _is_in_sleep_window(time(18, 42), sleep, wake) is False


def test_is_in_sleep_window_handles_midnight_crossing() -> None:
    sleep, wake = time(23, 0), time(7, 0)
    assert _is_in_sleep_window(time(23, 30), sleep, wake) is True
    assert _is_in_sleep_window(time(2, 0), sleep, wake) is True
    assert _is_in_sleep_window(time(7, 0), sleep, wake) is False
    assert _is_in_sleep_window(time(18, 42), sleep, wake) is False


def test_bucket_inferences_groups_by_kind_and_slot() -> None:
    from zoneinfo import ZoneInfo

    rows = [
        {
            "source_kind": "entertainment.left_on",
            "confirmed_at": "2026-05-10T18:30:00+00:00",
            "inference": "left TV on",
        },
        {
            "source_kind": "entertainment.left_on",
            "confirmed_at": "2026-05-13T18:55:00+00:00",
            "inference": "left TV on again",
        },
        {
            "source_kind": "entertainment.left_on",
            "confirmed_at": "2026-05-14T19:01:00+00:00",
            "inference": "different slot",
        },
    ]
    buckets = _bucket_inferences(rows, 60, ZoneInfo("UTC"))
    assert buckets[("entertainment.left_on", 18)] == [rows[0], rows[1]]
    assert buckets[("entertainment.left_on", 19)] == [rows[2]]


def test_bucket_inferences_skips_rows_with_missing_kind_or_ts() -> None:
    from zoneinfo import ZoneInfo

    rows = [
        {"source_kind": "", "confirmed_at": "2026-05-14T12:00:00+00:00"},
        {"source_kind": "x", "confirmed_at": None, "created_at": None},
        {"source_kind": "x", "confirmed_at": "garbage", "created_at": None},
    ]
    assert _bucket_inferences(rows, 60, ZoneInfo("UTC")) == {}


# ── End-to-end scan ──────────────────────────────────────────────────────


def _confirmed_inference(
    *,
    kind: str,
    when: str,
    inference: str = "did the thing",
    source_event_log_id: int | None = None,
) -> dict[str, Any]:
    return {
        "source_kind": kind,
        "confirmed_at": when,
        "created_at": when,
        "inference": inference,
        "source_event_log_id": source_event_log_id,
    }


def _make_stores(
    confirmed: list[dict[str, Any]],
    existing_proposals: list[dict[str, Any]] | None = None,
):
    auto_store = AsyncMock()
    auto_store.recent = AsyncMock(return_value=confirmed)
    reflection_store = AsyncMock()
    reflection_store.list_proposals = AsyncMock(return_value=existing_proposals or [])
    reflection_store.add_proposal = AsyncMock(return_value=42)
    return auto_store, reflection_store


@pytest.mark.asyncio
async def test_scan_emits_proposal_when_pattern_matches_current_slot() -> None:
    """Three confirmed entertainment.left_on around 18:00–19:00 across the
    last week → at 18:42 today the scanner should emit one suggestion."""
    confirmed = [
        _confirmed_inference(
            kind="entertainment.left_on",
            when="2026-05-09T18:30:00+00:00",
            inference="left TV on for 5h past bedtime",
            source_event_log_id=101,
        ),
        _confirmed_inference(
            kind="entertainment.left_on",
            when="2026-05-11T18:50:00+00:00",
            source_event_log_id=102,
        ),
        _confirmed_inference(
            kind="entertainment.left_on",
            when="2026-05-13T18:10:00+00:00",
            source_event_log_id=103,
        ),
    ]
    auto_store, reflection_store = _make_stores(confirmed)

    now = datetime(2026, 5, 14, 18, 42, tzinfo=UTC)
    result = await scan_for_opportunities(
        reflection_store=reflection_store,
        auto_store=auto_store,
        pool=None,
        now=now,
    )

    assert result["emitted"] == 1
    assert result["slot"] == "18:00–19:00"
    reflection_store.add_proposal.assert_awaited_once()
    kwargs = reflection_store.add_proposal.await_args.kwargs
    assert kwargs["kind"] == "proactive_suggestion"
    assert "TV" in kwargs["title"]
    assert "18:00–19:00" in kwargs["title"]
    assert "source_kind=entertainment.left_on" in kwargs["rationale"]
    # Evidence event ids carried through
    assert kwargs["evidence_event_ids"] == [101, 102, 103]


@pytest.mark.asyncio
async def test_scan_does_not_emit_below_min_confirmations() -> None:
    """Two confirmations isn't a pattern — don't propose."""
    confirmed = [
        _confirmed_inference(kind="x.y", when="2026-05-12T18:30:00+00:00"),
        _confirmed_inference(kind="x.y", when="2026-05-13T18:30:00+00:00"),
    ]
    auto_store, reflection_store = _make_stores(confirmed)

    now = datetime(2026, 5, 14, 18, 42, tzinfo=UTC)
    result = await scan_for_opportunities(
        reflection_store=reflection_store,
        auto_store=auto_store,
        pool=None,
        now=now,
    )
    assert result["emitted"] == 0
    assert result["skipped"] == "no_candidates"
    reflection_store.add_proposal.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_skips_when_in_quiet_hours() -> None:
    """During the user's sleep window, the scanner emits nothing — the
    same regression the TV bedtime fix prevented should also gate
    proactive nudges from waking people up."""
    confirmed = [
        _confirmed_inference(kind="x.y", when="2026-05-12T03:00:00+00:00"),
        _confirmed_inference(kind="x.y", when="2026-05-13T03:00:00+00:00"),
        _confirmed_inference(kind="x.y", when="2026-05-14T03:00:00+00:00"),
    ]
    auto_store, reflection_store = _make_stores(confirmed)

    # Fake pool that says one member sleeps 00:30→09:00 — current 03:00 IS asleep.
    class _FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def fetch(self, _query):
            return [{"sleep_time": time(0, 30), "wake_time": time(9, 0)}]

    class _FakePool:
        def acquire(self):
            return _FakeConn()

    now = datetime(2026, 5, 14, 3, 0, tzinfo=UTC)
    result = await scan_for_opportunities(
        reflection_store=reflection_store,
        auto_store=auto_store,
        pool=_FakePool(),
        now=now,
    )

    assert result["emitted"] == 0
    assert result["skipped"] == "quiet_hours"
    reflection_store.add_proposal.assert_not_awaited()
    # Should not even bother reading inferences when muted
    auto_store.recent.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_dedups_against_recent_identical_proposal() -> None:
    """If the same source_kind was proposed within the dedup window,
    don't re-propose. Otherwise the user gets the same nudge every 15min."""
    confirmed = [
        _confirmed_inference(kind="x.y", when="2026-05-12T18:00:00+00:00"),
        _confirmed_inference(kind="x.y", when="2026-05-13T18:00:00+00:00"),
        _confirmed_inference(kind="x.y", when="2026-05-14T18:00:00+00:00"),
    ]
    existing = [
        {
            "kind": "proactive_suggestion",
            "rationale": "Confirmed 3 times. source_kind=x.y; recent_examples=[]",
            "created_at": "2026-05-14T17:30:00+00:00",
        }
    ]
    auto_store, reflection_store = _make_stores(confirmed, existing_proposals=existing)

    now = datetime(2026, 5, 14, 18, 42, tzinfo=UTC)
    result = await scan_for_opportunities(
        reflection_store=reflection_store,
        auto_store=auto_store,
        pool=None,
        now=now,
    )
    # Still found candidates but didn't emit because of dedup
    assert result["emitted"] == 0
    reflection_store.add_proposal.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_emits_at_most_one_per_call() -> None:
    """Two qualifying buckets → only the strongest one ships this scan."""
    confirmed = [
        # 4 confirmations of x.y at 18:00 (winner)
        _confirmed_inference(kind="x.y", when="2026-05-10T18:00:00+00:00"),
        _confirmed_inference(kind="x.y", when="2026-05-11T18:00:00+00:00"),
        _confirmed_inference(kind="x.y", when="2026-05-12T18:00:00+00:00"),
        _confirmed_inference(kind="x.y", when="2026-05-13T18:00:00+00:00"),
        # 3 confirmations of a.b at 18:00 (loses on count)
        _confirmed_inference(kind="a.b", when="2026-05-12T18:00:00+00:00"),
        _confirmed_inference(kind="a.b", when="2026-05-13T18:00:00+00:00"),
        _confirmed_inference(kind="a.b", when="2026-05-14T18:00:00+00:00"),
    ]
    auto_store, reflection_store = _make_stores(confirmed)

    now = datetime(2026, 5, 14, 18, 42, tzinfo=UTC)
    result = await scan_for_opportunities(
        reflection_store=reflection_store,
        auto_store=auto_store,
        pool=None,
        now=now,
    )

    assert result["emitted"] == 1
    # x.y with 4 confirmations should win over a.b with 3
    chosen_rationale = reflection_store.add_proposal.await_args.kwargs["rationale"]
    assert "source_kind=x.y" in chosen_rationale


@pytest.mark.asyncio
async def test_scan_drops_inferences_older_than_lookback(monkeypatch) -> None:
    """A 90-day-old confirmation shouldn't anchor a 'you usually do X' claim."""
    monkeypatch.setenv("PROACTIVE_LOOKBACK_DAYS", "7")
    confirmed = [
        _confirmed_inference(kind="x.y", when="2026-01-12T18:00:00+00:00"),
        _confirmed_inference(kind="x.y", when="2026-01-13T18:00:00+00:00"),
        _confirmed_inference(kind="x.y", when="2026-01-14T18:00:00+00:00"),
    ]
    auto_store, reflection_store = _make_stores(confirmed)

    now = datetime(2026, 5, 14, 18, 42, tzinfo=UTC)
    result = await scan_for_opportunities(
        reflection_store=reflection_store,
        auto_store=auto_store,
        pool=None,
        now=now,
    )

    assert result["emitted"] == 0
    assert result["skipped"] == "no_candidates"


@pytest.mark.asyncio
async def test_scan_survives_store_unavailable() -> None:
    auto_store = AsyncMock()
    auto_store.recent = AsyncMock(side_effect=Exception("connection lost"))
    reflection_store = AsyncMock()

    now = datetime(2026, 5, 14, 18, 42, tzinfo=UTC)
    result = await scan_for_opportunities(
        reflection_store=reflection_store,
        auto_store=auto_store,
        pool=None,
        now=now,
    )

    assert result["emitted"] == 0
    assert result["skipped"] == "store_unavailable"
    reflection_store.add_proposal.assert_not_awaited()
