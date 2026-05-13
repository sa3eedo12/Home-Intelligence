from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fakeredis.aioredis import FakeRedis

from orchestrator.reactive import Reactive


@pytest.mark.asyncio
async def test_auto_inference_trigger_dispatches_full_envelope_and_keyboard() -> None:
    repo_root = Path(__file__).resolve().parents[1]  # noqa: ASYNC240
    triggers_path = repo_root / "reactive_triggers.yaml"
    triggers = yaml.safe_load(triggers_path.read_text(encoding="utf-8"))["triggers"]  # noqa: ASYNC240
    trigger = next(t for t in triggers if t["id"] == "auto_inference_on_observer_event")

    redis = FakeRedis(decode_responses=True)
    registry = MagicMock()
    fake_keyboard = [[{"text": "✅ Yes, log it", "callback": "infer:77:confirmed"}]]
    registry.dispatch = AsyncMock(
        return_value={
            "ok": True,
            "result": {
                "ok": True,
                "summary": "🤔 Did you just go to bed?",
                "keyboard": fake_keyboard,
                "auto_inference_id": 77,
            },
        }
    )
    reactive = Reactive(registry=registry, redis=redis, triggers_path=str(triggers_path))
    envelope = {
        "agent": "observer.sleep",
        "kind": "sleep.likely_asleep",
        "summary": "Bedroom signals suggest everyone is likely asleep",
        "payload": {"signals": {"bedroom_lights_off": True, "tv_off": True}},
        "ts": "2026-05-13T19:00:00+00:00",
    }

    assert reactive._matches(trigger.get("match", {}), envelope) is True
    await reactive.handle_event(trigger, envelope)

    registry.dispatch.assert_awaited_once()
    args = registry.dispatch.call_args.args
    assert args[0] == "personal_assistant"
    assert args[1] == "auto_infer_observer_event"
    assert args[2] == envelope

    rows = await redis.xrange("notify.outbound")
    assert len(rows) == 1
    notification = json.loads(rows[0][1]["payload"])
    assert notification["topic"] == "auto_inference"
    assert notification["severity"] == "info"
    assert notification["keyboard"] == fake_keyboard
