from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any

from .telemetry import get_logger

logger = get_logger("home_agents_sdk.health_store")

_SUM_METRICS = {
    "steps",
    "active_energy",
    "sleep_asleep",
    "sleep_awake",
    "sleep_inBed",
    "sleep_deep",
    "sleep_rem",
    "sleep_core",
    "mindfulness",
}
_COUNT_METRICS = {"workout"}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _format_value(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _format_value(_decode_json(val)) for key, val in value.items()}
    if isinstance(value, list):
        return [_format_value(_decode_json(item)) for item in value]
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key, value in list(data.items()):
        if key in {"metadata", "raw"}:
            value = _decode_json(value)
        data[key] = _format_value(value)
    return data


def _json_object(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return {"value": value}


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


class HealthStore:
    def __init__(self, pool: Any | None) -> None:
        self.pool = pool

    @asynccontextmanager
    async def _connection(self, operation: str):
        if self.pool is None:
            logger.warning("health_store_unavailable", operation=operation, reason="no_pool")
            yield None
            return
        try:
            async with self.pool.acquire() as conn:
                yield conn
        except Exception as exc:  # noqa: BLE001
            logger.warning("health_store_unavailable", operation=operation, error=str(exc))
            yield None

    async def upsert_metrics(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        clean_rows = [row for row in (self._prepare_row(row) for row in rows) if row is not None]
        if not clean_rows:
            return {"inserted": 0, "skipped": len(rows)}

        async with self._connection("upsert_metrics") as conn:
            if conn is None:
                return {"inserted": 0, "skipped": len(rows)}
            try:
                inserted = await conn.fetchval(
                    """
                    WITH input AS (
                        SELECT *
                        FROM jsonb_to_recordset($1::jsonb) AS x(
                            metric text,
                            started_at timestamptz,
                            ended_at timestamptz,
                            value double precision,
                            unit text,
                            source text,
                            member_id int,
                            metadata jsonb,
                            raw jsonb
                        )
                    ), inserted AS (
                        INSERT INTO health_metrics(
                            metric, started_at, ended_at, value, unit, source,
                            member_id, metadata, raw
                        )
                        SELECT
                            metric,
                            started_at,
                            ended_at,
                            value,
                            unit,
                            COALESCE(NULLIF(source, ''), 'health_auto_export'),
                            member_id,
                            COALESCE(metadata, '{}'::jsonb),
                            COALESCE(raw, '{}'::jsonb)
                        FROM input
                        WHERE metric IS NOT NULL AND started_at IS NOT NULL
                        ON CONFLICT DO NOTHING
                        RETURNING 1
                    )
                    SELECT count(*)::int FROM inserted
                    """,
                    _json_dumps(clean_rows),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "health_store_query_failed", operation="upsert_metrics", error=str(exc)
                )
                return {"inserted": 0, "skipped": len(rows)}
        inserted_count = int(inserted or 0)
        return {"inserted": inserted_count, "skipped": max(0, len(rows) - inserted_count)}

    async def list_recent(
        self, metric: str | None = None, hours: int = 24
    ) -> list[dict[str, Any]]:
        bounded_hours = _bounded_int(hours, default=24, low=1, high=24 * 365)
        metric_filter = str(metric).strip() if metric else None
        async with self._connection("list_recent") as conn:
            if conn is None:
                return []
            try:
                rows = await conn.fetch(
                    """
                    SELECT id, metric, started_at, ended_at, value, unit, source,
                           member_id, metadata, raw, received_at
                    FROM health_metrics
                    WHERE started_at >= now() - ($1::int * interval '1 hour')
                      AND ($2::text IS NULL OR metric = $2::text)
                    ORDER BY started_at DESC, id DESC
                    LIMIT 500
                    """,
                    bounded_hours,
                    metric_filter,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("health_store_query_failed", operation="list_recent", error=str(exc))
                return []
        return [_row_dict(row) for row in rows]

    async def aggregate_daily(self, metric: str, days: int = 30) -> list[dict[str, Any]]:
        metric_filter = str(metric or "").strip()
        if not metric_filter:
            return []
        bounded_days = _bounded_int(days, default=30, low=1, high=365)
        # Sleep metrics carry overlapping intervals AND parallel stage
        # rows. Apple Health sends snapshots like:
        #   sleep_asleep 02:21 → 04:40  (138.95min — early partial)
        #   sleep_asleep 02:21 → 09:25  (423.883min — full night so far)
        #   sleep_asleep 06:01 → 09:25  (203.95min — late partial)
        # All three are the SAME sleep session expressed differently.
        # A naive sum gives 12.8h for a 7h night.
        #
        # Two-stage dedup: first within (started_at, ended_at) take the
        # MAX(value) latest snapshot, then within a day pick the SINGLE
        # interval with the largest coverage (longest end-start span).
        # Cumulative metrics like steps have distinct non-overlapping
        # intervals so this is still correct for them — each interval
        # is unique, max-by-coverage picks the only interval, sum picks
        # all of them via the GROUP BY day.
        is_session_metric = metric_filter.startswith("sleep_") or metric_filter in {
            "workout",
            "mindfulness",
        }
        if is_session_metric:
            query = """
                WITH deduped AS (
                    SELECT date_trunc('day', started_at)::date AS day,
                           started_at,
                           ended_at,
                           max(value) AS value,
                           (array_agg(unit ORDER BY received_at DESC NULLS LAST))[1] AS unit
                    FROM health_metrics
                    WHERE metric = $1::text
                      AND started_at >= date_trunc('day', now())
                          - (($2::int - 1) * interval '1 day')
                    GROUP BY day, started_at, ended_at
                ),
                ranked AS (
                    SELECT day, started_at, ended_at, value, unit,
                           row_number() OVER (
                               PARTITION BY day
                               ORDER BY value DESC NULLS LAST,
                                        ended_at - started_at DESC NULLS LAST
                           ) AS rn
                    FROM deduped
                )
                SELECT day,
                       1::int AS count,
                       value::double precision AS sum,
                       value::double precision AS avg,
                       value::double precision AS min,
                       value::double precision AS max,
                       unit
                FROM ranked
                WHERE rn = 1
                ORDER BY day ASC
            """
        else:
            query = """
                WITH deduped AS (
                    SELECT date_trunc('day', started_at)::date AS day,
                           started_at,
                           ended_at,
                           max(value) AS value,
                           (array_agg(unit ORDER BY received_at DESC NULLS LAST))[1] AS unit
                    FROM health_metrics
                    WHERE metric = $1::text
                      AND started_at >= date_trunc('day', now())
                          - (($2::int - 1) * interval '1 day')
                    GROUP BY day, started_at, ended_at
                )
                SELECT day,
                       count(*)::int AS count,
                       sum(value)::double precision AS sum,
                       avg(value)::double precision AS avg,
                       min(value)::double precision AS min,
                       max(value)::double precision AS max,
                       (array_agg(unit))[1] AS unit
                FROM deduped
                GROUP BY day
                ORDER BY day ASC
            """
        async with self._connection("aggregate_daily") as conn:
            if conn is None:
                return []
            try:
                rows = await conn.fetch(query, metric_filter, bounded_days)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "health_store_query_failed", operation="aggregate_daily", error=str(exc)
                )
                return []
        return [self._aggregate_row(metric_filter, row) for row in rows]

    async def latest(self, metric: str) -> dict[str, Any] | None:
        metric_filter = str(metric or "").strip()
        if not metric_filter:
            return None
        async with self._connection("latest") as conn:
            if conn is None:
                return None
            try:
                row = await conn.fetchrow(
                    """
                    SELECT id, metric, started_at, ended_at, value, unit, source,
                           member_id, metadata, raw, received_at
                    FROM health_metrics
                    WHERE metric = $1::text
                    ORDER BY started_at DESC, id DESC
                    LIMIT 1
                    """,
                    metric_filter,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("health_store_query_failed", operation="latest", error=str(exc))
                return None
        return _row_dict(row) if row else None

    async def summary(self) -> dict[str, Any]:
        async with self._connection("summary") as conn:
            if conn is None:
                return {"total_metrics": 0, "last_received_at": None}
            try:
                row = await conn.fetchrow(
                    """
                    SELECT count(*)::bigint AS total_metrics,
                           max(received_at) AS last_received_at
                    FROM health_metrics
                    """
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("health_store_query_failed", operation="summary", error=str(exc))
                return {"total_metrics": 0, "last_received_at": None}
        return _row_dict(row) if row else {"total_metrics": 0, "last_received_at": None}

    def _prepare_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(row, dict):
            return None
        metric = str(row.get("metric") or "").strip()
        started_at = row.get("started_at")
        if not metric or not started_at:
            return None
        value = row.get("value")
        if value in ("", None):
            clean_value = None
        else:
            try:
                clean_value = float(value)
            except (TypeError, ValueError):
                clean_value = None
        member_id = row.get("member_id")
        if member_id in ("", None):
            clean_member_id = None
        else:
            try:
                clean_member_id = int(member_id)
            except (TypeError, ValueError):
                clean_member_id = None
        return {
            "metric": metric,
            "started_at": _format_value(started_at),
            "ended_at": _format_value(row.get("ended_at")),
            "value": clean_value,
            "unit": str(row.get("unit") or "").strip() or None,
            "source": str(row.get("source") or "health_auto_export").strip()
            or "health_auto_export",
            "member_id": clean_member_id,
            "metadata": _json_object(row.get("metadata")),
            "raw": _json_object(row.get("raw")),
        }

    def _aggregate_row(self, metric: str, row: Any) -> dict[str, Any]:
        data = _row_dict(row)
        for key in ("sum", "avg", "min", "max"):
            data[key] = float(data[key]) if data.get(key) is not None else None
        data["count"] = int(data.get("count") or 0)
        if metric in _COUNT_METRICS:
            aggregation = "count"
            value = float(data["count"])
        elif metric in _SUM_METRICS or metric.startswith("sleep_"):
            aggregation = "sum"
            value = data.get("sum")
        else:
            aggregation = "avg"
            value = data.get("avg")
        return {"metric": metric, "aggregation": aggregation, "value": value, **data}
