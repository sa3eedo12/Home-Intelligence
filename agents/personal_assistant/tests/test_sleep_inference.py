from __future__ import annotations

from datetime import UTC, date, datetime, time
from unittest.mock import AsyncMock, MagicMock

import pytest
from home_agents_sdk.sleep_summaries_store import SleepSummariesStore

from tools import sleep_inference as sleep


class _Acquire:
    def __init__(self, conn: MagicMock) -> None:
        self.conn = conn

    async def __aenter__(self) -> MagicMock:
        return self.conn

    async def __aexit__(self, *_args) -> None:  # noqa: ANN002
        return None


def _pool_with(conn: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire(conn))
    return pool


def test_quality_classification_uses_duration_deep_sleep_and_restlessness() -> None:
    assert sleep._classify_quality(
        duration_minutes=299,
        deep_sleep_minutes=90,
        interruptions=0,
        observer_delta_minutes=0,
        typical_sleep_delta_minutes=0,
        typical_wake_delta_minutes=0,
        crossed_midnight=True,
    )[0] == "short"
    assert sleep._classify_quality(
        duration_minutes=435,
        deep_sleep_minutes=75,
        interruptions=0,
        observer_delta_minutes=5,
        typical_sleep_delta_minutes=10,
        typical_wake_delta_minutes=10,
        crossed_midnight=True,
    )[0] == "great"
    assert sleep._classify_quality(
        duration_minutes=390,
        deep_sleep_minutes=None,
        interruptions=0,
        observer_delta_minutes=5,
        typical_sleep_delta_minutes=10,
        typical_wake_delta_minutes=10,
        crossed_midnight=True,
    )[0] == "decent"
    assert sleep._classify_quality(
        duration_minutes=460,
        deep_sleep_minutes=70,
        interruptions=3,
        observer_delta_minutes=5,
        typical_sleep_delta_minutes=10,
        typical_wake_delta_minutes=10,
        crossed_midnight=True,
    )[0] == "restless"


def test_keyboard_layout_puts_guess_first_and_uses_sleep_callbacks() -> None:
    keyboard = sleep._keyboard_for(42, "decent")

    assert keyboard[0][0] == {"text": "✅ Decent", "callback": "sleep:42:decent"}
    callbacks = [button["callback"] for row in keyboard for button in row]
    assert "sleep:42:great" in callbacks
    assert "sleep:42:restless" in callbacks
    assert callbacks[-1] == "sleep:42:_skip"


