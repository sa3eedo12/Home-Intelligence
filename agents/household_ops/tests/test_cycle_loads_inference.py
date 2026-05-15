from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tools.cycle_loads import (
    CANDIDATE_LABELS,
    _bucket_for_duration,
    _guess_from_program,
    _habitual_label,
    _infer,
    _keyboard_for,
)


def test_guess_from_program_keyword_match() -> None:
    label, reason = _guess_from_program("Delicates")
    assert label == "delicates"
    assert "delicate" in reason

    label, reason = _guess_from_program("Sports Cycle")
    assert label == "workout"

    label, reason = _guess_from_program("Cotton 60")
    assert label == "colors"


def test_guess_from_program_no_match() -> None:
    label, reason = _guess_from_program(None)
    assert label is None and reason == ""
    label, reason = _guess_from_program("ECO mystery cycle")
    assert label is None


def test_duration_bucket_thresholds() -> None:
    assert _bucket_for_duration(20 * 60)[0] == "quick"
    assert _bucket_for_duration(35 * 60)[0] == "delicates"
    assert _bucket_for_duration(60 * 60)[0] == "colors"
    assert _bucket_for_duration(100 * 60)[0] == "towels"
    assert _bucket_for_duration(150 * 60)[0] == "bedding"
    assert _bucket_for_duration(None)[0] is None


def test_habitual_label_majority_wins() -> None:
    label, reason = _habitual_label(["colors", "colors", "whites"])
    assert label == "colors"
    assert "most common" in reason


def test_habitual_label_no_repeat_means_no_signal() -> None:
    label, _ = _habitual_label(["colors", "whites"])
    assert label is None


def test_infer_program_signal_beats_duration() -> None:
    label, conf, reason = _infer(
        duration_seconds=60 * 60,  # 'colors' bucket
        program="Delicates",
        history=[],
    )
    assert label == "delicates"
    assert conf >= 0.75
    assert "delicate" in reason


def test_infer_duration_when_no_program() -> None:
    label, conf, reason = _infer(
        duration_seconds=20 * 60,
        program=None,
        history=[],
    )
    assert label == "quick"
    assert 0.4 < conf < 0.6


def test_infer_habit_boosts_agreeing_signal() -> None:
    label, conf, _ = _infer(
        duration_seconds=60 * 60,
        program="Cotton",
        history=["colors", "colors", "colors"],
    )
    assert label == "colors"
    assert conf >= 0.85


def test_infer_default_when_no_signals() -> None:
    label, conf, reason = _infer(duration_seconds=None, program=None, history=[])
    assert label == "colors"
    assert conf == 0.2
    assert "default" in reason.lower()


def test_keyboard_has_guess_first_and_skip_button() -> None:
    keyboard = _keyboard_for(42, "towels")
    flat = [btn["callback"] for row in keyboard for btn in row]
    # The guessed label should be the first button overall
    assert keyboard[0][0]["callback"] == "cycle:42:towels"
    assert keyboard[0][0]["text"].startswith("✅ ")
    # _skip is always present
    assert "cycle:42:_skip" in flat
    # Every non-skip callback references a known candidate
    for callback in flat:
        label = callback.rsplit(":", 1)[1]
        assert label in (*CANDIDATE_LABELS, "_skip")


def test_keyboard_caps_button_count() -> None:
    keyboard = _keyboard_for(1, "colors")
    # 6 candidate buttons (across however many rows) + 1 skip row = max 3 rows total
    assert len(keyboard) <= 3
    candidate_count = sum(
        1 for row in keyboard for btn in row if not btn["callback"].endswith(":_skip")
    )
    assert candidate_count == 6


