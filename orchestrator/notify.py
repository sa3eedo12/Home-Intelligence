from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from .policy_engine import NotifyPayload, PolicyEngine

logger = get_logger("notify")

STREAM = "notify.outbound"
GROUP = "orchestrator:notify"


def _to_payload(raw: dict[str, Any], default_chat_id: int) -> NotifyPayload:
    chat_id = int(raw.get("chat_id") or default_chat_id)
    return NotifyPayload(
        chat_id=chat_id,
        text=str(raw.get("text", "")),
        severity=str(raw.get("severity", "info")),
        topic=raw.get("topic"),
        agent=raw.get("agent"),
        capability=raw.get("capability"),
        keyboard=raw.get("keyboard"),
        fingerprint=raw.get("fingerprint"),
    )


def _brief_lines(brief: dict[str, Any]) -> list[str]:
    body = brief.get("body_json") or {}
    summary = str(brief.get("summary") or body.get("summary") or "Morning Brief")
    lines = ["*🌅 Morning Brief*", "", summary]

    # Inferences captured in the last 24h (washer/vacuum/sleep/presence/tv).
    # Brief enrichment: only surface confirmed labels — the unconfirmed
    # ones are already in 'questions_for_you' below.
    learned = _learned_yesterday(brief)
    if learned:
        lines.extend(["", "*🧠 What I learned about you*"])
        for line in learned[:8]:
            lines.append(f"- {line}")

    # Cross-source correlations the night uncovered (HR drift vs sleep,
    # late-TV → bad sleep, coffee→HR, step trends, wake drift).
    correlations = _correlations_section(brief)
    if correlations:
        lines.extend(["", "*🔍 I noticed*"])
        for line in correlations[:6]:
            lines.append(f"- {line}")

    # Anomalies the night detected (vacuum overdue, etc.)
    anomalies = _anomalies_24h(brief)
    if anomalies:
        lines.extend(["", "*🔔 What I noticed*"])
        for line in anomalies[:5]:
            lines.append(f"- {line}")

    sections = [
        ("Questions for you", body.get("questions_for_you") or []),
        ("Suggestions for me", body.get("suggestions_for_me") or []),
    ]
    for title, items in sections:
        if not items:
            continue
        lines.extend(["", f"*{title}*"])
        for item in items[:5]:
            if isinstance(item, dict):
                text = item.get("title") or item.get("summary") or item.get("rationale") or item
            else:
                text = item
            lines.append(f"- {str(text)[:240]}")
    lines.extend(["", "_Tap a notification to confirm or correct any guess._"])
    return lines


def _learned_yesterday(brief: dict[str, Any]) -> list[str]:
    """Mine the brief body for interesting things that happened. Today this
    pulls from 'evidence.events' (filtered by the reflector) plus any inline
    inference summaries the brief carried."""
    body = brief.get("body_json") or {}
    events = (body.get("evidence") or {}).get("events") or []
    out: list[str] = []
    seen_kinds: dict[str, int] = {}
    for ev in events:
        cap = str(ev.get("capability") or "")
        # Surface user-meaningful events only
        keep = (
            cap.endswith(".cycle_completed")
            or cap == "cleaning.completed"
            or cap == "coffee.brewed"
            or cap == "presence.changed"
            or cap == "sleep.likely_asleep"
            or cap == "sleep.likely_awake"
            or cap == "entertainment.left_on"
        )
        if not keep:
            continue
        seen_kinds[cap] = seen_kinds.get(cap, 0) + 1
    for cap, count in sorted(seen_kinds.items(), key=lambda kv: -kv[1]):
        if cap.endswith(".cycle_completed"):
            out.append(f"Washer cycle ran {count}× yesterday")
        elif cap == "cleaning.completed":
            out.append(f"Vacuum cleaned {count}× yesterday")
        elif cap == "coffee.brewed":
            out.append(f"Coffee brewed {count}× yesterday")
        elif cap == "sleep.likely_asleep":
            out.append("You went to sleep at your usual time")
        elif cap == "sleep.likely_awake":
            out.append("You woke up at your usual time")
        elif cap == "entertainment.left_on":
            out.append(f"TV was left on for a stretch {count}×")
        elif cap == "presence.changed":
            # Too noisy to show one line per — surface only if many
            if count >= 3:
                out.append(f"{count} 'home/away' events recorded")
    return out


def _anomalies_24h(brief: dict[str, Any]) -> list[str]:
    body = brief.get("body_json") or {}
    events = (body.get("evidence") or {}).get("events") or []
    out: list[str] = []
    for ev in events:
        if str(ev.get("capability") or "") == "anomaly.detected":
            payload = ev.get("payload") or {}
            kind = str(payload.get("anomaly_type") or "anomaly")
            summary = str(ev.get("summary") or kind)
            out.append(summary)
    return out


def _correlations_section(brief: dict[str, Any]) -> list[str]:
    """Render the cross-source correlation insights produced by the
    correlations phase. Each entry is `{headline, detail?, confidence}`."""
    body = brief.get("body_json") or {}
    items = body.get("correlations") or []
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        headline = str(item.get("headline") or "").strip()
        if not headline:
            continue
        detail = item.get("detail")
        if detail:
            out.append(f"{headline} — {str(detail)[:160]}")
        else:
            out.append(headline[:200])
    return out


async def send_morning_brief(tg_app: Any, brief: dict[str, Any], chat_id: int) -> None:
    text = "\n".join(_brief_lines(brief))
    if len(text) > 4000:
        text = text[:3990].rstrip() + "\n…"
    try:
        await tg_app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as exc:
        logger.warning("morning_brief_markdown_send_failed", error=str(exc))
        await tg_app.bot.send_message(chat_id=chat_id, text=text)


async def run_consumer(
    redis: Redis, policy_engine: PolicyEngine, send_fn: Callable[..., Any]
) -> None:
    consumer = "orchestrator-notify-1"

    try:
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    default_chat_id = int((await redis.get("config:telegram_chat_id")) or "0")

    while True:
        try:
            messages = await redis.xreadgroup(
                groupname=GROUP,
                consumername=consumer,
                streams={STREAM: ">"},
                count=10,
                block=1000,
            )
            for stream_name, entries in messages:
                for message_id, fields in entries:
                    payload_raw = json.loads(fields.get("payload", "{}"))
                    payload = _to_payload(payload_raw, default_chat_id)
                    decision = await policy_engine.evaluate(payload)
                    record = {
                        "ts": datetime.now(UTC).isoformat(),
                        "topic": payload.topic,
                        "severity": payload.severity,
                        "agent": payload.agent,
                        "decision": decision.action,
                        "reason": decision.reason,
                        "text": payload.text[:250],
                    }
                    try:
                        if decision.action == "send":
                            await send_fn(payload.chat_id, payload.text, payload.keyboard)
                        elif decision.action == "rollup" and decision.rollup_text:
                            await redis.xadd(
                                STREAM,
                                {
                                    "payload": json.dumps(
                                        {
                                            "chat_id": payload.chat_id,
                                            "text": decision.rollup_text,
                                            "severity": payload.severity,
                                            "topic": payload.topic,
                                            "agent": payload.agent,
                                            "capability": payload.capability,
                                        }
                                    )
                                },
                            )
                    except Exception as exc:
                        logger.warning("notify_send_failed", error=str(exc))
                    finally:
                        await redis.lpush("policy:recent", json.dumps(record))
                        await redis.ltrim("policy:recent", 0, 99)
                        await redis.xack(stream_name, GROUP, message_id)
        except Exception as exc:
            logger.warning("notify_consumer_error", error=str(exc))
