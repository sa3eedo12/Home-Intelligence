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
    sections = [
        ("Yesterday", body.get("yesterday") or []),
        ("Questions for you", body.get("questions_for_you") or []),
        ("Suggestions for me", body.get("suggestions_for_me") or []),
        ("Code wishlist", body.get("code_wishlist") or []),
    ]
    for title, items in sections:
        lines.extend(["", f"*{title}*"])
        if not items:
            lines.append("- None")
            continue
        for item in items[:5]:
            if isinstance(item, dict):
                text = item.get("title") or item.get("summary") or item.get("rationale") or item
            else:
                text = item
            lines.append(f"- {str(text)[:240]}")
    return lines


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
