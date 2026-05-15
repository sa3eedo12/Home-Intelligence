from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from home_agents_sdk.presence_returns_store import PresenceReturnsStore

from tools import presence_inference
from tools.presence_inference import (
    CANDIDATE_CONTEXTS,
    _history_day_of_week,
    _infer,
    _keyboard_for,
    _summary_for,
)

DXB = ZoneInfo("Asia/Dubai")


def _pool_with(conn: MagicMock) -> MagicMock:
    cm = AsyncMock()
    cm.__aenter__.return_value = conn
    cm.__aexit__.return_value = None
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


def _dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=DXB)


def test_infer_weekday_work_return() -> None:
    context, confidence, reason = _infer(
        away_minutes=9 * 60,
        returned_at=_dt(2026, 5, 13, 17, 30),
        history=[],
    )

    assert context == "work"
    assert confidence >= 0.75
    assert "workday" in reason


def test_infer_evening_social_return() -> None:
    context, confidence, reason = _infer(
        away_minutes=3 * 60,
        returned_at=_dt(2026, 5, 14, 21),
        history=[],
    )

    assert context == "social"
    assert confidence >= 0.6
    assert "evening" in reason


def test_infer_weekday_afternoon_school_return() -> None:
    context, confidence, _ = _infer(
        away_minutes=6 * 60,
        returned_at=_dt(2026, 5, 13, 15),
        history=[],
    )

    assert context == "school"
    assert confidence >= 0.5


def test_infer_weekend_short_absence_defaults_to_gym() -> None:
    context, confidence, reason = _infer(
        away_minutes=2 * 60,
        returned_at=_dt(2026, 5, 16, 10),
        history=[],
    )

    assert context == "gym"
    assert confidence >= 0.5
    assert "weekend" in reason


def test_habit_history_disambiguates_short_weekday_absence() -> None:
    returned_at = _dt(2026, 5, 12, 18)
    day_of_week = _history_day_of_week(returned_at)
    history = [(18, day_of_week, "gym"), (17, day_of_week, "gym"), (18, day_of_week, "gym")]

    context, confidence, reason = _infer(
        away_minutes=90,
        returned_at=returned_at,
        history=history,
    )

    assert context == "gym"
    assert confidence >= 0.7
    assert "recent confirmations" in reason


def test_keyboard_has_presence_callbacks_and_skip() -> None:
    keyboard = _keyboard_for(42, "work")
    flat = [btn["callback"] for row in keyboard for btn in row]

    assert keyboard[0][0] == {"text": "✅ work", "callback": "presence:42:work"}
    assert "presence:42:_skip" in flat
    for callback in flat:
        context = callback.rsplit(":", 1)[1]
        assert context in (*CANDIDATE_CONTEXTS, "_skip")


def test_summary_matches_welcome_home_shape() -> None:
    summary = _summary_for("Saeed", "work", 9 * 60, _dt(2026, 5, 13, 17, 30))

    assert summary == "👋 Welcome home, Saeed. Coming back from work? (gone 9h, weekday afternoon)"


@pytest.mark.asyncio
async def test_infer_presence_return_short_circuits_non_home_state() -> None:
    result = await presence_inference.infer_presence_return(
        entity_id="device_tracker.saeed_phone",
        person="Saeed",
        state="not_home",
    )

    assert result == {"ok": True, "ignored": True, "summary": "", "keyboard": []}


class _FakePresenceStore:
    def __init__(self, left_at: datetime) -> None:
        self.left_at = left_at
        self.inserted: dict | None = None

    async def last_left_at(self, entity_id: str) -> datetime:
        assert entity_id == "device_tracker.saeed_phone"
        return self.left_at

    async def confirmed_context_history(
        self,
        person: str | None,
        limit_days: int = 30,
    ) -> list[tuple[int, int, str]]:
        assert person == "Saeed"
        assert limit_days == 30
        return []

    async def insert_return(self, **kwargs) -> int:
        self.inserted = kwargs
        return 123


@pytest.mark.asyncio
async def test_infer_presence_return_persists_and_returns_keyboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    returned_at = _dt(2026, 5, 13, 17, 30)
    fake_store = _FakePresenceStore(returned_at - timedelta(hours=9))

    async def fake_pool() -> object:
        return object()

    monkeypatch.setattr(presence_inference, "_pool", fake_pool)
    monkeypatch.setattr(presence_inference, "PresenceReturnsStore", lambda pool: fake_store)

    result = await presence_inference.infer_presence_return(
        entity_id="device_tracker.saeed_phone",
        person="Saeed",
        state="home",
        since=returned_at.isoformat(),
    )

    assert result["context"] == "work"
    assert result["presence_return_id"] == 123
    assert result["keyboard"][0][0]["callback"] == "presence:123:work"
    assert fake_store.inserted is not None
    assert fake_store.inserted["away_minutes"] == 9 * 60
    assert fake_store.inserted["guessed_context"] == "work"


