from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from home_agents_sdk.tools import clear_tools

import tools.core as core_module
from tools.core import (
    ALERT_NARRATIVE_KEY,
    NARRATIVE_KEY,
    _aggregate,
    _template_narrative,
    agent_card,
    summarize_activity,
    summarize_alerts,
)


@pytest.fixture(autouse=True)
def _reset_tools():
    yield
    clear_tools()
    # Re-import to re-register tools for next test.
    import importlib

    importlib.reload(core_module)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(core_module, "_redis_client", lambda: fake)
    return fake


def _make_event(agent: str, status: str, duration_ms: float = 12.5) -> dict:
    return {
        "agent": agent,
        "capability": "do_thing",
        "status": status,
        "duration_ms": duration_ms,
        "ts": datetime.now(UTC).isoformat(),
    }


def test_aggregate_counts_per_agent_and_status() -> None:
    events = [
        _make_event("home_automation", "ok"),
        _make_event("home_automation", "ok"),
        _make_event("home_automation", "error"),
        _make_event("system_health", "ok"),
    ]
    agg = _aggregate(events)
    by_agent = {row["agent"]: row for row in agg["by_agent"]}
    assert by_agent["home_automation"]["ok"] == 2
    assert by_agent["home_automation"]["errors"] == 1
    assert by_agent["system_health"]["ok"] == 1
    assert agg["total_events"] == 4
    assert agg["total_errors"] == 1


def test_template_narrative_says_idle_when_empty() -> None:
    text = _template_narrative(15, _aggregate([]))
    assert "No agent activity" in text
    assert "15" in text


def test_template_narrative_lists_agents_and_flags_errors() -> None:
    events = [
        _make_event("home_automation", "ok"),
        _make_event("system_health", "error"),
    ]
    text = _template_narrative(15, _aggregate(events))
    assert "home_automation" in text
    assert "system_health" in text
    assert "error" in text.lower()


async def _seed_activity(fake, events: list[dict]) -> None:
    for event in events:
        await fake.xadd("events.activity", {"payload": json.dumps(event)})


@pytest.mark.asyncio
async def test_summarize_activity_falls_back_to_template_when_llm_down(
    fake_redis, monkeypatch
) -> None:
    await _seed_activity(
        fake_redis,
        [_make_event("home_automation", "ok"), _make_event("system_health", "ok")],
    )

    async def _no_llm(_w, _agg):
        return None

    monkeypatch.setattr(core_module, "_llm_narrative", _no_llm)

    result = await summarize_activity(window_minutes=15)
    assert "narrative" in result
    assert "home_automation" in result["narrative"]

    cached = await fake_redis.get(NARRATIVE_KEY)
    assert cached is not None
    parsed = json.loads(cached)
    assert parsed["narrative"] == result["narrative"]


@pytest.mark.asyncio
async def test_summarize_activity_uses_llm_when_available(fake_redis, monkeypatch) -> None:
    await _seed_activity(fake_redis, [_make_event("home_automation", "ok")])

    async def _fake_llm(_window, _agg):
        return "Everything is delightful."

    monkeypatch.setattr(core_module, "_llm_narrative", _fake_llm)

    result = await summarize_activity(window_minutes=10)
    assert result["narrative"] == "Everything is delightful."


@pytest.mark.asyncio
async def test_agent_card_says_idle_with_no_events(fake_redis, monkeypatch) -> None:
    result = await agent_card(agent="entertainment", window_minutes=5)
    assert "entertainment" in result["line"]
    assert "idle" in result["line"].lower()


@pytest.mark.asyncio
async def test_agent_card_summarizes_when_events_exist(fake_redis, monkeypatch) -> None:
    await _seed_activity(
        fake_redis,
        [
            _make_event("knowledge_notes", "ok", duration_ms=20),
            _make_event("knowledge_notes", "error"),
        ],
    )
    result = await agent_card(agent="knowledge_notes", window_minutes=15)
    assert "knowledge_notes" in result["line"]
    assert "error" in result["line"].lower()


@pytest.mark.asyncio
async def test_summarize_alerts_no_alerts_message(fake_redis, monkeypatch) -> None:
    result = await summarize_alerts(window_minutes=30)
    assert "No warnings" in result["narrative"] or "no warnings" in result["narrative"].lower()
    cached = await fake_redis.get(ALERT_NARRATIVE_KEY)
    assert cached is not None


@pytest.mark.asyncio
async def test_summarize_alerts_filters_by_severity_and_window(fake_redis, monkeypatch) -> None:
    fresh_warn = {
        "ts": datetime.now(UTC).isoformat(),
        "topic": "system.cpu",
        "severity": "warn",
        "agent": "system_health",
        "decision": "send",
        "reason": "test",
        "text": "CPU high",
    }
    old_warn = {
        "ts": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        "topic": "system.cpu",
        "severity": "warn",
        "agent": "system_health",
        "decision": "send",
        "reason": "old",
        "text": "old alert",
    }
    info_only = {
        "ts": datetime.now(UTC).isoformat(),
        "topic": "system.cpu",
        "severity": "info",
        "agent": "system_health",
        "decision": "send",
        "reason": "test",
        "text": "ok",
    }
    for item in [fresh_warn, old_warn, info_only]:
        await fake_redis.lpush("policy:recent", json.dumps(item))

    async def _fake_llm_chat(*_a, **_kw):
        raise RuntimeError("no llm")

    class _StubLlm:
        async def chat(self, *_a, **_kw):
            raise RuntimeError("no llm")

    monkeypatch.setattr(core_module, "_llm_client", lambda: _StubLlm())

    result = await summarize_alerts(window_minutes=30)
    assert result["alert_count"] == 1
    assert "1 alert" in result["narrative"]


@pytest.mark.asyncio
async def test_summarize_activity_publishes_dashboard_update(fake_redis, monkeypatch) -> None:
    await _seed_activity(fake_redis, [_make_event("home_automation", "ok")])

    async def _no_llm(_window, _agg):
        return None

    monkeypatch.setattr(core_module, "_llm_narrative", _no_llm)

    await summarize_activity(window_minutes=15)

    rows = await fake_redis.xrange(core_module.DASHBOARD_STREAM)
    assert len(rows) == 1
    payload = json.loads(rows[0][1]["payload"])
    assert payload["type"] == "activity.summary"
    assert payload["agent"] == "dashboard_curator"
    assert payload["record"]["stats"]["total_events"] == 1