@pytest.mark.asyncio
async def test_infer_sleep_summary_persists_health_and_observer_summary(monkeypatch) -> None:
    monkeypatch.setenv("USER_TZ", "UTC")
    pool = MagicMock()

    async def _fake_pool() -> MagicMock:
        return pool

    async def _fake_member(_pool, member_id=None):  # noqa: ANN001
        assert member_id is None
        return {"id": 7, "sleep_time": time(23, 0), "wake_time": time(7, 0)}

    async def _fake_observer_events(_pool, _start, _end):  # noqa: ANN001
        return [
            {
                "capability": "sleep.likely_asleep",
                "ts": datetime(2026, 5, 12, 22, 58, tzinfo=UTC),
                "payload": {"detected_at": "2026-05-12T22:58:00+00:00"},
            },
            {
                "capability": "sleep.likely_awake",
                "ts": datetime(2026, 5, 13, 7, 12, tzinfo=UTC),
                "payload": {"detected_at": "2026-05-13T07:12:00+00:00"},
            },
        ]

    rows_by_metric = {
        "sleep_core": [
            {
                "metric": "sleep_core",
                "started_at": "2026-05-12T23:00:00+00:00",
                "ended_at": "2026-05-13T06:00:00+00:00",
                "value": 420,
                "member_id": 7,
            }
        ],
        "sleep_deep": [
            {
                "metric": "sleep_deep",
                "started_at": "2026-05-13T06:00:00+00:00",
                "ended_at": "2026-05-13T07:10:00+00:00",
                "value": 70,
                "member_id": 7,
            }
        ],
    }

    class _FakeHealthStore:
        def __init__(self, _pool) -> None:  # noqa: ANN001
            pass

        async def list_recent(self, metric: str | None = None, hours: int = 24):
            assert hours >= 36
            return rows_by_metric.get(metric or "", [])

    stores = []

    class _FakeSummaryStore:
        def __init__(self, _pool) -> None:  # noqa: ANN001
            self.inserted = None
            stores.append(self)

        async def insert_summary(self, **kwargs):  # noqa: ANN003
            self.inserted = kwargs
            return 88

    monkeypatch.setattr(sleep, "_pool", _fake_pool)
    monkeypatch.setattr(sleep, "_member", _fake_member)
    monkeypatch.setattr(sleep, "_observer_events", _fake_observer_events)
    monkeypatch.setattr(sleep, "HealthStore", _FakeHealthStore)
    monkeypatch.setattr(sleep, "SleepSummariesStore", _FakeSummaryStore)

    result = await sleep.infer_sleep_summary(night_of="2026-05-12")

    assert result["ok"] is True
    assert result["quality"] == "great"
    assert result["sleep_summary_id"] == 88
    assert "~8h 10m" in result["summary"]
    assert result["keyboard"][0][0]["callback"] == "sleep:88:great"
    inserted = stores[0].inserted
    assert inserted["household_member_id"] == 7
    assert inserted["night_of"] == date(2026, 5, 12)
    assert inserted["duration_minutes"] == 490
    assert inserted["deep_sleep_minutes"] == 70
    assert inserted["observer_likely_awake_at"] == datetime(2026, 5, 13, 7, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_sleep_summaries_store_insert_uses_upsert() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 5})
    store = SleepSummariesStore(_pool_with(conn))

    summary_id = await store.insert_summary(
        household_member_id=7,
        night_of=date(2026, 5, 12),
        asleep_at=datetime(2026, 5, 12, 23, tzinfo=UTC),
        awake_at=datetime(2026, 5, 13, 7, tzinfo=UTC),
        duration_minutes=480,
        deep_sleep_minutes=80,
        observer_likely_asleep_at=None,
        observer_likely_awake_at=None,
        interruptions=0,
        guessed_quality="great",
        guessed_reasoning="test",
    )

    query = conn.fetchrow.await_args.args[0]
    assert summary_id == 5
    assert "ON CONFLICT" in query
    assert "sleep_summaries" in query


# ── Envelope / over-long row defenses (post-Saeed-bug) ──────────────────


def test_interval_covers_children_detects_fajr_envelope() -> None:
    """Parent 02:00→09:00 with two children that cover the same span
    minus a 60-min gap is an envelope. Excluding it leaves the two
    real segments and the gap (Fajr) is correctly NOT counted as sleep.
    """
    parent = (
        datetime(2026, 5, 18, 22, 0, tzinfo=UTC),
        datetime(2026, 5, 19, 5, 0, tzinfo=UTC),
    )
    children = [
        (
            datetime(2026, 5, 18, 22, 0, tzinfo=UTC),
            datetime(2026, 5, 19, 1, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 5, 19, 2, 0, tzinfo=UTC),
            datetime(2026, 5, 19, 5, 0, tzinfo=UTC),
        ),
    ]
    assert sleep._interval_covers_children(parent, children) is True


def test_interval_covers_children_rejects_when_only_one_child() -> None:
    parent = (
        datetime(2026, 5, 18, 22, 0, tzinfo=UTC),
        datetime(2026, 5, 19, 5, 0, tzinfo=UTC),
    )
    children = [
        (
            datetime(2026, 5, 18, 22, 0, tzinfo=UTC),
            datetime(2026, 5, 19, 1, 0, tzinfo=UTC),
        )
    ]
    assert sleep._interval_covers_children(parent, children) is False


