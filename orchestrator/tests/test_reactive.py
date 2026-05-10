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