# --- helpers required by datetime parsing tests ---
def test_iso_parse_roundtrips_z_suffix() -> None:
    from tools.cycle_loads import _parse_iso

    dt = _parse_iso("2026-05-13T19:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.astimezone(UTC).hour == 19


def test_iso_parse_handles_datetime_passthrough() -> None:
    from tools.cycle_loads import _parse_iso

    now = datetime(2026, 5, 13, 19, 0, tzinfo=UTC)
    assert _parse_iso(now) == now


def test_coerce_int_accepts_floats_and_strings() -> None:
    from tools.cycle_loads import _coerce_int

    assert _coerce_int(60) == 60
    assert _coerce_int("60") == 60
    assert _coerce_int("60.5") == 60
    assert _coerce_int(-1) is None
    assert _coerce_int("garbage") is None
    assert _coerce_int(None) is None


# ── infer_cycle_load tool: end-to-end against a fake store ────────────────


@pytest.mark.asyncio
async def test_infer_cycle_load_accepts_payload_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct callers (legacy + tests) pass a single ``payload`` dict."""
    from tools.cycle_loads import infer_cycle_load

    captured: dict = {}

    class FakeStore:
        def __init__(self, pool: object) -> None:
            self.pool = pool

        async def confirmed_label_history(
            self, *, appliance: str, limit: int = 15
        ) -> list[str]:
            return []

        async def insert_guess(self, **kwargs: object) -> int:
            captured.update(kwargs)
            return 17

    async def fake_pool() -> object:
        return object()

    monkeypatch.setattr("tools.cycle_loads._pool", fake_pool)
    monkeypatch.setattr("tools.cycle_loads.CycleLoadsStore", FakeStore)

    result = await infer_cycle_load(
        {
            "appliance": "washer",
            "entity_id": "sensor.washer_machine_state",
            "duration_seconds": 2700,
            "program": "Cotton",
            "ended_at": "2026-05-13T19:00:00Z",
        }
    )

    assert result["ok"] is True
    assert result["cycle_load_id"] == 17
    assert captured["appliance"] == "washer"
    assert captured["entity_id"] == "sensor.washer_machine_state"
    assert captured["duration_seconds"] == 2700
    assert captured["program"] == "Cotton"


@pytest.mark.asyncio
async def test_infer_cycle_load_accepts_kwargs_from_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the agent SDK invokes tools as ``fn(**payload)``.

    This is the exact failure mode that caused 0 cycle_loads in production
    despite 4 ``appliance.cycle_completed`` events firing — the tool was
    ``async def infer_cycle_load(payload: dict)`` and blew up with
    ``unexpected keyword argument 'appliance'``. The tool must accept
    fields as kwargs too.
    """
    from tools.cycle_loads import infer_cycle_load

    captured: dict = {}

    class FakeStore:
        def __init__(self, pool: object) -> None:
            self.pool = pool

        async def confirmed_label_history(
            self, *, appliance: str, limit: int = 15
        ) -> list[str]:
            return []

        async def insert_guess(self, **kwargs: object) -> int:
            captured.update(kwargs)
            return 31

    async def fake_pool() -> object:
        return object()

    monkeypatch.setattr("tools.cycle_loads._pool", fake_pool)
    monkeypatch.setattr("tools.cycle_loads.CycleLoadsStore", FakeStore)

    # Mimic agent_base._invoke_tool: kwargs = dict(payload); fn(**kwargs)
    sdk_payload = {
        "appliance": "washer",
        "entity_id": "sensor.washer_machine_state",
        "duration_seconds": 2700,
        "program": "Cotton",
        "ended_at": "2026-05-13T19:00:00Z",
    }
    result = await infer_cycle_load(**sdk_payload)

    assert result["ok"] is True
    assert result["cycle_load_id"] == 31
    assert captured["appliance"] == "washer"
    assert captured["program"] == "Cotton"


@pytest.mark.asyncio
async def test_infer_cycle_load_kwargs_override_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both a payload dict and kwargs are supplied, kwargs win."""
    from tools.cycle_loads import infer_cycle_load

    captured: dict = {}

    class FakeStore:
        def __init__(self, pool: object) -> None:
            self.pool = pool

        async def confirmed_label_history(
            self, *, appliance: str, limit: int = 15
        ) -> list[str]:
            return []

        async def insert_guess(self, **kwargs: object) -> int:
            captured.update(kwargs)
            return 1

    async def fake_pool() -> object:
        return object()

    monkeypatch.setattr("tools.cycle_loads._pool", fake_pool)
    monkeypatch.setattr("tools.cycle_loads.CycleLoadsStore", FakeStore)

    await infer_cycle_load(
        {"appliance": "washer", "program": "Cotton"},
        program="Delicates",
    )
    assert captured["program"] == "Delicates"


# ── Authoritative cycle_name signal ──────────────────────────────────────


def test_cycle_name_is_strongest_signal() -> None:
    """When sensor.washer_cycle reports 'Bedding', the inference should
    take it directly with high confidence — even if duration would
    suggest something else."""
    label, conf, reason = _infer(
        duration_seconds=40 * 60,  # would map to 'delicates'
        program=None,
        history=[],
        cycle_name="Bedding",
    )
    assert label == "bedding"
    assert conf >= 0.9
    assert "cycle name 'Bedding'" in reason


def test_cycle_name_falls_through_to_program_keywords_for_fuzzy_names() -> None:
    """Samsung's 'Bubble Soak Color Care' should still resolve via the
    fuzzy program-keyword map when no direct CANDIDATE_LABEL match."""
    label, conf, reason = _infer(
        duration_seconds=60 * 60,
        program=None,
        history=[],
        cycle_name="Bubble Soak Color Care",
    )
    assert label == "colors"
    assert conf >= 0.9
    assert "cycle name" in reason


def test_no_cycle_name_falls_back_to_program_path() -> None:
    """Unchanged backwards-compat: when cycle_name is None, the program
    + duration logic still works as before."""
    label, conf, reason = _infer(
        duration_seconds=2700,
        program="Cotton",
        history=[],
        cycle_name=None,
    )
    assert label == "colors"
    assert conf >= 0.7
    assert "program 'Cotton'" in reason


def test_cycle_name_unknown_falls_through_to_duration() -> None:
    """A cycle name we can't match shouldn't lock in a wrong label."""
    label, conf, reason = _infer(
        duration_seconds=2700,
        program=None,
        history=[],
        cycle_name="MyCustomMode42",
    )
    # No match → duration takes over
    assert label == "colors"
    assert conf < 0.95


@pytest.mark.asyncio
async def test_infer_cycle_load_persists_cycle_name(monkeypatch) -> None:
    """End-to-end: when the observer envelope carries cycle_name,
    infer_cycle_load uses it AND records it in the persisted row."""
    from tools.cycle_loads import infer_cycle_load

    captured: dict = {}

    class FakeStore:
        def __init__(self, pool: object) -> None:
            self.pool = pool

        async def confirmed_label_history(self, *, appliance: str, limit: int = 15):
            return []

        async def insert_guess(self, **kwargs: object) -> int:
            captured.update(kwargs)
            return 50

    async def fake_pool() -> object:
        return object()

    monkeypatch.setattr("tools.cycle_loads._pool", fake_pool)
    monkeypatch.setattr("tools.cycle_loads.CycleLoadsStore", FakeStore)

    result = await infer_cycle_load(
        appliance="washer",
        entity_id="sensor.washer_machine_state",
        duration_seconds=5400,
        cycle_name="Bedding",
        ended_at="2026-05-15T11:00:00Z",
    )

    assert result["ok"] is True
    assert result["confidence"] >= 0.9
    # Persisted reasoning trail mentions the cycle name
    assert "cycle name" in captured["guessed_reasoning"].lower()
    assert captured["guessed_label"] == "bedding"
