from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

PENDING_TTL_SECONDS = 5 * 60


def _pending_key(chat_id: int | str) -> str:
    return f"pending:chat:{chat_id}"


async def set_pending(redis: Redis, chat_id: int | str, payload: dict[str, Any]) -> None:
    stored = {
        "agent": payload.get("agent"),
        "capability": payload.get("capability"),
        "inputs": payload.get("inputs") or {},
        "reason": payload.get("reason") or "",
        "created_at": payload.get("created_at") or datetime.now(UTC).isoformat(),
        "prompt_text": payload.get("prompt_text") or payload.get("reason") or "",
    }
    await redis.set(
        _pending_key(chat_id),
        json.dumps(stored, ensure_ascii=False, default=str),
        ex=PENDING_TTL_SECONDS,
    )


async def get_pending(redis: Redis, chat_id: int | str) -> dict[str, Any] | None:
    raw = await redis.get(_pending_key(chat_id))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await clear_pending(redis, chat_id)
        return None
    return data if isinstance(data, dict) else None


async def clear_pending(redis: Redis, chat_id: int | str) -> None:
    await redis.delete(_pending_key(chat_id))
