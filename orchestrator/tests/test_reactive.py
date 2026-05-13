from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakeredis.aioredis import FakeRedis

from orchestrator.reactive import Reactive


@pytest.mark.asyncio
async def test_doorbell_ring_dispatches_and_notifies(tmp_path: Path) -> None:
    triggers = tmp_path / "reactive.yaml"
    triggers.write_text(
        """
triggers:
  - id: doorbell_ring
    stream: events.home
    match: { type: doorbell_ring }
    dispatch:
      agent: home_automation
      capability: doorbell.summarize_event
      inputs: { event_type: doorbell_ring }
    notify_from_result:
      text_field: summary
      topic: doorbell.ring
      severity: alert
""",
        encoding="utf-8",
    )

    redis = FakeRedis(decode_responses=True)
    registry = MagicMock()
    registry.dispatch = AsyncMock(return_value={"ok": True, "result": {"summary": "Someone rang"}})

    reactive = Reactive(registry=registry, redis=redis, triggers_path=str(triggers))
    trigger = {
        "id": "doorbell_ring",
        "stream": "events.home",
        "match": {"type": "doorbell_ring"},
        "dispatch": {
            "agent": "home_automation",
            "capability": "doorbell.summarize_event",
            "inputs": {"event_type": "doorbell_ring"},
        },
        "notify_from_result": {
            "text_field": "summary",
            "topic": "doorbell.ring",
            "severity": "alert",
        },
    }

    await reactive.handle_event(trigger, {"type": "doorbell_ring"})

    registry.dispatch.assert_awaited_once()
    rows = await redis.xrange("notify.outbound")
    assert len(rows) == 1
    payload = json.loads(rows[0][1]["payload"])
    assert payload["severity"] == "alert"
    assert payload["topic"] == "doorbell.ring"


@pytest.mark.asyncio
async def test_system_severity_min_matching(tmp_path: Path) -> None:
    triggers = tmp_path / "reactive.yaml"
    triggers.write_text("triggers: []", encoding="utf-8")

    redis = FakeRedis(decode_responses=True)
    registry = MagicMock()
    registry.dispatch = AsyncMock()
    reactive = Reactive(registry=registry, redis=redis, triggers_path=str(triggers))

    trigger = {
        "id": "system_metric_breach",
        "match": {"severity_min": "warn"},
        "notify_from_payload": {
            "text_template": "{metric}",
            "topic_template": "system.{metric}",
            "severity_field": "severity",
        },
    }

    assert reactive._matches(trigger["match"], {"severity": "warn"}) is True
    assert reactive._matches(trigger["match"], {"severity": "info"}) is False


@pytest.mark.asyncio
async def test_observer_events_become_user_notifications(tmp_path: Path) -> None:
    """The shipped reactive_triggers.yaml turns each observer event into a notification.

    NOTE: ``appliance_cycle_completed`` is excluded here because it dispatches
    to ``household_ops.infer_cycle_load`` rather than rendering from the
    payload directly. Its own dedicated test covers the dispatch path.
    """
    repo_root = Path(__file__).resolve().parents[1]  # noqa: ASYNC240 - sync setup
    triggers_path = repo_root / "reactive_triggers.yaml"
    triggers_text = triggers_path.read_text(encoding="utf-8")  # noqa: ASYNC240

    redis = FakeRedis(decode_responses=True)
    registry = MagicMock()
    registry.dispatch = AsyncMock()
    reactive = Reactive(registry=registry, redis=redis, triggers_path=str(triggers_path))

    import yaml as _yaml

    triggers = _yaml.safe_load(triggers_text)["triggers"]
    by_id = {t["id"]: t for t in triggers}

    cases = [
        ("cleaning_completed", "cleaning.completed", "Vacuum cleaning completed for Roomba"),
        ("coffee_brewed", "coffee.brewed", "Coffee brewed by Espresso"),
        (
            "sleep_likely_asleep",
            "sleep.likely_asleep",
            "Bedroom signals suggest everyone is likely asleep",
        ),
        (
            "sleep_likely_awake",
            "sleep.likely_awake",
            "Bedroom signals suggest someone is awake",
        ),
    ]
    for trigger_id, kind, summary in cases:
        assert trigger_id in by_id, f"missing reactive trigger {trigger_id}"
        trigger = by_id[trigger_id]
        assert trigger["stream"] == "events.observed"
        assert trigger["match"]["kind"] == kind

        await reactive.handle_event(
            trigger,
            {"agent": f"observer.{trigger_id}", "kind": kind, "summary": summary, "ts": "x"},
        )

    rows = await redis.xrange("notify.outbound")
    assert len(rows) == len(cases)
    summaries = [json.loads(row[1]["payload"])["text"] for row in rows]
    for _, _, summary in cases:
        assert any(summary in s for s in summaries), f"no notification with summary {summary!r}"


