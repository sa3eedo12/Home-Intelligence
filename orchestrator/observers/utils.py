from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

try:  # Keep this map aligned with the HA agent without making tests depend on its install path.
    from agents.home_automation.tools.appliance import APPLIANCE_SYNONYMS
except Exception:  # pragma: no cover - exercised only when the agent package is not importable.
    APPLIANCE_SYNONYMS: dict[str, list[str]] = {
        "washer": ["washing_machine", "washer", "laundry"],
        "dryer": ["tumble_dryer", "dryer"],
        "dishwasher": ["dishwasher"],
        "oven": ["oven"],
        "fridge": ["fridge", "refrigerator"],
        "freezer": ["freezer"],
        "coffee": ["coffee_machine", "coffee_maker", "espresso"],
        "vacuum": ["vacuum", "robot_cleaner"],
        "ac": ["climate.", "air_conditioner", "ac_"],
        "tv": ["media_player.", "tv"],
    }

BRAND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Bosch Home Connect": ("bosch", "home_connect", "home connect"),
    "Miele": ("miele",),
    "Samsung SmartThings": ("samsung", "smartthings", "smart things"),
    "LG ThinQ": ("lg", "thinq", "thin q"),
}
RUNNING_STATES = {"on", "run", "running", "active", "in_progress", "washing", "cleaning", "brewing"}
FINISHING_STATES = {"done", "complete", "completed", "finish", "finished", "finishing"}
IDLE_STATES = {"off", "idle", "standby", "docked", "paused"}
PROGRAM_KEYS = (
    "active_program",
    "selected_program",
    "program",
    "course",
    "cycle",
    "mode",
    "operation_state",
)


@dataclass(frozen=True)
class StateChange:
    entity_id: str
    old_state: str | None
    new_state: str | None
    attributes: dict[str, Any]
    old_attributes: dict[str, Any]
    friendly_name: str
    area: str | None
    ts: str


def extract_state_change(payload: dict[str, Any]) -> StateChange | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    old_obj = _state_object(data.get("old_state", payload.get("old_state")))
    new_obj = _state_object(data.get("new_state", payload.get("new_state")))
    entity_id = str(
        data.get("entity_id")
        or payload.get("entity_id")
        or new_obj.get("entity_id")
        or old_obj.get("entity_id")
        or ""
    ).strip()
    if not entity_id:
        return None

    attrs = _dict_value(new_obj.get("attributes") or payload.get("attributes"))
    old_attrs = _dict_value(old_obj.get("attributes") or payload.get("old_attributes"))
    new_state = _state_value(new_obj.get("state", payload.get("state")))
    old_state = _state_value(old_obj.get("state", payload.get("old_state")))
    friendly_name = str(
        attrs.get("friendly_name")
        or payload.get("friendly_name")
        or payload.get("name")
        or entity_id
    )
    area_raw = attrs.get("area") or attrs.get("area_name") or payload.get("area")
    ts = str(
        payload.get("time_fired")
        or payload.get("ts")
        or new_obj.get("last_changed")
        or new_obj.get("last_updated")
        or datetime.now(UTC).isoformat()
    )
    return StateChange(
        entity_id=entity_id,
        old_state=old_state,
        new_state=new_state,
        attributes=attrs,
        old_attributes=old_attrs,
        friendly_name=friendly_name,
        area=str(area_raw) if area_raw not in (None, "") else None,
        ts=ts,
    )


def matches_appliance(change: StateChange, appliance: str) -> bool:
    needles = APPLIANCE_SYNONYMS.get(appliance, [appliance])
    if appliance == "vacuum" and domain_of(change.entity_id) == "vacuum":
        return True
    haystack = " ".join(
        [
            change.entity_id,
            change.friendly_name,
            str(change.attributes.get("brand") or ""),
            str(change.attributes.get("manufacturer") or ""),
            str(change.attributes.get("model") or ""),
        ]
    ).casefold()
    return any(needle.casefold() in haystack for needle in needles)


def domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def normalized_state(state: str | None) -> str:
    return str(state or "").strip().casefold().replace(" ", "_").replace("-", "_")


def is_running_state(state: str | None) -> bool:
    return normalized_state(state) in RUNNING_STATES


def is_finishing_state(state: str | None) -> bool:
    return normalized_state(state) in FINISHING_STATES


def is_idle_state(state: str | None) -> bool:
    return normalized_state(state) in IDLE_STATES


def first_attr(attrs: dict[str, Any], keys: tuple[str, ...] = PROGRAM_KEYS) -> Any | None:
    for key in keys:
        value = attrs.get(key)
        if value not in (None, ""):
            return value
    return None


def detect_brand(entity_id: str, attrs: dict[str, Any], friendly_name: str = "") -> str:
    haystack = " ".join(
        [
            entity_id,
            friendly_name,
            str(attrs.get("brand") or ""),
            str(attrs.get("manufacturer") or ""),
            str(attrs.get("integration") or ""),
            str(attrs.get("attribution") or ""),
            str(attrs.get("model") or ""),
        ]
    ).casefold()
    for label, needles in BRAND_KEYWORDS.items():
        if any(needle in haystack for needle in needles):
            return label
    return "generic"


def seconds_between(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    start_dt = parse_datetime(start)
    end_dt = parse_datetime(end)
    if start_dt is None or end_dt is None:
        return None
    return max(0, round((end_dt - start_dt).total_seconds()))


def parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def remember_bounded(
    states: OrderedDict[str, Any],
    entity_id: str,
    factory: type[Any],
    max_entities: int,
) -> Any:
    if entity_id in states:
        states.move_to_end(entity_id)
        return states[entity_id]
    if len(states) >= max_entities:
        states.popitem(last=False)
    value = factory()
    states[entity_id] = value
    return value


def _state_object(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _state_value(raw: Any) -> str | None:
    if raw is None or isinstance(raw, dict):
        return None
    text = str(raw).strip()
    return text or None


def _dict_value(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}
