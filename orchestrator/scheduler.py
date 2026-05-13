from __future__ import annotations

import inspect
import json
import os
from collections.abc import Awaitable, Callable
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


InternalCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


class Scheduler:
    def __init__(
        self,
        registry: Any,
        redis: Redis,
        schedules_path: str,
        timezone: str = "Asia/Dubai",
        internal_callbacks: dict[str, InternalCallback] | None = None,
    ) -> None:
        self._registry = registry
        self._redis = redis
        self._schedules_path = Path(schedules_path)
        self._scheduler = AsyncIOScheduler(timezone=timezone)
        self._definitions: dict[str, dict[str, Any]] = {}
        self._history: dict[str, dict[str, str]] = {}
        self._internal_callbacks = dict(internal_callbacks or {})

    @property
    def apscheduler(self) -> AsyncIOScheduler:
        return self._scheduler

    def register_internal_callback(self, capability: str, callback: InternalCallback) -> None:
        self._internal_callbacks[capability] = callback

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
            cron_cfg = self._resolve_cron(job_cfg)
            trigger = CronTrigger(**cron_cfg, timezone=self._job_timezone(job_cfg))
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
            if dispatch.get("agent") == "__orchestrator__":
                capability = str(dispatch.get("capability") or "")
                result = await self._dispatch_internal(capability, inputs)
            else:
                result = await self._registry.dispatch(
                    dispatch.get("agent", ""),
                    dispatch.get("capability", ""),
                    inputs,
                )

            if cfg.get("notify"):
                payload = self._build_notify_payload(cfg, dispatch, result)
                if payload is not None:
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
    ) -> dict[str, Any] | None:
        notify = cfg.get("notify", {})
        output = result.get("result") if isinstance(result, dict) and "result" in result else result
        keyboard = None
        if isinstance(output, dict):
            if output.get("notify") is False:
                return None
            text_field = notify.get("text_field")
            if text_field:
                text = str(output.get(text_field) or "")
            else:
                text = json.dumps(output, ensure_ascii=False, indent=2)[:1200]
            keyboard_field = notify.get("keyboard_field")
            if keyboard_field:
                keyboard = output.get(keyboard_field)
        elif isinstance(output, str):
            text = output
        else:
            text = json.dumps(output, ensure_ascii=False, indent=2)[:1200]
        if not text.strip():
            return None
        payload = {
            "text": text,
            "severity": notify.get("severity", "info"),
            "topic": notify.get("topic"),
            "agent": dispatch.get("agent"),
            "capability": dispatch.get("capability"),
        }
        if keyboard:
            payload["keyboard"] = keyboard
        return payload

    async def _dispatch_internal(self, capability: str, inputs: dict[str, Any]) -> dict[str, Any]:
        callback = self._internal_callbacks.get(capability)
        if callback is None:
            raise KeyError(f"Unknown orchestrator capability: {capability}")
        result = callback(inputs)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _resolve_cron(self, job_cfg: dict[str, Any]) -> dict[str, Any]:
        cron = dict(job_cfg.get("cron", {}))
        cron.pop("timezone", None)
        if job_cfg.get("time_env"):
            default_time = str(job_cfg.get("time_default") or "07:30")
            raw_time = os.environ.get(str(job_cfg["time_env"])) or default_time
            hour, minute = self._parse_hhmm(raw_time)
            cron["hour"] = hour
            cron["minute"] = minute
        return {key: self._resolve_env_value(value) for key, value in cron.items()}

    def _job_timezone(self, job_cfg: dict[str, Any]) -> Any:
        timezone_env = job_cfg.get("timezone_env")
        if timezone_env:
            value = os.environ.get(str(timezone_env)) or os.environ.get("TZ")
            if value:
                return value
        cron_tz = job_cfg.get("cron", {}).get("timezone")
        if isinstance(cron_tz, str) and cron_tz.startswith("${") and cron_tz.endswith("}"):
            return os.environ.get(cron_tz[2:-1], self._scheduler.timezone)
        return cron_tz or self._scheduler.timezone

    def _parse_hhmm(self, raw: str) -> tuple[int, int]:
        parts = raw.strip().split(":", 1)
        if len(parts) != 2:
            raise ValueError(f"Expected HH:MM time, got {raw!r}")
        hour = int(parts[0])
        minute = int(parts[1])
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError(f"Invalid HH:MM time: {raw!r}")
        return hour, minute

    def _resolve_env_value(self, value: Any) -> Any:
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            return os.environ.get(value[2:-1], "")
        return value

    def _resolve_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {key: self._resolve_env_value(value) for key, value in inputs.items()}

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
