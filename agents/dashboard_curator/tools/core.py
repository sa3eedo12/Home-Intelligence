from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from home_agents_sdk import tool
from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis

logger = get_logger("dashboard_curator")

ACTIVITY_STREAM = "events.activity"
DASHBOARD_STREAM = "dashboard.updates"
NOTIFY_HISTORY = "policy:recent"
NARRATIVE_KEY = "dashboard:narrative"
ALERT_NARRATIVE_KEY = "dashboard:alert_narrative"
AGENT_CARD_KEY_PREFIX = "dashboard:agent_card:"
NARRATIVE_TTL_SECONDS = 600


def _redis_client() -> Redis:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    return Redis.from_url(redis_url, decode_responses=True)


def _llm_client() -> OllamaClient:
    return OllamaClient(os.getenv("OLLAMA_URL", "http://ollama:11434"))


def _default_model() -> str:
    return os.getenv("DEFAULT_MODEL", "qwen3-8b-16k")


def _narrative_model() -> str:
    """Model used for the chatty per-minute narrative summaries.

    Defaults to the small qwen3-0.6b-4k model (already warmed by the orchestrator)
    so the 60-second scheduled cycle doesn't pile up behind 30s LLM calls and
    blow the dispatch timeout. Override via DASHBOARD_NARRATIVE_MODEL for
    higher fidelity at the cost of latency.
    """
    return os.getenv("DASHBOARD_NARRATIVE_MODEL", "qwen3-0.6b-4k")


def _ms_since(iso_ts: str) -> float:
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds() * 1000.0


async def _read_recent_activity(client: Redis, window_minutes: int) -> list[dict[str, Any]]:
    cutoff_ms = int((datetime.now(UTC) - timedelta(minutes=window_minutes)).timestamp() * 1000)
    min_id = f"{cutoff_ms}-0"
    raw = await client.xrange(ACTIVITY_STREAM, min=min_id, count=2000)
    events: list[dict[str, Any]] = []
    for _msg_id, fields in raw:
        try:
            payload = json.loads(fields.get("payload", "{}"))
        except (TypeError, ValueError):
            continue
        events.append(payload)
    return events


async def _read_recent_notifications(client: Redis, limit: int = 50) -> list[dict[str, Any]]:
    raw = await client.lrange(NOTIFY_HISTORY, 0, limit - 1)
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except (TypeError, ValueError):
            continue
    return out


async def _publish_dashboard_update(
    client: Redis, update_type: str, record: dict[str, Any]
) -> None:
    payload = {
        "type": update_type,
        "agent": "dashboard_curator",
        "generated_at": datetime.now(UTC).isoformat(),
        "record": record,
    }
    try:
        await client.xadd(DASHBOARD_STREAM, {"payload": json.dumps(payload, default=str)})
    except Exception as exc:
        logger.warning("dashboard_update_publish_failed", error=str(exc), update_type=update_type)


def _aggregate(events: list[dict[str, Any]]) -> dict[str, Any]:
    per_agent: dict[str, Counter[str]] = {}
    durations: dict[str, list[float]] = {}
    errors: list[dict[str, Any]] = []
    for event in events:
        agent = event.get("agent") or "unknown"
        status = event.get("status") or "unknown"
        per_agent.setdefault(agent, Counter())[status] += 1
        if status == "ok":
            durations.setdefault(agent, []).append(float(event.get("duration_ms") or 0.0))
        if status == "error":
            errors.append(event)
    summary = []
    for agent, counts in sorted(per_agent.items()):
        d = durations.get(agent, [])
        avg_ms = round(sum(d) / len(d), 1) if d else 0.0
        summary.append(
            {
                "agent": agent,
                "ok": counts.get("ok", 0),
                "errors": counts.get("error", 0),
                "started": counts.get("started", 0),
                "avg_ms": avg_ms,
            }
        )
    return {
        "by_agent": summary,
        "total_events": len(events),
        "total_errors": sum(c.get("error", 0) for c in per_agent.values()),
        "recent_errors": errors[-5:],
    }


def _template_narrative(window_minutes: int, agg: dict[str, Any]) -> str:
    if agg["total_events"] == 0:
        return f"No agent activity in the last {window_minutes} minutes."
    lines = [f"In the last {window_minutes} minutes:"]
    for row in agg["by_agent"]:
        ok = row["ok"]
        errors = row["errors"]
        bits = []
        if ok:
            bits.append(f"{ok} OK")
        if errors:
            bits.append(f"{errors} error{'s' if errors != 1 else ''}")
        if not bits:
            continue
        lines.append(f"- **{row['agent']}**: {', '.join(bits)} (avg {row['avg_ms']:.0f} ms)")
    if agg["total_errors"] == 0:
        lines.append("All systems nominal.")
    else:
        lines.append(f"⚠️ {agg['total_errors']} error(s) in window — see activity feed.")
    return "\n".join(lines)


