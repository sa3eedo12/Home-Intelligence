"""Auto-complete recurring chores from observed appliance events.

Vacuum and washer observers already emit `cleaning.completed` and
`appliance.cycle_completed` envelopes onto the `events.observed` stream.
This consumer reads that stream alongside the reactive trigger consumer
and, for each matching event, looks up the chore template that declares
the matching `auto_detect_kind` and inserts a chore_log row.

We do NOT replace the existing reactive trigger — that still fires the
"infer_cleaning_run" or "infer_cycle_load" capability for downstream
analytics. This is purely a side-effect: when the appliance reports
done, the chore closes itself.

Dedup is handled by ChoreStore (we just rely on a 10-minute event-level
window in the observers themselves; if the same vacuum run somehow
emits twice we'll log twice, but that's harmless — last completion
wins for the cadence math).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import asyncpg
from home_agents_sdk.chore_store import ChoreStore
from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis
from redis.exceptions import ResponseError


logger = get_logger("orchestrator.chore_auto_detect")

STREAM = "events.observed"
GROUP = "orchestrator:chore_auto"
CONSUMER = "chore-auto-1"

# Observed event kinds → auto_detect_kind. Keep this tight; the cost
# of a wrong match is "system marked the wrong chore done", which the
# user can undo from the dashboard.
KIND_TO_DETECT: dict[str, str] = {
    "cleaning.completed": "vacuum",
    "appliance.cycle_completed": "washer",
}


async def run_consumer(
    *,
    redis: Redis,
    pool: asyncpg.Pool,
    store: ChoreStore | None = None,
    poll_block_ms: int = 2000,
) -> None:
    """Long-lived consumer task. Mirrors the shape of notify.run_consumer."""
    chore_store = store or ChoreStore(pool)
    try:
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    while True:
        try:
            messages = await redis.xreadgroup(
                groupname=GROUP, consumername=CONSUMER,
                streams={STREAM: ">"}, count=10, block=poll_block_ms,
            )
            for stream_name, entries in messages:
                for message_id, fields in entries:
                    try:
                        await _process_one(fields, chore_store)
                    except Exception as exc:
                        logger.warning(
                            "chore_auto_process_failed",
                            message_id=message_id, error=str(exc),
                        )
                    finally:
                        await redis.xack(stream_name, GROUP, message_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("chore_auto_consumer_error", error=str(exc))
            await asyncio.sleep(1)


async def _process_one(
    fields: dict[Any, Any], store: ChoreStore
) -> None:
    """Inspect one stream entry. If it's an auto-detect-eligible kind,
    mark the matching chore done."""
    kind = _decode(fields.get("kind"))
    detect_kind = KIND_TO_DETECT.get(kind or "")
    if not detect_kind:
        return
    payload = _decode_json(fields.get("payload"))
    inner = payload.get("payload") if isinstance(payload, dict) else None
    if not isinstance(inner, dict):
        inner = payload if isinstance(payload, dict) else {}
    entity_id = str(inner.get("entity_id") or "") or None
    appliance = str(inner.get("appliance") or "")
    if detect_kind == "washer" and appliance and appliance != "washer":
        # appliance.cycle_completed fires for dryer/dishwasher/oven too;
        # only the washer chore should auto-close on washer events.
        return
    matched = await _find_template(store, detect_kind=detect_kind,
                                    entity_id=entity_id)
    if not matched:
        return
    note = f"auto-detected from {detect_kind}"
    if entity_id:
        note += f" ({entity_id})"
    log_id = await store.log_completion(
        template_id=matched["id"],
        source=f"auto_{detect_kind}",
        note=note,
    )
    if log_id is not None:
        logger.info(
            "chore_auto_completed",
            template_id=matched["id"], name=matched["name"],
            detect_kind=detect_kind, entity_id=entity_id,
        )


async def _find_template(
    store: ChoreStore, *, detect_kind: str, entity_id: str | None,
) -> dict[str, Any] | None:
    """First try an exact entity match, then fall back to the first
    template whose auto_detect_kind matches."""
    templates = await store.list_templates(active_only=True)
    if entity_id:
        for t in templates:
            if (t.get("auto_detect_entity") == entity_id
                and t.get("auto_detect_kind") == detect_kind):
                return t
    for t in templates:
        if (t.get("auto_detect_kind") == detect_kind
            and not t.get("auto_detect_entity")):
            return t
    return None


def _decode(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _decode_json(value: Any) -> Any:
    raw = _decode(value)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
