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
    """The shipped reactive_triggers.yaml turns each observer event into a notification."""
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
        (
            "appliance_cycle_completed",
            "appliance.cycle_completed",
            "Washer cycle completed for Bosch",
        ),
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
