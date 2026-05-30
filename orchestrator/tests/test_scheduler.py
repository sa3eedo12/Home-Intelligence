from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakeredis.aioredis import FakeRedis

from orchestrator.scheduler import Scheduler


@pytest.mark.asyncio
async def test_yaml_registers_cron_interval_and_dow_jobs(tmp_path: Path) -> None:
    schedules = tmp_path / "schedules.yaml"
    schedules.write_text(
        """
jobs:
  - id: job_cron
    trigger: cron
    cron: { hour: 7, minute: 30 }
    dispatch: { agent: a, capability: c, inputs: {} }
  - id: job_interval
    trigger: interval
    interval: { minutes: 15 }
    dispatch: { agent: a, capability: c, inputs: {} }
  - id: job_dow
    trigger: cron
    cron: { day_of_week: "wed,sat", hour: 18, minute: 0 }
    dispatch: { agent: a, capability: c, inputs: {} }
""",
        encoding="utf-8",
    )
    registry = MagicMock()
    registry.dispatch = AsyncMock(return_value={"ok": True, "result": "ok"})
    scheduler = Scheduler(
        registry=registry, redis=FakeRedis(decode_responses=True), schedules_path=str(schedules)
    )

    await scheduler.start()
    try:
        jobs = scheduler.apscheduler.get_jobs()
        assert len(jobs) == 3
        assert {j.id for j in jobs} == {"job_cron", "job_interval", "job_dow"}
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_list_jobs_includes_next_run_times(tmp_path: Path) -> None:
    schedules = tmp_path / "schedules.yaml"
    schedules.write_text(
        """
jobs:
  - id: job1
    trigger: interval
    interval: { minutes: 5 }
    dispatch: { agent: a, capability: c, inputs: {} }
""",
        encoding="utf-8",
    )
    registry = MagicMock()
    registry.dispatch = AsyncMock(return_value={"ok": True, "result": "ok"})
    scheduler = Scheduler(
        registry=registry, redis=FakeRedis(decode_responses=True), schedules_path=str(schedules)
    )
    await scheduler.start()
    try:
        jobs = scheduler.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "job1"
        assert jobs[0].next_run_time is not None
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_internal_orchestrator_dispatch_runs_callback(tmp_path: Path) -> None:
    schedules = tmp_path / "schedules.yaml"
    schedules.write_text(
        """
jobs:
  - id: reflect
    trigger: interval
    interval: { minutes: 5 }
    dispatch: { agent: __orchestrator__, capability: reflector.run, inputs: {} }
""",
        encoding="utf-8",
    )
    callback = AsyncMock(return_value={"brief_id": 1})
    registry = MagicMock()
    registry.dispatch = AsyncMock()
    scheduler = Scheduler(
        registry=registry,
        redis=FakeRedis(decode_responses=True),
        schedules_path=str(schedules),
        internal_callbacks={"reflector.run": callback},
    )
    await scheduler.start()
    try:
        result = await scheduler.run_job_now("reflect")
        assert result == {"ok": True, "result": {"brief_id": 1}}
        callback.assert_awaited_once_with({})
        registry.dispatch.assert_not_awaited()
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_job_notify_can_use_text_field_keyboard_and_notify_flag(tmp_path: Path) -> None:
    schedules = tmp_path / "schedules.yaml"
    schedules.write_text(
        """
jobs:
  - id: sleep
    trigger: interval
    interval: { minutes: 5 }
    dispatch: { agent: personal_assistant, capability: infer_sleep_summary, inputs: {} }
    notify:
      severity: notice
      topic: sleep.summary
      text_field: summary
      keyboard_field: keyboard
  - id: quiet
    trigger: interval
    interval: { minutes: 5 }
    dispatch: { agent: personal_assistant, capability: late_bedtime_check, inputs: {} }
    notify: { severity: notice, topic: sleep.bedtime, text_field: summary }
""",
        encoding="utf-8",
    )
    redis = FakeRedis(decode_responses=True)
    registry = MagicMock()
    keyboard = [[{"text": "Decent", "callback": "sleep:1:decent"}]]
    registry.dispatch = AsyncMock(
        side_effect=[
            {"ok": True, "result": {"summary": "slept well", "keyboard": keyboard}},
            {"ok": True, "result": {"summary": "", "notify": False}},
        ]
    )

    scheduler = Scheduler(registry=registry, redis=redis, schedules_path=str(schedules))
    await scheduler.start()
    try:
        await scheduler.run_job_now("sleep")
        await scheduler.run_job_now("quiet")
        stream_rows = await redis.xrange("notify.outbound")
        assert len(stream_rows) == 1
        payload = json.loads(stream_rows[0][1]["payload"])
        assert payload["text"] == "slept well"
        assert payload["keyboard"] == keyboard
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_job_execution_dispatches_and_emits_notify(tmp_path: Path) -> None:
    schedules = tmp_path / "schedules.yaml"
    schedules.write_text(
        """
jobs:
  - id: hello
    trigger: interval
    interval: { minutes: 5 }
    dispatch: { agent: test_agent, capability: say_hi, inputs: {} }
    notify: { severity: info, topic: test.topic }
""",
        encoding="utf-8",
    )
    redis = FakeRedis(decode_responses=True)
    registry = MagicMock()
    registry.dispatch = AsyncMock(return_value={"ok": True, "result": "hi"})

    scheduler = Scheduler(registry=registry, redis=redis, schedules_path=str(schedules))
    await scheduler.start()
    try:
        result = await scheduler.run_job_now("hello")
        assert result["ok"] is True

        stream_rows = await redis.xrange("notify.outbound")
        assert len(stream_rows) == 1
        payload = json.loads(stream_rows[0][1]["payload"])
        assert payload["text"] == "hi"
        assert payload["topic"] == "test.topic"
    finally:
        await scheduler.shutdown()


