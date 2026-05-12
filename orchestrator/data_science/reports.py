from __future__ import annotations

import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.telemetry import get_logger

from .common import SingleFlightJob, format_ts

logger = get_logger("orchestrator.data_science.reports")


class ReportGenerator(SingleFlightJob):
    def __init__(
        self,
        pool: Any,
        reports_dir: str | Path | None = None,
        llm: Any | None = None,
        model: str | None = None,
        event_log_store: Any | None = None,
    ) -> None:
        super().__init__(job_name="reports", pool=pool, event_log_store=event_log_store)
        raw_dir = reports_dir or os.environ.get("REPORTS_DIR") or "data/reports"
        self.reports_dir = Path(raw_dir)
        self.llm = llm
        self.model = model or os.environ.get("REPORT_MODEL") or os.environ.get("DEFAULT_MODEL")

    async def weekly_report(self) -> dict[str, Any]:
        async def _work() -> dict[str, Any]:
            now = _now()
            iso = now.isocalendar()
            period_label = f"{iso.year}W{iso.week:02d}"
            start = now - timedelta(days=7)
            return await self._generate(
                kind="weekly",
                period_label=period_label,
                filename=f"weekly-{period_label}.md",
                start=start,
                end=now,
            )

        return await self._run_singleflight(_work, job_name="weekly_report")

    async def monthly_report(self) -> dict[str, Any]:
        async def _work() -> dict[str, Any]:
            now = _now()
            period_label = f"{now.year:04d}-{now.month:02d}"
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return await self._generate(
                kind="monthly",
                period_label=period_label,
                filename=f"monthly-{period_label}.md",
                start=start,
                end=now,
            )

        return await self._run_singleflight(_work, job_name="monthly_report")

    async def list_recent_reports(self, limit: int = 10) -> list[dict[str, Any]]:
        if self.pool is None:
            return []
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT kind, period_label, file_path, summary, generated_at
                    FROM reports
                    ORDER BY generated_at DESC
                    LIMIT $1
                    """,
                    int(limit),
                )
            return [_row_dict(row) for row in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("reports_list_failed", error=str(exc))
            return []

    async def get_report(self, kind: str, period_label: str) -> dict[str, Any] | None:
        if self.pool is None:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT kind, period_label, file_path, summary, body_markdown, generated_at
                    FROM reports
                    WHERE kind = $1 AND period_label = $2
                    """,
                    kind,
                    period_label,
                )
            return _row_dict(row) if row else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "report_fetch_failed", kind=kind, period_label=period_label, error=str(exc)
            )
            return None

    async def _generate(
        self,
        *,
        kind: str,
        period_label: str,
        filename: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        data = await self._load_data(start, end)
        markdown = self._render_markdown(kind, period_label, start, end, data)
        narrative = await self._optional_narrative(markdown)
        if narrative:
            markdown = f"{narrative.strip()}\n\n---\n\n{markdown}"

        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_dir / filename
        path.write_text(markdown, encoding="utf-8")
        summary = _summary(data)
        await self._store_report(kind, period_label, path, summary, markdown)
        return {"path": str(path), "markdown": markdown, "summary": summary}

    async def _load_data(self, start: datetime, end: datetime) -> dict[str, Any]:
        data: dict[str, Any] = {
            "events_count": 0,
            "errors_count": 0,
            "top_usage": [],
            "top_errors": [],
            "habits": [],
            "kg_growth": {"things": 0, "habits": 0, "preferences": 0},
            "proposals": [],
            "anomalies": [],
        }
        if self.pool is None:
            return data
        try:
            async with self.pool.acquire() as conn:
                data["events_count"] = int(
                    await _safe_fetchval(
                        conn,
                        """
                        SELECT count(*) FROM event_log
                        WHERE ts >= $1 AND ts < $2
                        """,
                        start,
                        end,
                    )
                    or 0
                )
                data["errors_count"] = int(
                    await _safe_fetchval(
                        conn,
                        """
                        SELECT count(*) FROM event_log
                        WHERE ts >= $1 AND ts < $2
                          AND (
                            capability ILIKE '%error%'
                            OR summary ILIKE '%error%'
                            OR payload::text ILIKE '%error%'
                            OR payload::text ILIKE '%failed%'
                          )
                        """,
                        start,
                        end,
                    )
                    or 0
                )
                data["top_usage"] = await _safe_fetch(
                    conn,
                    """
                    SELECT agent, capability, count(*) AS count
                    FROM event_log
                    WHERE ts >= $1 AND ts < $2
                    GROUP BY agent, capability
                    ORDER BY count DESC, agent, capability
                    LIMIT 3
                    """,
                    start,
                    end,
                )
                data["top_errors"] = await _safe_fetch(
                    conn,
                    """
                    SELECT agent, capability, count(*) AS count
                    FROM event_log
                    WHERE ts >= $1 AND ts < $2
                      AND (
                        capability ILIKE '%error%'
                        OR summary ILIKE '%error%'
                        OR payload::text ILIKE '%error%'
                        OR payload::text ILIKE '%failed%'
                      )
                    GROUP BY agent, capability
                    ORDER BY count DESC, agent, capability
                    LIMIT 3
                    """,
                    start,
                    end,
                )
                data["habits"] = await _safe_fetch(
                    conn,
                    """
                    SELECT subject, confidence, source, created_at
                    FROM habits
                    WHERE created_at >= $1 AND created_at < $2
                    ORDER BY confidence DESC, subject
                    LIMIT 10
                    """,
                    start,
                    end,
                )
                data["kg_growth"] = {
                    "things": int(
                        await _safe_fetchval(
                            conn,
                            """
                            SELECT count(*) FROM things
                            WHERE learned_at >= $1 AND learned_at < $2
                            """,
                            start,
                            end,
                        )
                        or 0
                    ),
                    "habits": int(
                        await _safe_fetchval(
                            conn,
                            """
                            SELECT count(*) FROM habits
                            WHERE created_at >= $1 AND created_at < $2
                            """,
                            start,
                            end,
                        )
                        or 0
                    ),
                    "preferences": int(
                        await _safe_fetchval(
                            conn,
                            """
                            SELECT count(*) FROM preferences
                            WHERE updated_at >= $1 AND updated_at < $2
                            """,
                            start,
                            end,
                        )
                        or 0
                    ),
                }
                data["proposals"] = await _safe_fetch(
                    conn,
                    """
                    SELECT status, title, created_at, resolved_at
                    FROM proposals
                    WHERE status = ANY($3::text[])
                      AND COALESCE(resolved_at, created_at) >= $1
                      AND COALESCE(resolved_at, created_at) < $2
                    ORDER BY COALESCE(resolved_at, created_at) DESC
                    LIMIT 50
                    """,
                    start,
                    end,
                    ["accepted", "dismissed", "auto_confirmed"],
                )
                data["anomalies"] = await _safe_fetch(
                    conn,
                    """
                    SELECT COALESCE(payload->>'phase', capability, 'unknown') AS phase,
                           count(*) AS count
                    FROM event_log
                    WHERE ts >= $1 AND ts < $2
                      AND agent = 'reflector'
                      AND (summary ILIKE '%error%' OR payload::text ILIKE '%error%')
                    GROUP BY phase
                    HAVING count(*) >= 2
                    ORDER BY count DESC, phase
                    LIMIT 10
                    """,
                    start,
                    end,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("report_data_load_failed", error=str(exc))
        return data

    def _render_markdown(
        self,
        kind: str,
        period_label: str,
        start: datetime,
        end: datetime,
        data: dict[str, Any],
    ) -> str:
        proposal_counts = Counter(row.get("status") for row in data["proposals"])
        lines = [
            f"# {kind.title()} report · {period_label}",
            "",
            f"Period: {start.isoformat()} → {end.isoformat()}",
            "",
            "## Headline metrics",
            "",
            f"- Events: {data['events_count']}",
            f"- Errors: {data['errors_count']}",
            "- Top capabilities by usage:",
        ]
        lines.extend(_capability_lines(data["top_usage"]))
        lines.append("- Top capabilities by errors:")
        lines.extend(_capability_lines(data["top_errors"]))
        lines.extend(
            [
                "",
                "## Habits learned this period",
                "",
            ]
        )
        if data["habits"]:
            for row in data["habits"]:
                lines.append(
                    f"- {row.get('subject')} ({float(row.get('confidence') or 0):.2f}, "
                    f"source={row.get('source') or 'unknown'})"
                )
        else:
            lines.append("- None recorded.")
        growth = data["kg_growth"]
        lines.extend(
            [
                "",
                "## Knowledge graph growth",
                "",
                f"- +{growth['things']} things",
                f"- +{growth['habits']} habits",
                f"- +{growth['preferences']} preferences",
                "",
                "## Proposals",
                "",
                f"- Accepted: {proposal_counts.get('accepted', 0)}",
                f"- Dismissed: {proposal_counts.get('dismissed', 0)}",
                f"- Auto-confirmed: {proposal_counts.get('auto_confirmed', 0)}",
            ]
        )
        if data["proposals"]:
            lines.append("- Titles:")
            for row in data["proposals"][:12]:
                lines.append(f"  - [{row.get('status')}] {row.get('title')}")
        lines.extend(["", "## Anomalies", ""])
        if data["anomalies"]:
            for row in data["anomalies"]:
                lines.append(
                    f"- Reflector phase `{row.get('phase')}` errored {row.get('count')} times."
                )
        else:
            lines.append("- No repeated reflector phase errors detected.")
        lines.append("")
        return "\n".join(lines)

    async def _optional_narrative(self, markdown: str) -> str | None:
        if os.environ.get("LLM_REPORT_NARRATION", "false").strip().lower() != "true":
            return None
        llm = self.llm or OllamaClient(os.environ.get("OLLAMA_URL", "http://ollama:11434"))
        try:
            response = await llm.chat(
                model=self.model,
                temperature=0.2,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Write a concise three-paragraph executive narrative "
                            "for this home AI report."
                        ),
                    },
                    {"role": "user", "content": markdown[:6000]},
                ],
            )
            message = response.get("message") if isinstance(response, dict) else None
            if isinstance(message, dict):
                return str(message.get("content") or "").strip() or None
            if isinstance(response, dict):
                return str(response.get("response") or "").strip() or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("report_llm_narration_failed", error=str(exc))
        return None

    async def _store_report(
        self,
        kind: str,
        period_label: str,
        path: Path,
        summary: str,
        markdown: str,
    ) -> None:
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO reports(kind, period_label, file_path, summary, body_markdown)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (kind, period_label)
                    DO UPDATE SET
                        file_path = EXCLUDED.file_path,
                        summary = EXCLUDED.summary,
                        body_markdown = EXCLUDED.body_markdown,
                        generated_at = now()
                    """,
                    kind,
                    period_label,
                    str(path),
                    summary,
                    markdown,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "report_store_failed", kind=kind, period_label=period_label, error=str(exc)
            )


async def _safe_fetch(conn: Any, query: str, *args: Any) -> list[dict[str, Any]]:
    try:
        rows = await conn.fetch(query, *args)
        return [_row_dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("report_query_failed", error=str(exc))
        return []


async def _safe_fetchval(conn: Any, query: str, *args: Any) -> Any:
    try:
        return await conn.fetchval(query, *args)
    except Exception as exc:  # noqa: BLE001
        logger.warning("report_scalar_query_failed", error=str(exc))
        return None


def _row_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        key: format_ts(value) if isinstance(value, datetime) else value
        for key, value in data.items()
    }


def _capability_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["  - none"]
    lines = []
    for row in rows:
        label = f"{row.get('agent') or 'unknown'}.{row.get('capability') or 'unknown'}"
        lines.append(f"  - {label}: {row.get('count')}")
    return lines


def _summary(data: dict[str, Any]) -> str:
    habit_count = data["kg_growth"]["habits"]
    return f"{data['events_count']} events, {data['errors_count']} errors, {habit_count} new habits"


def _now() -> datetime:
    return datetime.now(UTC)