def test_interval_covers_children_rejects_contiguous_children() -> None:
    """If children fully tile the parent with no gap, parent is just
    a coarser summary and either keeping or dropping it gives the
    same union — but we err on side of keeping it to preserve metric
    accounting. Returns False because there's no measurable interior
    gap that would indicate a true envelope."""
    parent = (
        datetime(2026, 5, 18, 22, 0, tzinfo=UTC),
        datetime(2026, 5, 19, 5, 0, tzinfo=UTC),
    )
    children = [
        (
            datetime(2026, 5, 18, 22, 0, tzinfo=UTC),
            datetime(2026, 5, 19, 1, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 5, 19, 1, 0, tzinfo=UTC),
            datetime(2026, 5, 19, 5, 0, tzinfo=UTC),
        ),
    ]
    assert sleep._interval_covers_children(parent, children) is False


def test_strip_envelope_rows_drops_implausibly_long_singletons() -> None:
    """The exact Saeed bug: a 23.5h sleep_asleep row that bundles two
    unrelated days. Must be dropped before union math runs."""
    window_start = datetime(2026, 5, 18, 16, 30, tzinfo=UTC)
    window_end = datetime(2026, 5, 19, 9, 0, tzinfo=UTC)
    envelope = {
        "metric": "sleep_asleep",
        "started_at": "2026-05-18T01:16:15+00:00",
        "ended_at": "2026-05-19T00:46:56+00:00",  # 23.5h
        "value": 1410,
        "member_id": 2,
    }
    real = {
        "metric": "sleep_asleep",
        "started_at": "2026-05-18T21:28:29+00:00",  # 01:28 local
        "ended_at": "2026-05-19T00:46:56+00:00",  # 04:46 local
        "value": 198,
        "member_id": 2,
    }
    kept, dropped = sleep._strip_envelope_rows(
        [envelope, real], window_start, window_end
    )
    assert envelope not in kept
    assert real in kept
    assert envelope in dropped


def test_strip_envelope_rows_keeps_normal_night() -> None:
    """A clean 7h night with one row per metric stays untouched."""
    window_start = datetime(2026, 5, 12, 19, 0, tzinfo=UTC)
    window_end = datetime(2026, 5, 13, 11, 0, tzinfo=UTC)
    rows = [
        {
            "metric": "sleep_asleep",
            "started_at": "2026-05-12T23:00:00+00:00",
            "ended_at": "2026-05-13T06:00:00+00:00",
            "value": 420,
        },
        {
            "metric": "sleep_deep",
            "started_at": "2026-05-13T03:00:00+00:00",
            "ended_at": "2026-05-13T04:00:00+00:00",
            "value": 60,
        },
    ]
    kept, dropped = sleep._strip_envelope_rows(rows, window_start, window_end)
    assert kept == rows
    assert dropped == []


def test_default_night_of_for_past_midnight_sleeper_is_today() -> None:
    """Regression: Saeed's sleep_time=00:30. The morning cron runs at
    08:00 local on May 19 — for him, 'last night' = today (May 19),
    because he fell asleep at ~02:30 AM TODAY. Yesterday's date would
    look at the wrong 24h window entirely."""
    member = {"id": 2, "name": "Saeed", "sleep_time": time(0, 30), "wake_time": time(9, 0)}
    today = datetime.now(sleep._tz()).date()
    assert sleep._default_night_of(member) == today


def test_default_night_of_for_pre_midnight_sleeper_is_yesterday() -> None:
    """Classic case: 23:00 sleeper. 'Last night' means yesterday's
    evening to this morning, so night_of = yesterday."""
    from datetime import timedelta as _td

    member = {"id": 9, "name": "Alex", "sleep_time": time(23, 0), "wake_time": time(7, 0)}
    today = datetime.now(sleep._tz()).date()
    assert sleep._default_night_of(member) == today - _td(days=1)


def test_default_night_of_handles_missing_sleep_time() -> None:
    """No member info → fall back to pre-midnight default (23:00) → yesterday."""
    from datetime import timedelta as _td

    today = datetime.now(sleep._tz()).date()
    assert sleep._default_night_of(None) == today - _td(days=1)
    assert sleep._default_night_of({}) == today - _td(days=1)


# ── Bedtime drift detection (closes proposal #53) ───────────────────────


