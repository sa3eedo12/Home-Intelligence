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