@pytest.mark.asyncio
async def test_presence_returns_store_insert_confirm_and_history() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": 44},
            {"id": 44, "person": "Saeed", "confirmed_context": "work"},
        ]
    )
    conn.fetch = AsyncMock(
        return_value=[{"hour_of_day": 17, "day_of_week": 3, "confirmed_context": "work"}]
    )
    store = PresenceReturnsStore(_pool_with(conn))

    inserted_id = await store.insert_return(
        entity_id="device_tracker.saeed_phone",
        person="Saeed",
        left_at=_dt(2026, 5, 13, 8, 30).astimezone(UTC),
        returned_at=_dt(2026, 5, 13, 17, 30).astimezone(UTC),
        away_minutes=9 * 60,
        guessed_context="work",
        guessed_confidence=0.78,
        guessed_reasoning="weekday workday",
    )
    confirmed = await store.confirm(44, "work", 111)
    history = await store.confirmed_context_history("Saeed", limit_days=30)

    assert inserted_id == 44
    assert confirmed == {"id": 44, "person": "Saeed", "confirmed_context": "work"}
    assert history == [(17, 3, "work")]
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_presence_returns_store_last_left_at_prefers_observer_since() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "since": "2026-05-13T08:30:00Z",
            "ts": datetime(2026, 5, 13, 8, 31, tzinfo=UTC),
        }
    )
    store = PresenceReturnsStore(_pool_with(conn))

    left_at = await store.last_left_at("device_tracker.saeed_phone")

    assert left_at == datetime(2026, 5, 13, 8, 30, tzinfo=UTC)
    query, entity_id = conn.fetchrow.await_args.args
    assert "presence.changed" in query
    assert entity_id == "device_tracker.saeed_phone"


# ── _person_from + welcome message hardening ─────────────────────────────


def test_person_from_collapses_consecutive_duplicate_tokens() -> None:
    """REGRESSION: 'Welcome home, SAEED-PC SAEED-PC' was reaching Telegram
    because the observer's friendly_name carried duplicated tokens."""
    from tools.presence_inference import _person_from

    assert _person_from(None, "SAEED-PC SAEED-PC") is None  # rejected as device
    assert _person_from(None, "Saeed Saeed") == "Saeed"
    assert _person_from(None, "Jude Jude Jude") == "Jude"


def test_person_from_rejects_device_names() -> None:
    """PCs/laptops/desktops should not be welcomed home as people."""
    from tools.presence_inference import _person_from

    assert _person_from("device_tracker.saeed_pc", None) is None
    assert _person_from(None, "Saeeds-Laptop") is None
    assert _person_from(None, "Office iMac") is None
    assert _person_from(None, "Workstation") is None


def test_person_from_keeps_human_names() -> None:
    from tools.presence_inference import _person_from

    assert _person_from("person.saeed", None) == "Saeed"
    assert _person_from(None, "Saeed") == "Saeed"
    assert _person_from(None, "Jude Smith") == "Jude Smith"


@pytest.mark.asyncio
async def test_infer_presence_return_skips_for_device_name(monkeypatch) -> None:
    """Even if a device tracker bypasses the observer's authority gate
    and reaches infer_presence_return, the welcome should NOT fire."""
    from tools import presence_inference

    async def _pool() -> object:
        return object()

    monkeypatch.setattr(presence_inference, "_pool", _pool)
    monkeypatch.setattr(
        presence_inference,
        "PresenceReturnsStore",
        lambda _pool: _FakePresenceReturnsStore(),
    )

    result = await presence_inference.infer_presence_return(
        entity_id="device_tracker.saeed_pc",
        person="SAEED-PC SAEED-PC",
        state="home",
        since="2026-05-14T18:12:00+00:00",
    )

    assert result["ok"] is True
    assert result["ignored"] is True
    assert result["reason"] == "non_person_entity"


@pytest.mark.asyncio
async def test_infer_presence_return_caps_implausibly_long_away_time(
    monkeypatch,
) -> None:
    """REGRESSION: after orchestrator restart, last_left_at could pull
    a stale not_home from days ago, producing 'gone 24h' when the user
    was actually only out for 10min. Cap and reset to None above 18h."""
    from datetime import datetime as _dt

    from tools import presence_inference

    class _StaleStore:
        async def last_left_at(self, _entity_id):
            return _dt(2026, 5, 13, 14, 0, tzinfo=UTC)  # ~24h ago

        async def confirmed_context_history(self, *_args, **_kwargs):
            return []

        async def insert_return(self, **_kwargs):
            return 99

    async def _pool() -> object:
        return object()

    monkeypatch.setattr(presence_inference, "_pool", _pool)
    monkeypatch.setattr(presence_inference, "PresenceReturnsStore", lambda _pool: _StaleStore())

    result = await presence_inference.infer_presence_return(
        entity_id="device_tracker.saeeds_iphone",
        person="Saeed",
        state="home",
        since="2026-05-14T14:12:00+00:00",
    )

    # Welcome still fires (real phone, real person), but 'gone Xh' is gone
    assert result["ok"] is True
    assert result.get("ignored") is not True
    summary = result["summary"]
    # Should NOT contain a "gone NNh" hint after the cap kicked in
    assert "gone" not in summary.casefold()


class _FakePresenceReturnsStore:
    async def last_left_at(self, _entity_id):
        return None

    async def confirmed_context_history(self, *_args, **_kwargs):
        return []

    async def insert_return(self, **_kwargs):
        return 1