def test_median_bedtime_anchors_across_midnight(monkeypatch) -> None:
    """Naive averaging of 00:30 + 23:30 = 12:00 which is nonsense. The
    anchored median correctly returns 00:00."""
    monkeypatch.setenv("USER_TZ", "UTC")
    samples = [
        datetime(2026, 5, 13, 0, 30, tzinfo=UTC),
        datetime(2026, 5, 14, 23, 30, tzinfo=UTC),
        datetime(2026, 5, 15, 0, 0, tzinfo=UTC),
    ]
    median = sleep._median_bedtime(samples, anchor=time(0, 0))
    assert median is not None
    assert median == time(0, 0)


def test_median_bedtime_for_saeeds_actual_data(monkeypatch) -> None:
    """Real Saeed data: bedtimes 01:25, 02:21, 03:05, 02:27 local. Anchor
    at configured 00:30. Median should be ~02:24."""
    monkeypatch.setenv("USER_TZ", "UTC")
    samples = [
        datetime(2026, 5, 14, 1, 25, tzinfo=UTC),
        datetime(2026, 5, 15, 2, 21, tzinfo=UTC),
        datetime(2026, 5, 16, 3, 5, tzinfo=UTC),
        datetime(2026, 5, 17, 2, 27, tzinfo=UTC),
    ]
    median = sleep._median_bedtime(samples, anchor=time(0, 30))
    assert median is not None
    # Median of the two middle samples (02:21 + 02:27) / 2 = 02:24
    assert median == time(2, 24)


def test_median_bedtime_handles_empty_list() -> None:
    assert sleep._median_bedtime([], anchor=time(0, 30)) is None


@pytest.mark.asyncio
async def test_propose_bedtime_update_emits_when_drift_exceeds_threshold(
    monkeypatch,
) -> None:
    """4 nights of bedtime ~02:25 with configured 00:30 → 115 min drift
    → proposal emitted with the median in title."""
    monkeypatch.setenv("USER_TZ", "UTC")
    member = {
        "id": 2,
        "name": "Saeed",
        "sleep_time": time(0, 30),
        "wake_time": time(9, 0),
    }
    samples = [
        datetime(2026, 5, 14, 1, 25, tzinfo=UTC),
        datetime(2026, 5, 15, 2, 21, tzinfo=UTC),
        datetime(2026, 5, 16, 3, 5, tzinfo=UTC),
        datetime(2026, 5, 17, 2, 27, tzinfo=UTC),
    ]

    async def _fake_bedtimes(_pool, *, member_id, lookback_nights=7):  # noqa: ANN001
        assert member_id == 2
        return samples

    proposals: list[dict] = []

    class _FakeReflectionStore:
        def __init__(self, _pool) -> None:  # noqa: ANN001
            pass

        async def add_proposal(self, **kwargs):  # noqa: ANN003
            proposals.append(kwargs)
            return 999

    class _FakeConn:
        async def fetchval(self, *_a, **_kw):  # noqa: ANN002
            return None

    class _Acquire2:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *_args):  # noqa: ANN002
            return None

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=_Acquire2())

    monkeypatch.setattr(sleep, "_recent_confirmed_or_observed_bedtimes", _fake_bedtimes)
    import home_agents_sdk.reflection_store as rs_module

    monkeypatch.setattr(rs_module, "ReflectionStore", _FakeReflectionStore)

    result = await sleep._propose_bedtime_update_if_drifted(fake_pool, member=member)
    assert result == 999
    assert len(proposals) == 1
    title = proposals[0]["title"]
    assert "02:24" in title
    assert "Saeed" in title
    assert proposals[0]["kind"] == "suggested_action"
    assert proposals[0]["for_member_id"] == 2


