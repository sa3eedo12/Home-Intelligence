from __future__ import annotations

import hashlib
import json
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from fnmatch import fnmatch
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

SEVERITY_ORDER = ["debug", "info", "notice", "warn", "alert", "critical"]
SLEEP_WINDOW_CACHE_TTL_SECONDS = 5 * 60


@dataclass(slots=True)
class NotifyPayload:
    chat_id: int
    text: str
    severity: str = "info"
    topic: str | None = None
    agent: str | None = None
    capability: str | None = None
    keyboard: Any = None
    fingerprint: str | None = None


@dataclass(slots=True)
class Decision:
    action: str
    reason: str
    rollup_text: str | None = None


class PolicyEngine:
    def __init__(
        self,
        policies: dict[str, Any],
        redis: Redis,
        now_fn: Callable[[], datetime] | None = None,
        pool: Any | None = None,
    ) -> None:
        self._policies = policies
        self._redis = redis
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._pool = pool
        # Cache for household sleep windows. None means 'never loaded';
        # an empty list means 'loaded, but the household has none configured'.
        self._sleep_windows: list[tuple[time, time]] | None = None
        self._sleep_windows_loaded_at: float = 0.0

    @property
    def policies(self) -> dict[str, Any]:
        return self._policies

    async def reload(self, new_policies: dict[str, Any]) -> None:
        self._policies = new_policies

    async def evaluate(self, payload: NotifyPayload) -> Decision:
        severity = (payload.severity or "info").lower()

        if severity == "critical":
            await self._bump_stat("sent")
            return Decision(action="send", reason="critical_bypass")

        if await self._is_muted(payload):
            await self._bump_stat("suppressed")
            await self._bump_stat("suppressed.mute")
            return Decision(action="suppress", reason="manual_mute")

        fingerprint = payload.fingerprint or self._fingerprint(payload)

        rate_decision = await self._rate_limit(payload)
        if rate_decision.action != "send":
            await self._bump_stat(rate_decision.action)
            await self._bump_stat(f"{rate_decision.action}.rate_limit")
            return rate_decision

        if await self.quiet_hours_active() and not self._quiet_pass(payload):
            await self._bump_stat("suppressed")
            await self._bump_stat("suppressed.quiet_hours")
            return Decision(action="suppress", reason="quiet_hours")

        if not await self._dedupe(payload, fingerprint):
            await self._bump_stat("suppressed")
            await self._bump_stat("suppressed.dedupe")
            return Decision(action="suppress", reason="dedupe")

        await self._bump_stat("sent")
        return Decision(action="send", reason="allowed")

    async def _bump_stat(self, key: str) -> None:
        await self._redis.hincrby("policy:stats", key, 1)

    def _quiet_hours_active(self, now: datetime) -> bool:
        """True if NOW falls inside any active quiet window.

        Window sources, in order of precedence:
          1. household_members.sleep_time/wake_time (loaded async via
             ``_member_sleep_windows`` and cached). When at least one
             member has a sleep_time set, the union of all member windows
             defines quiet hours and the static config below is IGNORED.
             This stops the policy engine from muting at 22:30 just
             because that's the YAML default, when the user actually
             goes to bed at 00:30.
          2. Static config in ``policies.yaml`` quiet_hours.start/end —
             only used when no household members have sleep_time set
             (fresh install) or the DB is unreachable.

        Either source can be disabled by setting quiet_hours.enabled=false.
        """
        cfg = self._policies.get("quiet_hours", {})
        if not cfg.get("enabled", False):
            return False

        tz_name = cfg.get("tz", "Asia/Dubai")
        local = now.astimezone(ZoneInfo(tz_name))
        local_time = local.time().replace(tzinfo=None)

        member_windows = self._sleep_windows
        if member_windows:
            return any(
                _time_is_in_sleep_window(local_time, sleep, wake)
                for sleep, wake in member_windows
            )

        # Static fallback (no member data available)
        start_s = cfg.get("start", "22:30")
        end_s = cfg.get("end", "07:00")
        start_h, start_m = (int(x) for x in start_s.split(":", 1))
        end_h, end_m = (int(x) for x in end_s.split(":", 1))
        return _time_is_in_sleep_window(
            local_time,
            time(start_h, start_m),
            time(end_h, end_m),
        )

    async def _refresh_member_sleep_windows(self) -> None:
        """Pull (sleep_time, wake_time) for every non-pet member with a
        sleep_time set. Cached for 5 minutes — DB queries on the hot
        notification path would add latency for no benefit."""
        if self._pool is None:
            self._sleep_windows = []
            self._sleep_windows_loaded_at = _time.monotonic()
            return
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT sleep_time, wake_time
                      FROM household_members
                     WHERE sleep_time IS NOT NULL
                       AND role <> 'pet'
                    """
                )
        except Exception:
            # Don't override an existing cache on transient DB failures.
            if self._sleep_windows is None:
                self._sleep_windows = []
            self._sleep_windows_loaded_at = _time.monotonic()
            return
        windows: list[tuple[time, time]] = []
        for row in rows:
            sleep = row["sleep_time"]
            wake = row["wake_time"] or time(7, 0)
            if isinstance(sleep, time) and isinstance(wake, time):
                windows.append((sleep, wake))
        self._sleep_windows = windows
        self._sleep_windows_loaded_at = _time.monotonic()

    async def _ensure_sleep_windows_fresh(self) -> None:
        if (
            self._sleep_windows is None
            or _time.monotonic() - self._sleep_windows_loaded_at
            > SLEEP_WINDOW_CACHE_TTL_SECONDS
        ):
            await self._refresh_member_sleep_windows()

    def _quiet_pass(self, payload: NotifyPayload) -> bool:
        topic = payload.topic or ""
        capability = payload.capability or ""
        severity = (payload.severity or "info").lower()
        for rule in self._policies.get("allow_during_quiet", []):
            if rule.get("severity") and rule["severity"].lower() == severity:
                return True
            topic_pattern = rule.get("topic_pattern")
            if topic_pattern and fnmatch(topic, topic_pattern):
                return True
            cap_pattern = rule.get("capability_pattern")
            if cap_pattern and fnmatch(capability, cap_pattern):
                return True
        return False

    async def _is_muted(self, payload: NotifyPayload) -> bool:
        keys: list[str] = []
        if payload.agent:
            keys.append(f"policy:mute:{payload.agent}")
        if payload.topic:
            keys.append(f"policy:mute:{payload.topic}")
        if not keys:
            return False
        values = await self._redis.mget(keys)
        return any(v is not None for v in values)

    async def _rate_limit(self, payload: NotifyPayload) -> Decision:
        rules = self._policies.get("rate_limits", [])
        rl_fingerprint = f"{payload.agent or ''}|{payload.topic or ''}"
        for rule in rules:
            if not self._rule_matches(payload, rule.get("match", {})):
                continue
            rule_id = rule.get("id", "default")
            key = f"policy:rl:{rule_id}:{rl_fingerprint}"
            window_minutes = int(rule.get("window_minutes", 60))
            ttl = max(1, window_minutes * 60)
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, ttl)
            if count <= int(rule.get("max", 1)):
                return Decision(action="send", reason=f"rate_limit:{rule_id}")

            rollup_template = rule.get("rollup_message")
            if rollup_template:
                rollup_key = f"policy:rollup:{rule_id}:{rl_fingerprint}"
                suppressed_count = await self._redis.incr(rollup_key)
                if suppressed_count == 1:
                    await self._redis.expire(rollup_key, ttl)
                text = rollup_template.format(count=suppressed_count, window_minutes=window_minutes)
                return Decision(action="rollup", reason=f"rate_limit:{rule_id}", rollup_text=text)
            return Decision(action="suppress", reason=f"rate_limit:{rule_id}")
        return Decision(action="send", reason="rate_limit:none")

    async def _dedupe(self, payload: NotifyPayload, fingerprint: str) -> bool:
        window_minutes = int(self._policies.get("dedupe", {}).get("window_minutes", 30))
        ttl = max(1, window_minutes * 60)
        key = f"policy:dedupe:{fingerprint}"
        ok = await self._redis.set(key, "1", ex=ttl, nx=True)
        return bool(ok)

    def _fingerprint(self, payload: NotifyPayload) -> str:
        template = self._policies.get("dedupe", {}).get(
            "default_fingerprint", "{agent}|{topic}|{text|sha256:64}"
        )
        values = {
            "agent": payload.agent or "",
            "topic": payload.topic or "",
            "text": payload.text or "",
            "severity": payload.severity or "",
            "capability": payload.capability or "",
        }

        result = template
        for key, value in values.items():
            result = result.replace(f"{{{key}}}", str(value))

        while "{text|sha256:" in result:
            start = result.index("{text|sha256:")
            end = result.index("}", start)
            chunk = result[start + 13 : end]
            digest_len = int(chunk)
            digest = hashlib.sha256((payload.text or "").encode("utf-8")).hexdigest()[:digest_len]
            result = f"{result[:start]}{digest}{result[end + 1 :]}"
        return result

    def _rule_matches(self, payload: NotifyPayload, match: dict[str, Any]) -> bool:
        if "agent" in match and (payload.agent or "") != match["agent"]:
            return False
        if "topic" in match and (payload.topic or "") != match["topic"]:
            return False
        if "topic_pattern" in match and not fnmatch(payload.topic or "", match["topic_pattern"]):
            return False
        if "capability_pattern" in match and not fnmatch(
            payload.capability or "", match["capability_pattern"]
        ):
            return False
        return True

    async def quiet_hours_active(self) -> bool:
        override = await self._redis.get("policy:override:quiet")
        if override == "on":
            return True
        if override == "off":
            return False
        await self._ensure_sleep_windows_fresh()
        return self._quiet_hours_active(self._now_fn())

    async def set_quiet_override(self, value: str, ttl_seconds: int) -> None:
        await self._redis.set("policy:override:quiet", value, ex=ttl_seconds)

    async def clear_quiet_override(self) -> None:
        await self._redis.delete("policy:override:quiet")

    async def get_stats(self) -> dict[str, int]:
        raw = await self._redis.hgetall("policy:stats")
        return {k: int(v) for k, v in raw.items()}

    async def get_recent_decisions(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self._redis.lrange("policy:recent", 0, max(limit - 1, 0))
        return [json.loads(r) for r in rows]

    async def get_active_mutes(self) -> list[dict[str, Any]]:
        keys = await self._redis.keys("policy:mute:*")
        output: list[dict[str, Any]] = []
        for key in sorted(keys):
            ttl = await self._redis.ttl(key)
            output.append({"key": key.removeprefix("policy:mute:"), "ttl_seconds": max(ttl, 0)})
        return output


def _time_is_in_sleep_window(now_time: time, sleep_time: time, wake_time: time) -> bool:
    """True if ``now_time`` falls inside the [sleep_time, wake_time) window.

    Mirrors orchestrator.observers.tv_observer._time_is_in_sleep_window —
    handles midnight-crossing windows (23:00 -> 07:00) and same-clock-face
    windows (00:30 -> 09:00) correctly. Used here so the policy engine's
    quiet hours respect the user's actual bedtime instead of the static
    YAML default.
    """
    if sleep_time == wake_time:
        return False
    if sleep_time < wake_time:
        return sleep_time <= now_time < wake_time
    return now_time >= sleep_time or now_time < wake_time