@pytest.mark.asyncio
async def test_observer_event_with_wrong_kind_is_ignored(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]  # noqa: ASYNC240 - sync setup
    triggers_path = repo_root / "reactive_triggers.yaml"
    triggers_text = triggers_path.read_text(encoding="utf-8")  # noqa: ASYNC240

    redis = FakeRedis(decode_responses=True)
    registry = MagicMock()
    registry.dispatch = AsyncMock()
    reactive = Reactive(registry=registry, redis=redis, triggers_path=str(triggers_path))

    import yaml as _yaml

    triggers = _yaml.safe_load(triggers_text)["triggers"]
    washer = next(t for t in triggers if t["id"] == "appliance_cycle_completed")

    await reactive.handle_event(
        washer,
        {"agent": "observer.washer", "kind": "presence.changed", "summary": "Saeed left"},
    )
    rows = await redis.xrange("notify.outbound")
    assert rows == []


@pytest.mark.asyncio
async def test_appliance_cycle_completed_dispatches_and_carries_keyboard(tmp_path: Path) -> None:
    """The shipped appliance_cycle_completed trigger should:
    - Dispatch to household_ops.infer_cycle_load with the observer payload as inputs
    - Read summary AND keyboard from the result and forward to notify.outbound
    """
    repo_root = Path(__file__).resolve().parents[1]  # noqa: ASYNC240
    triggers_path = repo_root / "reactive_triggers.yaml"
    triggers_text = triggers_path.read_text(encoding="utf-8")  # noqa: ASYNC240

    redis = FakeRedis(decode_responses=True)
    registry = MagicMock()
    fake_keyboard = [[{"text": "colors", "callback": "cycle:1:colors"}]]
    registry.dispatch = AsyncMock(
        return_value={
            "ok": True,
            "result": {
                "ok": True,
                "summary": (
                    "🧺 Washer cycle done. best guess: **colors** (80%). Confirm or correct?"
                ),
                "label": "colors",
                "confidence": 0.8,
                "cycle_load_id": 99,
                "keyboard": fake_keyboard,
            },
        }
    )
    reactive = Reactive(registry=registry, redis=redis, triggers_path=str(triggers_path))

    import yaml as _yaml

    triggers = _yaml.safe_load(triggers_text)["triggers"]
    trigger = next(t for t in triggers if t["id"] == "appliance_cycle_completed")

    envelope = {
        "agent": "observer.washer",
        "kind": "appliance.cycle_completed",
        "summary": "Washer cycle completed for Samsung Washer",
        "payload": {
            "appliance": "washer",
            "entity_id": "sensor.washer_power",
            "duration_seconds": 2700,
            "program": "Cotton",
        },
        "ts": "2026-05-13T19:00:00+00:00",
    }
    await reactive.handle_event(trigger, envelope)

    registry.dispatch.assert_awaited_once()
    args = registry.dispatch.call_args
    assert args.args[0] == "household_ops"
    assert args.args[1] == "infer_cycle_load"
    inputs = args.args[2]
    assert inputs["appliance"] == "washer"
    assert inputs["entity_id"] == "sensor.washer_power"
    assert inputs["duration_seconds"] == 2700
    assert inputs["program"] == "Cotton"

    rows = await redis.xrange("notify.outbound")
    assert len(rows) == 1
    notification = json.loads(rows[0][1]["payload"])
    assert "best guess" in notification["text"]
    assert notification["keyboard"] == fake_keyboard


