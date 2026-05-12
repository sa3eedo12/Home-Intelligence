from __future__ import annotations

from typing import Any

from home_agents_sdk.telemetry import get_logger

from .common import SingleFlightJob, command_count, maybe_await

logger = get_logger("orchestrator.data_science.maintenance")

_STREAM_LIMITS = {
    "events.activity": 10_000,
    "events.observed": 10_000,
    "events.system": 10_000,
    "events.home": 10_000,
    "notify.outbound": 2_000,
}


class MaintenanceJob(SingleFlightJob):
    def __init__(
        self,
        pool: Any,
        redis: Any,
        event_log_store: Any | None = None,
    ) -> None:
        super().__init__(job_name="maintenance", pool=pool, event_log_store=event_log_store)
        self.redis = redis

    async def run(self) -> dict[str, Any]:
        return await self._run_singleflight(self._run)

    async def _run(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "ok",
            "streams_trimmed": {},
            "archived_rows": 0,
            "deleted_event_rows": 0,
            "deleted_brief_rows": 0,
            "reclaimed_space_estimate_bytes": 0,
            "errors": [],
        }
        await self._trim_streams(result)
        await self._vacuum_event_log(result)
        await self._archive_old_events(result)
        await self._delete_old_briefs(result)
        if result["errors"]:
            result["status"] = "partial"
        result["reclaimed_space_estimate_bytes"] = (
            int(result["deleted_event_rows"]) * 2048 + int(result["deleted_brief_rows"]) * 4096
        )
        return result

    async def _trim_streams(self, result: dict[str, Any]) -> None:
        if self.redis is None:
            result["errors"].append({"step": "redis_trim", "error": "redis_unavailable"})
            return
        for stream, maxlen in _STREAM_LIMITS.items():
            try:
                trimmed = await maybe_await(
                    self.redis.xtrim(stream, maxlen=maxlen, approximate=True)
                )
                result["streams_trimmed"][stream] = int(trimmed or 0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("maintenance_stream_trim_failed", stream=stream, error=str(exc))
                result["errors"].append({"step": f"trim:{stream}", "error": str(exc)})

    async def _vacuum_event_log(self, result: dict[str, Any]) -> None:
        if self.pool is None:
            result["errors"].append({"step": "vacuum", "error": "postgres_unavailable"})
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("VACUUM (ANALYZE) event_log")
        except Exception as exc:  # noqa: BLE001
            logger.warning("maintenance_vacuum_failed", error=str(exc))
            result["errors"].append({"step": "vacuum", "error": str(exc)})

    async def _archive_old_events(self, result: dict[str, Any]) -> None:
        if self.pool is None:
            result["errors"].append({"step": "archive", "error": "postgres_unavailable"})
            return
        try:
            async with self.pool.acquire() as conn:
                insert_status = await conn.execute(
                    """
                    INSERT INTO event_log_archive
                    SELECT * FROM event_log
                    WHERE ts < now() - interval '90 days'
                    ON CONFLICT DO NOTHING
                    """
                )
                delete_status = await conn.execute(
                    """
                    DELETE FROM event_log
                    WHERE ts < now() - interval '90 days'
                    """
                )
            result["archived_rows"] = command_count(insert_status)
            result["deleted_event_rows"] = command_count(delete_status)
        except Exception as exc:  # noqa: BLE001
            logger.warning("maintenance_archive_failed", error=str(exc))
            result["errors"].append({"step": "archive", "error": str(exc)})

    async def _delete_old_briefs(self, result: dict[str, Any]) -> None:
        if self.pool is None:
            result["errors"].append({"step": "delete_briefs", "error": "postgres_unavailable"})
            return
        try:
            async with self.pool.acquire() as conn:
                status = await conn.execute(
                    """
                    DELETE FROM morning_brief
                    WHERE generated_at < now() - interval '180 days'
                    """
                )
            result["deleted_brief_rows"] = command_count(status)
        except Exception as exc:  # noqa: BLE001
            logger.warning("maintenance_brief_delete_failed", error=str(exc))
            result["errors"].append({"step": "delete_briefs", "error": str(exc)})