# ── Notify payload building ─────────────────────────────────────


def _make_scheduler(tmp_path: Path = None) -> Scheduler:
    """Bare scheduler instance for unit-testing pure helpers."""
    # Need a real (empty) schedules file because Scheduler.__init__
    # tries to read it. Use a unique tmpfile each call.
    import tempfile
    fd = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False,
    )
    fd.write("jobs: []\n")
    fd.close()
    return Scheduler(
        registry=MagicMock(),
        redis=MagicMock(),
        schedules_path=fd.name,
        timezone="UTC",
        internal_callbacks={},
    )


def test_notify_picks_markdown_field_when_no_text_field_configured() -> None:
    """Tools like morning_brief and evening_recap return
    {markdown: ...}. Without an explicit text_field, the scheduler
    should pick the markdown content, not JSON-dump the whole dict."""
    sched = _make_scheduler()
    cfg = {"notify": {"severity": "info", "topic": "brief.morning"}}
    dispatch = {"agent": "personal_assistant", "capability": "morning_brief"}
    result = {"markdown": "### Morning Brief\n- All good"}
    payload = sched._build_notify_payload(cfg, dispatch, result)
    assert payload is not None
    assert payload["text"] == "### Morning Brief\n- All good"
    # Verify the JSON dump fallback didn't kick in
    assert "{" not in payload["text"][:5]


def test_notify_picks_summary_text_or_message_field() -> None:
    sched = _make_scheduler()
    cfg = {"notify": {"topic": "x"}}
    for field in ("text", "summary", "markdown", "message"):
        payload = sched._build_notify_payload(
            cfg, {"agent": "a", "capability": "c"},
            {field: f"hello via {field}"},
        )
        assert payload is not None
        assert payload["text"] == f"hello via {field}"


def test_notify_suppresses_empty_indexed_payload() -> None:
    """notes.index returns {ok: True, indexed: 0, skipped: 0} on idle
    polls — must be silent, not 'send me a JSON dump'."""
    sched = _make_scheduler()
    cfg = {"notify": {"topic": "notes.index"}}
    dispatch = {"agent": "knowledge_notes", "capability": "index_path"}
    payload = sched._build_notify_payload(
        cfg, dispatch, {"ok": True, "indexed": 0, "skipped": 0},
    )
    assert payload is None


def test_notify_suppresses_empty_items_payload() -> None:
    """pantry_low_stock returns {items: []} when nothing is low. The
    user should not be pinged with '{"items": []}'."""
    sched = _make_scheduler()
    cfg = {"notify": {"topic": "household.pantry_low"}}
    dispatch = {"agent": "household_ops", "capability": "pantry_low_stock"}
    payload = sched._build_notify_payload(cfg, dispatch, {"items": []})
    assert payload is None


def test_notify_still_sends_non_empty_items_payload() -> None:
    sched = _make_scheduler()
    cfg = {"notify": {"topic": "x"}}
    dispatch = {"agent": "a", "capability": "c"}
    payload = sched._build_notify_payload(
        cfg, dispatch,
        {"items": ["milk", "eggs"], "summary": "2 items low"},
    )
    assert payload is not None
    assert payload["text"] == "2 items low"


def test_notify_returns_none_for_null_output() -> None:
    sched = _make_scheduler()
    cfg = {"notify": {"topic": "x"}}
    payload = sched._build_notify_payload(
        cfg, {"agent": "a", "capability": "c"}, None,
    )
    assert payload is None


def test_notify_respects_explicit_text_field_when_set() -> None:
    """When the schedule explicitly names text_field, that takes
    priority over the auto-detect heuristic."""
    sched = _make_scheduler()
    cfg = {"notify": {"text_field": "summary", "topic": "x"}}
    payload = sched._build_notify_payload(
        cfg, {"agent": "a", "capability": "c"},
        {"summary": "use this", "markdown": "ignore this"},
    )
    assert payload is not None
    assert payload["text"] == "use this"


def test_notify_obeys_explicit_notify_false() -> None:
    sched = _make_scheduler()
    cfg = {"notify": {"topic": "x"}}
    payload = sched._build_notify_payload(
        cfg, {"agent": "a", "capability": "c"},
        {"notify": False, "summary": "would have sent"},
    )
    assert payload is None
