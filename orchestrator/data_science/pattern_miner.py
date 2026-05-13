from __future__ import annotations

import json
import math
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean, median
from typing import Any

from home_agents_sdk.telemetry import get_logger

from .common import SingleFlightJob, decode_json

logger = get_logger("orchestrator.data_science.pattern_miner")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_NOISE = -1
_UNVISITED = -99


@dataclass(slots=True)
class _Point:
    event_id: int
    ts: datetime
    agent: str
    capability: str
    summary: str
    day_of_week: int
    hour: float


class PatternMiner(SingleFlightJob):
    def __init__(
        self,
        pool: Any,
        knowledge_graph: Any,
        event_log_store: Any | None = None,
    ) -> None:
        super().__init__(job_name="pattern_mining", pool=pool, event_log_store=event_log_store)
        self.knowledge_graph = knowledge_graph

    async def run(self, window_days: int = 30) -> dict[str, Any]:
        self.window_days = max(1, int(window_days or 30))
        return await self._run_singleflight(self._run)

    async def _run(self) -> dict[str, Any]:
        if self.pool is None:
            return {
                "status": "skipped",
                "reason": "postgres_unavailable",
                "candidates": [],
                "stored": 0,
            }

        try:
            rows = await self._load_events(self.window_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pattern_miner_load_failed", error=str(exc))
            return {
                "status": "skipped",
                "reason": "postgres_unavailable",
                "candidates": [],
                "stored": 0,
            }

        points = [point for row in rows if (point := _point_from_row(row))]
        labels = _dbscan(points, eps=1.35, min_points=4)
        candidates = self._candidates_from_clusters(points, labels)

        stored = 0
        for candidate in candidates:
            try:
                if await self._store_candidate(candidate):
                    stored += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "pattern_miner_store_failed",
                    subject=candidate.get("subject"),
                    error=str(exc),
                )
        return {"candidates": candidates, "stored": stored}

    async def _load_events(self, window_days: int) -> list[Any]:
        async with self.pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT id, ts, agent, capability, summary, payload
                    FROM event_log
                    WHERE ts >= now() - ($1::int * interval '1 day')
                      AND agent <> 'data_science'
                      AND agent <> 'dashboard_curator'
                      AND agent NOT LIKE '__orchestrator__%'
                      AND agent NOT LIKE 'observer.%'
                      AND capability NOT IN (
                          'summarize_activity',
                          'summarize_alerts',
                          'reflector.run',
                          'advisor.run'
                      )
                    ORDER BY ts ASC
                    """,
                    window_days,
                )
            )

    def _candidates_from_clusters(
        self,
        points: list[_Point],
        labels: list[int],
    ) -> list[dict[str, Any]]:
        grouped: dict[int, list[_Point]] = {}
        for point, label in zip(points, labels, strict=True):
            if label < 0:
                continue
            grouped.setdefault(label, []).append(point)

        candidates: list[dict[str, Any]] = []
        for cluster_points in grouped.values():
            support_count = len(cluster_points)
            if support_count < 4:
                continue
            agent_counts = Counter(point.agent for point in cluster_points)
            top_agent, top_agent_count = agent_counts.most_common(1)[0]
            purity = top_agent_count / support_count
            if purity < 0.60:
                continue
            agent_points = [point for point in cluster_points if point.agent == top_agent]
            top_capability = Counter(point.capability for point in agent_points).most_common(1)[0][
                0
            ]
            subject = f"{top_agent}.{top_capability}" if top_capability else top_agent
            evidence_ids = sorted(point.event_id for point in cluster_points)
            candidate = {
                "subject": subject,
                "pattern": {
                    "days_of_week": [
                        _DAYS[index] for index in sorted({p.day_of_week for p in cluster_points})
                    ],
                    "time_window_local": _time_window(cluster_points),
                },
                "confidence": _confidence(support_count, purity),
                "evidence_event_ids": evidence_ids,
                "support_count": support_count,
            }
            candidates.append(candidate)

        return sorted(candidates, key=lambda item: (-item["confidence"], item["subject"]))

    async def _store_candidate(self, candidate: dict[str, Any]) -> bool:
        subject = str(candidate["subject"])
        existing = await self._existing_habit(subject)
        if existing is None:
            return await self._insert_habit(candidate)
        return await self._increment_support(existing, candidate)

    async def _existing_habit(self, subject: str) -> dict[str, Any] | None:
        list_habits = getattr(self.knowledge_graph, "list_habits", None)
        if callable(list_habits):
            try:
                rows = await list_habits(subject=subject)
            except TypeError:
                rows = [row for row in await list_habits() if row.get("subject") == subject]
            if rows:
                return dict(rows[0])
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, subject, pattern, frequency, confidence,
                       last_observed_at, source, created_at
                FROM habits
                WHERE subject = $1
                LIMIT 1
                """,
                subject,
            )
        return dict(row) if row else None

    async def _insert_habit(self, candidate: dict[str, Any]) -> bool:
        pattern = _stored_pattern(candidate)
        put_habit = getattr(self.knowledge_graph, "put_habit", None)
        if callable(put_habit):
            row = await put_habit(
                subject=candidate["subject"],
                pattern=pattern,
                frequency="recurring",
                confidence=float(candidate["confidence"]),
                last_observed_at=None,
                source="pattern_miner",
            )
            return row is not None
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                INSERT INTO habits(
                    subject, pattern, frequency, confidence, last_observed_at, source
                )
                VALUES ($1, $2::jsonb, 'recurring', $3, now(), 'pattern_miner')
                """,
                candidate["subject"],
                json.dumps(pattern, default=str),
                float(candidate["confidence"]),
            )
        return str(status).startswith("INSERT")

    async def _increment_support(
        self,
        existing: dict[str, Any],
        candidate: dict[str, Any],
    ) -> bool:
        pattern = decode_json(existing.get("pattern"), {})
        if not isinstance(pattern, dict):
            pattern = {}
        attrs = pattern.setdefault("attributes", {})
        if not isinstance(attrs, dict):
            attrs = {}
            pattern["attributes"] = attrs
        try:
            previous_support = int(attrs.get("support") or 0)
        except (TypeError, ValueError):
            previous_support = 0
        attrs["support"] = previous_support + int(candidate["support_count"])
        attrs["source"] = "pattern_miner"
        attrs["last_evidence_event_ids"] = candidate["evidence_event_ids"]
        attrs["last_pattern"] = candidate["pattern"]

        habit_id = existing.get("id")
        patch_row = getattr(self.knowledge_graph, "patch_row", None)
        if habit_id is not None and callable(patch_row):
            row = await patch_row(
                "habits",
                habit_id,
                {
                    "pattern": pattern,
                    "confidence": max(
                        float(existing.get("confidence") or 0), candidate["confidence"]
                    ),
                    "last_observed_at": datetime.now(UTC).isoformat(),
                    "source": existing.get("source") or "pattern_miner",
                },
            )
            return row is not None

        async with self.pool.acquire() as conn:
            if habit_id is not None:
                status = await conn.execute(
                    """
                    UPDATE habits
                    SET pattern = $2::jsonb,
                        confidence = GREATEST(confidence, $3),
                        last_observed_at = now()
                    WHERE id = $1
                    """,
                    int(habit_id),
                    json.dumps(pattern, default=str),
                    float(candidate["confidence"]),
                )
            else:
                status = await conn.execute(
                    """
                    UPDATE habits
                    SET pattern = $2::jsonb,
                        confidence = GREATEST(confidence, $3),
                        last_observed_at = now()
                    WHERE subject = $1
                    """,
                    candidate["subject"],
                    json.dumps(pattern, default=str),
                    float(candidate["confidence"]),
                )
        return str(status).startswith("UPDATE")


def _point_from_row(row: Any) -> _Point | None:
    data = dict(row)
    ts = _parse_datetime(data.get("ts"))
    if ts is None:
        return None
    try:
        event_id = int(data.get("id"))
    except (TypeError, ValueError):
        return None
    return _Point(
        event_id=event_id,
        ts=ts,
        agent=str(data.get("agent") or "unknown"),
        capability=str(data.get("capability") or ""),
        summary=str(data.get("summary") or ""),
        day_of_week=ts.weekday(),
        hour=ts.hour + (ts.minute / 60.0),
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _distance(left: _Point, right: _Point) -> float:
    hour_delta = abs(left.hour - right.hour)
    hour_delta = min(hour_delta, 24.0 - hour_delta)
    day_delta = abs(left.day_of_week - right.day_of_week)
    day_delta = min(day_delta, 7 - day_delta)
    return math.sqrt(hour_delta**2 + day_delta**2)


def _dbscan(points: list[_Point], *, eps: float, min_points: int) -> list[int]:
    labels = [_UNVISITED] * len(points)
    cluster_id = 0
    for index in range(len(points)):
        if labels[index] != _UNVISITED:
            continue
        neighbors = _neighbors(points, index, eps)
        if len(neighbors) < min_points:
            labels[index] = _NOISE
            continue
        labels[index] = cluster_id
        queue = deque(neighbors)
        while queue:
            neighbor = queue.popleft()
            if labels[neighbor] == _NOISE:
                labels[neighbor] = cluster_id
            if labels[neighbor] != _UNVISITED:
                continue
            labels[neighbor] = cluster_id
            expanded = _neighbors(points, neighbor, eps)
            if len(expanded) >= min_points:
                queue.extend(expanded)
        cluster_id += 1
    return labels


def _neighbors(points: list[_Point], index: int, eps: float) -> list[int]:
    return [other for other, point in enumerate(points) if _distance(points[index], point) <= eps]


def _time_window(points: list[_Point]) -> str:
    center = median(point.hour for point in points)
    spread = max(1.0, mean(abs(point.hour - center) for point in points) * 2)
    start_hour = (center - spread / 2) % 24
    end_hour = (center + spread / 2) % 24
    return f"{_format_hour(start_hour)}-{_format_hour(end_hour)}"


def _format_hour(hour: float) -> str:
    total_minutes = int(round(hour * 60)) % (24 * 60)
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _confidence(support_count: int, purity: float) -> float:
    return round(min(0.95, (0.35 + support_count / 20.0) * purity), 3)


def _stored_pattern(candidate: dict[str, Any]) -> dict[str, Any]:
    pattern = dict(candidate["pattern"])
    pattern["attributes"] = {
        "support": candidate["support_count"],
        "evidence_event_ids": candidate["evidence_event_ids"],
        "source": "pattern_miner",
    }
    return pattern