@pytest.mark.asyncio
async def test_presence_changed_home_dispatches_return_inference_and_keyboard(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]  # noqa: ASYNC240
    triggers_path = repo_root / "reactive_triggers.yaml"
    triggers_text = triggers_path.read_text(encoding="utf-8")  # noqa: ASYNC240

    redis = FakeRedis(decode_responses=True)
    registry = MagicMock()
    fake_keyboard = [[{"text": "✅ work", "callback": "presence:9:work"}]]
    registry.dispatch = AsyncMock(
        return_value={
            "ok": True,
            "result": {
                "ok": True,
                "summary": "👋 Welcome home, Saeed. Coming back from work?",
                "context": "work",
                "presence_return_id": 9,
                "keyboard": fake_keyboard,
            },
        }
    )
    reactive = Reactive(registry=registry, redis=redis, triggers_path=str(triggers_path))

    import yaml as _yaml

    triggers = _yaml.safe_load(triggers_text)["triggers"]
    trigger = next(t for t in triggers if t["id"] == "presence_return_home")

    envelope = {
        "agent": "observer.presence",
        "kind": "presence.changed",
        "summary": "Saeed is now home",
        "payload": {
            "entity_id": "device_tracker.saeed_phone",
            "person": "Saeed",
            "state": "home",
            "since": "2026-05-13T17:30:00+04:00",
        },
        "ts": "2026-05-13T13:30:00+00:00",
    }
    await reactive.handle_event(trigger, envelope)

    registry.dispatch.assert_awaited_once()
    args = registry.dispatch.call_args.args
    assert args[0] == "personal_assistant"
    assert args[1] == "infer_presence_return"
    assert args[2]["entity_id"] == "device_tracker.saeed_phone"
    assert args[2]["state"] == "home"

    rows = await redis.xrange("notify.outbound")
    assert len(rows) == 1
    notification = json.loads(rows[0][1]["payload"])
    assert "Welcome home" in notification["text"]
    assert notification["keyboard"] == fake_keyboard


@pytest.mark.asyncio
async def test_reactive_inputs_from_payload_uses_nested_field(tmp_path: Path) -> None:
    """Test the _build_dispatch_inputs helper with nested field selection."""
    triggers_path = tmp_path / "reactive.yaml"
    triggers_path.write_text("triggers: []", encoding="utf-8")
    redis = FakeRedis(decode_responses=True)
    registry = MagicMock()
    registry.dispatch = AsyncMock(return_value={"ok": True, "result": {"summary": "ok"}})
    reactive = Reactive(registry=registry, redis=redis, triggers_path=str(triggers_path))

    trigger = {
        "id": "x",
        "match": {},
        "dispatch": {
            "agent": "x",
            "capability": "y",
            "inputs_from": "payload.payload",
            "inputs": {"override_field": 1},
        },
        "notify_from_result": {"text_field": "summary", "topic": "x"},
    }
    await reactive.handle_event(
        trigger,
        {"payload": {"entity_id": "sensor.x", "duration_seconds": 60}, "summary": "ignored"},
    )
    args = registry.dispatch.call_args.args
    assert args[2] == {"entity_id": "sensor.x", "duration_seconds": 60, "override_field": 1}


@pytest.mark.asyncio
async def test_reactive_notify_from_payload_passes_keyboard_field(tmp_path: Path) -> None:
    triggers_path = tmp_path / "reactive.yaml"
    triggers_path.write_text("triggers: []", encoding="utf-8")
    redis = FakeRedis(decode_responses=True)
    registry = MagicMock()
    registry.dispatch = AsyncMock()
    reactive = Reactive(registry=registry, redis=redis, triggers_path=str(triggers_path))

    trigger = {
        "id": "y",
        "match": {},
        "notify_from_payload": {
            "text_template": "{summary}",
            "topic_template": "x.y",
            "keyboard_field": "kbd",
        },
    }
    kbd = [[{"text": "Yes", "callback": "cycle:1:colors"}]]
    await reactive.handle_event(trigger, {"summary": "hi", "kbd": kbd})
    rows = await redis.xrange("notify.outbound")
    assert len(rows) == 1
    notif = json.loads(rows[0][1]["payload"])
    assert notif["keyboard"] == kbd
