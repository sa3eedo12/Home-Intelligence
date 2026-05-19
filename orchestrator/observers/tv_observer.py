from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import Observer
from .utils import (
    device_key_for,
    domain_of,
    extract_state_change,
    normalized_state,
    parse_datetime,
    remember_bounded,
)

MAX_TRACKED_ENTITIES = 256
MAX_TRACKED_PRESENCE = 256
DEFAULT_MAX_ON_HOURS = 6.0
# Cooldown was 6h, which caused duplicate "TV left on" alerts on
# long-running cycles: TV on at 09:00 → fires at 15:00 (6h) → cooldown
# until 21:00 → fires AGAIN at 21:00 (12h). The user reported two
# alerts for the same Samsung TV on May 17. 24h gives us at most one
# alert per TV per rolling day, which matches the "left it on too
# long" intent. Tests can override per-case.
DEFAULT_COOLDOWN = timedelta(hours=24)
DEFAULT_WAKE_TIME = time(7, 0)
SLEEP_TIME_CACHE_TTL = timedelta(minutes=5)
MEDIA_ON_STATES = {"on", "playing", "paused", "idle", "buffering"}
DEVICE_ON_STATES = MEDIA_ON_STATES | {"active", "running"}
OFF_STATES = {"off", "standby", "unavailable", "unknown", "none"}


@dataclass
class _TvState:
    on_since: datetime | None = None
    friendly_name: str | None = None
    last_state: str | None = None


