from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis

logger = get_logger("scheduler")


@dataclass(slots=True)
class JobInfo:
    id: str
    next_run_time: str | None
    last_run_time: str | None
    last_status: str


class Scheduler:
    def __init__(
        self,
        registry: Any,
        redis: Redis,
        schedules_path: str,
        timezone: str = "Asia/Dubai",
    ) -> None:
        self._registry = registry
        self._redis = redis
        self._schedules_path = Path(schedules_path)
        self._scheduler = AsyncIOScheduler(timezone=timezone)
        self._definitions: dict[str, dict[str, Any]] = {}
        self._history: dict[str, dict[str, str]] = {}

    @property
    def apscheduler(self) -> AsyncIOScheduler:
        return self._scheduler

    async def start(self) -> None:
        await self.reload()
        self._scheduler.start()

    async def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    async def reload(self) -> dict[str, Any]:
        data = yaml.safe_load(self._schedules_path.read_text(encoding="utf-8")) or {}
        jobs = data.get("jobs", [])
        self._definitions = {j["id"]: j for j in jobs}

        for job in self._scheduler.get_jobs():
            self._scheduler.remove_job(job.id)

        for item in jobs:
            self._add_job(item)
        return {"jobs": len(jobs)}

    def _add_job(self, job_cfg: dict[str, Any]) -> None:
        trigger_type = job_cfg.get("trigger")
        if trigger_type == "cron":
            trigger = CronTrigger(**job_cfg.get("cron", {}), timezone=self._scheduler.timezone)
        elif trigger_type == "interval":
            trigger = IntervalTrigger(
                **job_cfg.get("interval", {}), timezone=self._scheduler.timezone
            )
        else:
            raise ValueError(f"Unsupported trigger type: {trigger_type}")

        job_id = job_cfg["id"]
        self._scheduler.add_job(
            self._execute_job,
            trigger=trigger,
            id=job_id,
            kwargs={"job_id": job_id},
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    async def _execute_job(self, job_id: str) -> dict[str, Any]:
        cfg = self._definitions[job_id]
        self._history.setdefault(job_id, {})["last_run_time"] = datetime.now(UTC).isoformat()
        try:
            dispatch = cfg.get("dispatch", {})
            inputs = self._resolve_inputs(dispatch.get("inputs", {}))
            result = await self._registry.dispatch(
                dispatch.get("agent", ""),
                dispatch.get("capability", ""),
                inputs,
            )

            if cfg.get("notify"):
                payload = self._build_notify_payload(cfg, dispatch, result)
                await self._redis.xadd("notify.outbound", {"payload": json.dumps(payload)})

            self._history[job_id]["last_status"] = "ok"
            return {"ok": True, "result": result}
        except Exception as exc:
            logger.warning("scheduled_job_failed", job_id=job_id, error=str(exc))
            self._history[job_id]["last_status"] = f"error: {exc}"
            return {"ok": False, "error": str(exc)}

    def _build_notify_payload(
        self,
        cfg: dict[str, Any],
        dispatch: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        notify = cfg.get("notify", {})
        output = result.get("result")
        if isinstance(output, str):
            text = output
        else:
            text = json.dumps(output, ensure_ascii=False, indent=2)[:1200]
        return {
            "text": text,
            "severity": notify.get("severity", "info"),
            "topic": notify.get("topic"),
            "agent": dispatch.get("agent"),
            "capability": dispatch.get("capability"),
        }

    def _resolve_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for key, value in inputs.items():
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                resolved[key] = os.environ.get(value[2:-1], "")
            else:
                resolved[key] = value
        return resolved

    def list_jobs(self) -> list[JobInfo]:
        rows: list[JobInfo] = []
        for job in self._scheduler.get_jobs():
            hist = self._history.get(job.id, {})
            rows.append(
                JobInfo(
                    id=job.id,
                    next_run_time=job.next_run_time.isoformat() if job.next_run_time else None,
                    last_run_time=hist.get("last_run_time"),
                    last_status=hist.get("last_status", "never"),
                )
            )
        return rows

    def get_job(self, job_id: str) -> Job | None:
        return self._scheduler.get_job(job_id)

    async def run_job_now(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._definitions:
            raise KeyError(job_id)
        return await self._execute_job(job_id)