@pytest.mark.asyncio
async def test_propose_bedtime_update_silent_when_drift_below_threshold(
    monkeypatch,
) -> None:
    """When observed median is within threshold of configured, no proposal."""
    monkeypatch.setenv("USER_TZ", "UTC")
    member = {
        "id": 2,
        "name": "Saeed",
        "sleep_time": time(2, 0),  # configured close to actual
        "wake_time": time(9, 0),
    }
    samples = [
        datetime(2026, 5, 14, 2, 5, tzinfo=UTC),
        datetime(2026, 5, 15, 1, 50, tzinfo=UTC),
        datetime(2026, 5, 16, 2, 10, tzinfo=UTC),
        datetime(2026, 5, 17, 1, 55, tzinfo=UTC),
    ]

    async def _fake_bedtimes(_pool, *, member_id, lookback_nights=7):  # noqa: ANN001
        return samples

    proposals: list[dict] = []

    class _FakeReflectionStore:
        def __init__(self, _pool) -> None:  # noqa: ANN001
            pass

        async def add_proposal(self, **kwargs):  # noqa: ANN003
            proposals.append(kwargs)
            return 1

    monkeypatch.setattr(sleep, "_recent_confirmed_or_observed_bedtimes", _fake_bedtimes)
    import home_agents_sdk.reflection_store as rs_module

    monkeypatch.setattr(rs_module, "ReflectionStore", _FakeReflectionStore)

    result = await sleep._propose_bedtime_update_if_drifted(MagicMock(), member=member)
    assert result is None
    assert proposals == []


@pytest.mark.asyncio
async def test_propose_bedtime_update_silent_with_too_few_nights(monkeypatch) -> None:
    """Fewer than 4 nights of data → not enough signal to propose."""
    member = {
        "id": 2,
        "name": "Saeed",
        "sleep_time": time(0, 30),
        "wake_time": time(9, 0),
    }

    async def _fake_bedtimes(_pool, *, member_id, lookback_nights=7):  # noqa: ANN001
        return [datetime(2026, 5, 17, 2, 27, tzinfo=UTC)]

    proposals: list[dict] = []

    class _FakeReflectionStore:
        def __init__(self, _pool) -> None:  # noqa: ANN001
            pass

        async def add_proposal(self, **kwargs):  # noqa: ANN003
            proposals.append(kwargs)
            return 1

    monkeypatch.setattr(sleep, "_recent_confirmed_or_observed_bedtimes", _fake_bedtimes)
    import home_agents_sdk.reflection_store as rs_module

    monkeypatch.setattr(rs_module, "ReflectionStore", _FakeReflectionStore)

    result = await sleep._propose_bedtime_update_if_drifted(MagicMock(), member=member)
    assert result is None
    assert proposals == []


@pytest.mark.asyncio
async def test_propose_bedtime_update_dedupes_recent_pending(monkeypatch) -> None:
    """If a pending 'Update bedtime' proposal already exists for the
    member within 14 days, don't emit a new one."""
    monkeypatch.setenv("USER_TZ", "UTC")
    member = {
        "id": 2,
        "name": "Saeed",
        "sleep_time": time(0, 30),
        "wake_time": time(9, 0),
    }
    samples = [
        datetime(2026, 5, 14, 1, 25, tzinfo=UTC),
        datetime(2026, 5, 15, 2, 21, tzinfo=UTC),
        datetime(2026, 5, 16, 3, 5, tzinfo=UTC),
        datetime(2026, 5, 17, 2, 27, tzinfo=UTC),
    ]

    async def _fake_bedtimes(_pool, *, member_id, lookback_nights=7):  # noqa: ANN001
        return samples

    proposals: list[dict] = []

    class _FakeReflectionStore:
        def __init__(self, _pool) -> None:  # noqa: ANN001
            pass

        async def add_proposal(self, **kwargs):  # noqa: ANN003
            proposals.append(kwargs)
            return 1

    class _FakeConn:
        async def fetchval(self, *_a, **_kw):  # noqa: ANN002
            return 1  # an existing proposal exists

    class _Acquire2:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *_args):  # noqa: ANN002
            return None

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=_Acquire2())

    monkeypatch.setattr(sleep, "_recent_confirmed_or_observed_bedtimes", _fake_bedtimes)
    import home_agents_sdk.reflection_store as rs_module

    monkeypatch.setattr(rs_module, "ReflectionStore", _FakeReflectionStore)

    result = await sleep._propose_bedtime_update_if_drifted(fake_pool, member=member)
    assert result is None
    assert proposals == []