async def _llm_narrative(window_minutes: int, agg: dict[str, Any]) -> str | None:
    """Try to get an LLM-narrated summary; return None on any failure."""
    client = _llm_client()
    facts = json.dumps(agg, ensure_ascii=False)
    system = (
        "You are the Home Intelligence dashboard narrator. "
        "Write a concise (max 4 sentences) Markdown summary of recent agent activity. "
        "Be factual, friendly, and lead with the headline. Do not invent data not in facts."
    )
    user = (
        f"Window: last {window_minutes} minutes.\n"
        f"Facts (JSON): {facts}\n"
        "Write the summary now."
    )
    try:
        resp = await client.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=_narrative_model(),
            temperature=0.3,
        )
    except Exception as exc:
        logger.warning("curator_llm_unavailable", error=str(exc))
        return None
    message = resp.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if not content:
        return None
    return str(content).strip()


@tool("summarize_activity")
async def summarize_activity(window_minutes: int = 15) -> dict[str, Any]:
    client = _redis_client()
    try:
        events = await _read_recent_activity(client, window_minutes)
        agg = _aggregate(events)
        narrative = await _llm_narrative(window_minutes, agg)
        if not narrative:
            narrative = _template_narrative(window_minutes, agg)
        record = {
            "narrative": narrative,
            "window_minutes": window_minutes,
            "generated_at": datetime.now(UTC).isoformat(),
            "stats": agg,
        }
        await client.set(NARRATIVE_KEY, json.dumps(record), ex=NARRATIVE_TTL_SECONDS)
        await _publish_dashboard_update(client, "activity.summary", record)
        return record
    finally:
        await client.aclose()


@tool("summarize_alerts")
async def summarize_alerts(window_minutes: int = 60) -> dict[str, Any]:
    client = _redis_client()
    try:
        all_recent = await _read_recent_notifications(client, limit=100)
        cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)
        relevant: list[dict[str, Any]] = []
        for item in all_recent:
            sev = str(item.get("severity", "")).lower()
            if sev not in {"warn", "alert", "critical"}:
                continue
            ts_raw = item.get("ts")
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                    if ts < cutoff:
                        continue
                except ValueError:
                    pass
            relevant.append(item)

        if not relevant:
            narrative = f"No warnings or alerts in the last {window_minutes} minutes."
        else:
            try:
                resp = await _llm_client().chat(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Summarize these home-system alerts in <= 3 sentences. "
                                "Lead with severity. Output Markdown."
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(relevant[:20], ensure_ascii=False),
                        },
                    ],
                    model=_narrative_model(),
                    temperature=0.2,
                )
                content = (resp.get("message") or {}).get("content")
                narrative = (
                    str(content).strip()
                    if content
                    else f"{len(relevant)} alert(s) in last {window_minutes} min."
                )
            except Exception as exc:
                logger.warning("curator_alert_llm_unavailable", error=str(exc))
                narrative = (
                    f"⚠️ {len(relevant)} alert(s) in last {window_minutes} min "
                    f"(LLM unavailable for narration)."
                )

        record = {
            "narrative": narrative,
            "window_minutes": window_minutes,
            "generated_at": datetime.now(UTC).isoformat(),
            "alert_count": len(relevant),
        }
        await client.set(ALERT_NARRATIVE_KEY, json.dumps(record), ex=NARRATIVE_TTL_SECONDS)
        await _publish_dashboard_update(client, "alerts.summary", record)
        return record
    finally:
        await client.aclose()


@tool("agent_card")
async def agent_card(agent: str, window_minutes: int = 15) -> dict[str, Any]:
    client = _redis_client()
    try:
        events = await _read_recent_activity(client, window_minutes)
        events = [e for e in events if e.get("agent") == agent]
        agg = _aggregate(events)
        if not events:
            line = f"{agent} has been idle for the last {window_minutes} minutes."
        else:
            row = next((r for r in agg["by_agent"] if r["agent"] == agent), None)
            if row is None:
                line = f"{agent}: no recent activity in window."
            else:
                line = (
                    f"{agent}: {row['ok']} ok, {row['errors']} error(s), "
                    f"avg {row['avg_ms']:.0f} ms in last {window_minutes} min."
                )
        record = {
            "agent": agent,
            "line": line,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        await client.set(
            f"{AGENT_CARD_KEY_PREFIX}{agent}",
            json.dumps(record),
            ex=NARRATIVE_TTL_SECONDS,
        )
        return record
    finally:
        await client.aclose()
