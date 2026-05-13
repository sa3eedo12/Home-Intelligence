from __future__ import annotations

import asyncio
import json
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml
from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from .policy_engine import SEVERITY_ORDER

logger = get_logger("reactive")


class Reactive:
    def __init__(self, registry: Any, redis: Redis, triggers_path: str) -> None:
        self._registry = registry
        self._redis = redis
        self._triggers_path = Path(triggers_path)
        self._triggers: list[dict[str, Any]] = []
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.reload()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def reload(self) -> dict[str, int]:
        await self.stop()
        data = yaml.safe_load(self._triggers_path.read_text(encoding="utf-8")) or {}
        self._triggers = data.get("triggers", [])
        for trigger in self._triggers:
            self._tasks.append(asyncio.create_task(self._consume_trigger(trigger)))
        return {"triggers": len(self._triggers)}

    async def _consume_trigger(self, trigger: dict[str, Any]) -> None:
        stream = trigger.get("stream", "events.home")
        trigger_id = trigger.get("id", "default")
        group = f"orchestrator:reactive:{trigger_id}"
        consumer = f"reactive:{trigger_id}"

        try:
            await self._redis.xgroup_create(stream, group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

        while True:
            try:
                batches = await self._redis.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={stream: ">"},
                    count=10,
                    block=1000,
                )
                for stream_name, entries in batches:
                    for message_id, fields in entries:
                        try:
                            payload = json.loads(fields.get("payload", "{}"))
                            await self.handle_event(trigger, payload)
                        except Exception as exc:
                            logger.warning(
                                "reactive_message_error", trigger=trigger_id, error=str(exc)
                            )
                        finally:
                            await self._redis.xack(stream_name, group, message_id)
            except Exception as exc:
                logger.warning("reactive_consumer_error", trigger=trigger_id, error=str(exc))

    def _build_dispatch_inputs(
        self, dispatch: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge static ``dispatch.inputs`` with optional ``inputs_from`` payload key.

        ``inputs_from: payload`` passes the entire observer payload as the
        capability's arguments. ``inputs_from: payload.<field>`` picks a
        sub-dict from the payload. Static ``inputs`` keys override anything
        sourced from the payload.
        """
        static = dispatch.get("inputs") or {}
        inputs_from = dispatch.get("inputs_from")
        if not inputs_from:
            return dict(static) if isinstance(static, dict) else {}
        source: Any = payload
        if isinstance(inputs_from, str):
            # "payload" → use the payload itself; "payload.x.y" → walk into payload["x"]["y"]
            parts = inputs_from.split(".")
            if parts and parts[0] == "payload":
                parts = parts[1:]
            for part in parts:
                if isinstance(source, dict):
                    source = source.get(part, {})
                else:
                    source = {}
        merged = dict(source) if isinstance(source, dict) else {}
        if isinstance(static, dict):
            merged.update(static)
        return merged

    async def handle_event(self, trigger: dict[str, Any], payload: dict[str, Any]) -> None:
        if not self._matches(trigger.get("match", {}), payload):
            return

        dispatch = trigger.get("dispatch")
        result: dict[str, Any] = {}
        if dispatch:
            inputs = self._build_dispatch_inputs(dispatch, payload)
            result = await self._registry.dispatch(
                dispatch.get("agent", ""),
                dispatch.get("capability", ""),
                inputs,
            )

        notify_from_result = trigger.get("notify_from_result")
        if notify_from_result:
            output = result.get("result") if isinstance(result, dict) else {}
            if not isinstance(output, dict):
                # Some dispatch paths return the result directly (no "result" wrapper)
                output = result if isinstance(result, dict) else {}
            text_field = notify_from_result.get("text_field", "summary")
            text = str(output.get(text_field, "")) if isinstance(output, dict) else str(output)
            keyboard: Any = notify_from_result.get("keyboard")
            keyboard_field = notify_from_result.get("keyboard_field")
            if keyboard_field and isinstance(output, dict):
                # Output field overrides any static keyboard set in the yaml
                keyboard = output.get(keyboard_field) or keyboard
            if text:
                await self._redis.xadd(
                    "notify.outbound",
                    {
                        "payload": json.dumps(
                            {
                                "text": text,
                                "topic": notify_from_result.get("topic"),
                                "severity": notify_from_result.get("severity", "info"),
                                "agent": dispatch.get("agent") if dispatch else None,
                                "capability": dispatch.get("capability") if dispatch else None,
                                "keyboard": keyboard,
                            }
                        )
                    },
                )

        notify_from_payload = trigger.get("notify_from_payload")
        if notify_from_payload:
            template = notify_from_payload.get("text_template", "{payload}")
            topic_template = notify_from_payload.get("topic_template", "events.system")
            text = template.format(**payload)
            topic = topic_template.format(**payload)
            severity = payload.get(notify_from_payload.get("severity_field", "severity"), "info")
            keyboard_field = notify_from_payload.get("keyboard_field")
            keyboard = payload.get(keyboard_field) if keyboard_field else None
            await self._redis.xadd(
                "notify.outbound",
                {
                    "payload": json.dumps(
                        {
                            "text": text,
                            "topic": topic,
                            "severity": severity,
                            "agent": payload.get("agent"),
                            "keyboard": keyboard,
                        }
                    )
                },
            )

    def _matches(self, match: dict[str, Any], payload: dict[str, Any]) -> bool:
        for key, value in match.items():
            if key == "severity_min":
                current = str(payload.get("severity", "debug")).lower()
                if SEVERITY_ORDER.index(current) < SEVERITY_ORDER.index(str(value).lower()):
                    return False
                continue
            if isinstance(value, str) and "*" in value:
                if not fnmatch(str(payload.get(key, "")), value):
                    return False
            elif payload.get(key) != value:
                return False
        return True