class TvObserver(Observer):
    name = "tv"
    subscribed_streams = ["events.home"]

    def __init__(
        self,
        *,
        max_on_hours: float | None = None,
        cooldown: timedelta | None = None,
        sleep_times: list[time | str | tuple[time | str | None, time | str | None]]
        | None = None,
    ) -> None:
        super().__init__()
        self.max_on_hours = (
            max_on_hours if max_on_hours is not None else _env_float("TV_MAX_ON_HOURS", None)
        )
        if self.max_on_hours is None:
            self.max_on_hours = _env_float("MAX_ON_HOURS", DEFAULT_MAX_ON_HOURS)
        self.cooldown = cooldown or DEFAULT_COOLDOWN
        self._states: OrderedDict[str, _TvState] = OrderedDict()
        self._presence_states: OrderedDict[str, str] = OrderedDict()
        self._recent_completions: OrderedDict[str, datetime] = OrderedDict()
        self._static_sleep_times = sleep_times is not None
        self._sleep_windows: list[tuple[time, time]] = _normalize_sleep_windows(sleep_times)
        self._sleep_times_loaded_at: datetime | None = None

    async def handle(self, payload: dict[str, Any]) -> None:
        change = extract_state_change(payload)
        if change is None:
            return

        self._update_presence(change)
        if _matches_tv(change):
            self._update_tv_state(change)

        now = parse_datetime(change.ts) or datetime.now(UTC)
        await self._check_active_tvs(now)

    def _update_presence(self, change: Any) -> None:
        if domain_of(change.entity_id) not in {"device_tracker", "person"}:
            return
        presence = _presence_state(change.new_state)
        if presence is None:
            return
        self._presence_states[change.entity_id] = presence
        self._presence_states.move_to_end(change.entity_id)
        while len(self._presence_states) > MAX_TRACKED_PRESENCE:
            self._presence_states.popitem(last=False)

    def _update_tv_state(self, change: Any) -> None:
        entry = remember_bounded(self._states, change.entity_id, _TvState, MAX_TRACKED_ENTITIES)
        entry.friendly_name = change.friendly_name
        entry.last_state = change.new_state
        if _is_on_state(change.entity_id, change.new_state):
            if entry.on_since is None:
                entry.on_since = parse_datetime(change.ts) or datetime.now(UTC)
            return
        entry.on_since = None

    async def _check_active_tvs(self, now: datetime) -> None:
        reason = await self._at_rest_reason(now)
        if reason is None:
            return
        for entity_id, entry in list(self._states.items()):
            if entry.on_since is None:
                continue
            on_hours = (now - entry.on_since).total_seconds() / 3600
            if on_hours < float(self.max_on_hours):
                continue
            if self._recently_completed(entity_id, now):
                continue
            friendly_name = entry.friendly_name or entity_id
            rounded_hours = round(on_hours, 2)
            await self.emit_event(
                "entertainment.left_on",
                f"{friendly_name} has been on for {rounded_hours:.1f}h ({reason})",
                {
                    "entity_id": entity_id,
                    "friendly_name": friendly_name,
                    "on_since": entry.on_since.isoformat(),
                    "on_hours": rounded_hours,
                    "reason": reason,
                },
            )
            self._mark_completed(entity_id, now)

    async def _at_rest_reason(self, now: datetime) -> str | None:
        if self._nobody_home():
            return "nobody_home"
        if await self._past_bedtime(now):
            return "past_bedtime"
        return None

    def _nobody_home(self) -> bool:
        return bool(self._presence_states) and all(
            state == "not_home" for state in self._presence_states.values()
        )

    async def _past_bedtime(self, now: datetime) -> bool:
        windows = await self._load_sleep_times(now)
        if not windows:
            return False
        local_now = now.astimezone(_local_tz()).time().replace(tzinfo=None)
        return any(_time_is_in_sleep_window(local_now, sleep, wake) for sleep, wake in windows)

    async def _load_sleep_times(self, now: datetime) -> list[tuple[time, time]]:
        if self._static_sleep_times:
            return list(self._sleep_windows)
        if (
            self._sleep_times_loaded_at is not None
            and now - self._sleep_times_loaded_at < SLEEP_TIME_CACHE_TTL
        ):
            return list(self._sleep_windows)
        pool = getattr(self.event_log_store, "pool", None)
        if pool is None:
            self._sleep_windows = []
            self._sleep_times_loaded_at = now
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT sleep_time, wake_time
                    FROM household_members
                    WHERE sleep_time IS NOT NULL
                      AND role <> 'pet'
                    """
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("tv_sleep_times_query_failed", error=str(exc))
            self._sleep_windows = []
            self._sleep_times_loaded_at = now
            return []
        windows: list[tuple[time, time]] = []
        for row in rows:
            sleep = _parse_time(row["sleep_time"])
            wake = _parse_time(row["wake_time"]) or DEFAULT_WAKE_TIME
            if sleep is not None:
                windows.append((sleep, wake))
        self._sleep_windows = windows
        self._sleep_times_loaded_at = now
        return list(self._sleep_windows)

    def _recently_completed(self, entity_id: str, now: datetime) -> bool:
        device = device_key_for(entity_id)
        last = self._recent_completions.get(device)
        if last is None:
            return False
        return (now - last) < self.cooldown

    def _mark_completed(self, entity_id: str, now: datetime) -> None:
        device = device_key_for(entity_id)
        self._recent_completions[device] = now
        self._recent_completions.move_to_end(device)
        while len(self._recent_completions) > 64:
            self._recent_completions.popitem(last=False)


def _matches_tv(change: Any) -> bool:
    entity_id = change.entity_id.casefold()
    entity_domain = domain_of(change.entity_id)
    if entity_domain == "media_player":
        return True
    if entity_id.startswith(("device.tv", "device.monitor")):
        return True

    type_hint = " ".join(
        str(change.attributes.get(key) or "")
        for key in ("type", "thing_type", "device_type", "device_class", "category")
    ).casefold()
    if "device.tv" in type_hint or "device.monitor" in type_hint:
        return True
    if type_hint.strip() in {"tv", "monitor"}:
        return True

    if entity_domain not in {"switch", "light"}:
        return False
    haystack = " ".join(
        [
            change.entity_id,
            change.friendly_name,
            str(change.attributes.get("friendly_name") or ""),
            str(change.attributes.get("device_class") or ""),
        ]
    ).casefold()
    # Blocklist: substrings that look TV-related but are NOT the TV itself.
    # Without this, switch.sound_sensor_tv_sound_detection matched on "tv"
    # and triggered "TV left on for 6h" notifications. Same defensive
    # filter the lights observer uses.
    if any(kw in haystack for kw in TV_FALSE_POSITIVE_KEYWORDS):
        return False
    return "tv" in haystack or "monitor" in haystack


# Substrings that indicate a switch/light entity is associated with a TV
# (so it matches the loose 'tv' substring) but is NOT the TV's power/state.
# Adding a new keyword here is the right move whenever a false positive
# surfaces in production.
TV_FALSE_POSITIVE_KEYWORDS: tuple[str, ...] = (
    "sound_sensor",
    "sound_detection",
    "remote_control",
    "child_lock",
    "indicator",
    "status_led",
    "backlight",
    "ambient",
    "bias_light",
    "screen_share",
    "energy",
    "power_consumption",
)


def _is_on_state(entity_id: str, state: str | None) -> bool:
    normalized = normalized_state(state)
    if not normalized or normalized in OFF_STATES:
        return False
    entity_domain = domain_of(entity_id)
    if entity_domain == "media_player":
        return normalized in MEDIA_ON_STATES
    if entity_id.casefold().startswith(("device.tv", "device.monitor")):
        return normalized in DEVICE_ON_STATES
    return normalized == "on"


def _presence_state(state: str | None) -> str | None:
    normalized = normalized_state(state)
    if normalized == "home":
        return "home"
    if normalized in {"not_home", "away", "offline"}:
        return "not_home"
    return None


def _time_is_in_sleep_window(now_time: time, sleep_time: time, wake_time: time) -> bool:
    """True if ``now_time`` falls inside the [sleep_time, wake_time) window.

    Handles midnight crossings: a window like (23:00, 07:00) means "asleep
    from 23:00 through 07:00 the next morning". A window like (00:30, 09:00)
    (sleep < wake on the same clock face) means "asleep from 00:30 to 09:00";
    the user is NOT considered "past bedtime" at, say, 18:42 of the previous
    evening — bedtime is still hours away. The old code mistakenly treated
    any sleep_time < 18:00 as a "morning bedtime" and triggered immediately
    past that wall-clock time.
    """
    if sleep_time == wake_time:
        return False
    if sleep_time < wake_time:
        return sleep_time <= now_time < wake_time
    # sleep_time > wake_time: window crosses midnight (e.g. 23:00 → 07:00)
    return now_time >= sleep_time or now_time < wake_time


def _normalize_sleep_windows(
    raw: list[time | str | tuple[time | str | None, time | str | None]] | None,
) -> list[tuple[time, time]]:
    """Accept the same shapes the constructor and DB row produce.

    Each entry may be either a bedtime alone (legacy ``time`` / ISO string),
    which is paired with the default wake time (``07:00``), or an explicit
    ``(sleep_time, wake_time)`` tuple. Missing wake times default to
    ``DEFAULT_WAKE_TIME``. Invalid entries are dropped.
    """
    windows: list[tuple[time, time]] = []
    for entry in raw or []:
        if isinstance(entry, tuple):
            sleep = _parse_time(entry[0])
            wake = _parse_time(entry[1]) or DEFAULT_WAKE_TIME
        else:
            sleep = _parse_time(entry)
            wake = DEFAULT_WAKE_TIME
        if sleep is not None:
            windows.append((sleep, wake))
    return windows


def _parse_time(raw: time | str | None) -> time | None:
    if raw is None:
        return None
    if isinstance(raw, time):
        return raw.replace(tzinfo=None)
    text = str(raw).strip()
    if not text:
        return None
    try:
        return time.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def _local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("TZ", "UTC"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _env_float(name: str, default: float | None) -> float | None:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def build() -> TvObserver:
    return TvObserver()
