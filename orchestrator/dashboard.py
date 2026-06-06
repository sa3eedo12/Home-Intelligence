from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from home_agents_sdk.health_store import HealthStore
from home_agents_sdk.health_goals_store import HealthGoalsStore
from home_agents_sdk.chore_store import ChoreStore
from home_agents_sdk.member_nag_windows_store import MemberNagWindowsStore
from home_agents_sdk.reflection_store import ReflectionStore
from home_agents_sdk.routine_lifecycle_store import RoutineLifecycleStore
from home_agents_sdk.telemetry import get_logger

from .admin import build_onboarding_state
from .data_science.common import current_embedding_model, decode_json
from .observers.utils import APPLIANCE_SYNONYMS

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
logger = get_logger("orchestrator.dashboard")

DISCOVERY_TYPES = [
    "appliance.washer",
    "appliance.dryer",
    "appliance.vacuum",
    "appliance.dishwasher",
    "appliance.oven",
    "appliance.coffee_maker",
    "appliance.fridge",
    "appliance.microwave",
    "appliance.air_purifier",
    "appliance.water_heater",
    "device.tv",
    "device.monitor",
    "device.speaker",
    "device.computer",
    "device.printer",
    "device.router",
    "device.phone",
    "device.tablet",
    "device.game_console",
    "vehicle.car",
    "vehicle.bike",
    "person.member",
    "room",
    "pet.dog",
    "pet.cat",
    "light",
    "sensor",
    "media_player",
    "other",
]


async def _status(request: Request) -> dict[str, Any]:
    provider = getattr(request.app.state, "status_provider", None)
    if provider is None:
        return {"reflection": {"last_run_at": None, "age_hours": None, "healthy": False}}
    status = await provider()
    status.setdefault("reflection", {"last_run_at": None, "age_hours": None, "healthy": False})
    return status


def _reflection_store(request: Request) -> Any:
    store = getattr(request.app.state, "reflection_store", None)
    if store is not None:
        return store
    return ReflectionStore(getattr(request.app.state, "pool", None))


def _health_store(request: Request) -> Any | None:
    store = getattr(request.app.state, "health_store", None)
    if store is not None:
        return store
    pool = getattr(request.app.state, "pool", None)
    return HealthStore(pool) if pool is not None else None


def _parse_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _relative_age(raw: Any) -> str:
    ts = _parse_dt(raw)
    if ts is None:
        return "never"
    hours = max(0.0, (datetime.now(UTC) - ts.astimezone(UTC)).total_seconds() / 3600)
    if hours < 1:
        return f"{max(1, round(hours * 60))}m ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


def _last_aggregate_value(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    value = rows[-1].get("value")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum_recent(rows: list[dict[str, Any]], metric: str) -> float:
    """Sum non-overlapping metric values across recent rows.

    Routes sleep_* metrics through :func:`_union_recent_sleep_minutes`
    because Health Auto Export re-sends the same sleep session with
    updated end-times on every sync, producing 2-3 rows for one night
    that a naive ``sum(value)`` triple-counts (Saeed observed 34h 35m
    of "asleep" for a 7h night because of this).
    """
    if metric.startswith("sleep_"):
        return _union_recent_sleep_minutes(rows, metric)
    total = 0.0
    for row in rows:
        if row.get("metric") != metric:
            continue
        try:
            total += float(row.get("value") or 0.0)
        except (TypeError, ValueError):
            continue
    return round(total, 1)


# HealthKit / HAE outer-envelope and re-sync dedup helpers, mirroring
# the logic in agents/personal_assistant/tools/sleep_inference.py.
# Duplicated here because the dashboard renders synchronously from
# raw HAE rows and shouldn't have to round-trip through the personal
# assistant agent just to compute a tile value.
_DASHBOARD_MAX_PLAUSIBLE_SLEEP_HOURS = 14


def _row_started_ended(row: dict[str, Any]) -> tuple[datetime, datetime] | None:
    started = _coerce_dt(row.get("started_at"))
    if started is None:
        return None
    ended = _coerce_dt(row.get("ended_at"))
    if ended is None:
        try:
            value_minutes = float(row.get("value") or 0)
        except (TypeError, ValueError):
            return None
        if value_minutes <= 0:
            return None
        ended = started + timedelta(minutes=value_minutes)
    if ended <= started:
        return None
    return started, ended


def _coerce_dt(value: Any) -> datetime | None:
    """Accept either a datetime object (asyncpg fetch) or an ISO string
    (after the HealthStore's JSON-friendly row formatter)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _union_recent_sleep_minutes(
    rows: list[dict[str, Any]], metric: str
) -> float:
    """Most-recent-night-only minute total for a sleep metric.

    Two HAE quirks to defend against:
      1. Re-sync produces 2-3 rows for ONE sleep session with the same
         started_at and progressively-larger ended_at as the user wakes
         up gradually. Naive sum triple-counts the same minutes.
      2. HAE occasionally emits an outer-envelope row spanning >14h
         (two unrelated days' sleep bundled into one row). These must
         be excluded — see the long comment in sleep_inference.py.

    One product quirk on top of the data quirks: the dashboard tile is
    a single "Sleep" number that users read as "last night." The query
    window pulls 36h of metrics, so on a normal morning we'd see TWO
    sleep sessions (yesterday's and today's) — summing them gave the
    "14h 15min sleep" production bug. Fix: cluster intervals into
    nights (a >2h wake gap breaks the night), return ONLY the most
    recent night's union.

    Strategy: per (started_at) bucket, keep the row with the LARGEST
    end_time (latest re-sync wins). Drop intervals longer than 14h.
    Sort + merge with a 2h wake-tolerance, then return the LAST
    merged interval's duration.
    """
    by_start: dict[datetime, tuple[datetime, datetime]] = {}
    for row in rows:
        if row.get("metric") != metric:
            continue
        interval = _row_started_ended(row)
        if interval is None:
            continue
        start, end = interval
        hours = (end - start).total_seconds() / 3600
        if hours > _DASHBOARD_MAX_PLAUSIBLE_SLEEP_HOURS:
            continue
        # Multiple snapshots of the same session: keep the one with the
        # latest ended_at (the most-recent re-sync).
        existing = by_start.get(start)
        if existing is None or end > existing[1]:
            by_start[start] = (start, end)

    intervals = sorted(by_start.values(), key=lambda p: p[0])
    if not intervals:
        return 0.0

    # 2h tolerance lets brief mid-night wakeups stay in the same "night";
    # a >2h gap forces a new night and discards the older one.
    wake_tolerance = timedelta(hours=2)
    merged: list[list[datetime]] = []
    for start, end in intervals:
        if not merged or start - merged[-1][1] > wake_tolerance:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end

    # The dashboard tile is "last night," not "the sum of every night
    # in the query window." Return the most recent merged session.
    last_start, last_end = merged[-1]
    seconds = (last_end - last_start).total_seconds()
    return round(seconds / 60, 1)


async def _health_snapshot(request: Request) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "configured": False,
        "summary": {"total_metrics": 0, "last_received_at": None, "last_sync_label": "never"},
        "tiles": {},
        "sleep_breakdown": {},
        "charts": {"steps": [], "sleep_asleep": [], "weight": [], "resting_heart_rate": []},
    }
    store = _health_store(request)
    if store is None:
        return empty
    try:
        summary_method = getattr(store, "summary", None)
        summary = await summary_method() if callable(summary_method) else {}
        (
            recent_sleep,
            steps_30,
            sleep_30,
            weight_30,
            resting_30,
            active_energy_1,
            latest_weight,
            latest_workout,
            latest_resting,
        ) = await asyncio.gather(
            store.list_recent(hours=36),
            store.aggregate_daily("steps", days=30),
            store.aggregate_daily("sleep_asleep", days=30),
            store.aggregate_daily("weight", days=30),
            store.aggregate_daily("resting_heart_rate", days=30),
            store.aggregate_daily("active_energy", days=1),
            store.latest("weight"),
            store.latest("workout"),
            store.latest("resting_heart_rate"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("health_dashboard_snapshot_failed", error=str(exc))
        return empty

    sleep_breakdown = {
        "asleep": _sum_recent(recent_sleep, "sleep_asleep"),
        "deep": _sum_recent(recent_sleep, "sleep_deep"),
        "rem": _sum_recent(recent_sleep, "sleep_rem"),
        "core": _sum_recent(recent_sleep, "sleep_core"),
        "awake": _sum_recent(recent_sleep, "sleep_awake"),
        "inBed": _sum_recent(recent_sleep, "sleep_inBed"),
    }
    sleep_total = sleep_breakdown["asleep"] or sum(
        sleep_breakdown[key] for key in ("deep", "rem", "core")
    )
    total_metrics = int(summary.get("total_metrics") or summary.get("count") or 0)
    last_received = summary.get("last_received_at")
    return {
        "configured": True,
        "summary": {
            "total_metrics": total_metrics,
            "last_received_at": last_received,
            "last_sync_label": _relative_age(last_received),
        },
        "tiles": {
            "sleep_minutes": round(sleep_total, 1),
            "steps_today": _last_aggregate_value(steps_30),
            "active_energy_today": _last_aggregate_value(active_energy_1),
            "latest_weight": latest_weight,
            "latest_workout": latest_workout,
            "resting_heart_rate": latest_resting,
        },
        "sleep_breakdown": sleep_breakdown,
        "charts": {
            "steps": steps_30,
            "sleep_asleep": sleep_30,
            "weight": weight_30,
            "resting_heart_rate": resting_30,
        },
    }


async def _data_science_snapshot(request: Request) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "reports": [],
        "maintenance_runs": [],
        "last_lora_run": None,
        "embedding": {"current_model": _current_embedding_model(request), "stale_event_count": 0},
    }
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return snapshot
    try:
        async with pool.acquire() as conn:
            report_rows = await conn.fetch(
                """
                SELECT kind, period_label, file_path, summary, generated_at
                FROM reports
                ORDER BY generated_at DESC
                LIMIT 10
                """
            )
            maintenance_rows = await conn.fetch(
                """
                SELECT ts, summary, payload
                FROM event_log
                WHERE agent = 'data_science'
                  AND capability = 'maintenance'
                ORDER BY ts DESC
                LIMIT 7
                """
            )
            lora_row = await conn.fetchrow(
                """
                SELECT id, started_at, finished_at, status, model_base,
                       training_file, quality_score, error
                FROM lora_training_runs
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            stale_count = await conn.fetchval(
                """
                SELECT count(*)
                FROM event_log
                WHERE embedding_model IS DISTINCT FROM $1
                """,
                snapshot["embedding"]["current_model"],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("data_science_dashboard_snapshot_failed", error=str(exc))
        return snapshot

    snapshot["reports"] = [_dashboard_row(row) for row in report_rows]
    snapshot["maintenance_runs"] = [_maintenance_row(row) for row in maintenance_rows]
    snapshot["last_lora_run"] = _dashboard_row(lora_row) if lora_row else None
    snapshot["embedding"]["stale_event_count"] = int(stale_count or 0)
    return snapshot


def _current_embedding_model(request: Request) -> str:
    reembed = getattr(request.app.state, "reembed", None)
    value = getattr(reembed, "current_model", None)
    if value:
        return str(value)
    return current_embedding_model(getattr(request.app.state, "embedder", None))


def _dashboard_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key, value in list(data.items()):
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    return data


def _maintenance_row(row: Any) -> dict[str, Any]:
    data = _dashboard_row(row)
    payload = decode_json(data.get("payload"), {})
    if not isinstance(payload, dict):
        payload = {}
    return {
        "ts": data.get("ts"),
        "summary": data.get("summary"),
        "status": payload.get("status") or "ok",
        "archived_rows": payload.get("archived_rows", 0),
        "errors": payload.get("errors") or [],
    }


async def _about_you_snapshot(
    request: Request, *, member_id: int | None = None
) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "members": [],
        "current_member": None,
        "personal_devices": [],
        "habits": [],
        "preferences": [],
        "routines": [],
    }
    knowledge_graph = getattr(request.app.state, "knowledge_graph", None)
    if knowledge_graph is None:
        return empty
    try:
        things, habits, preferences, routines, members = await asyncio.gather(
            knowledge_graph.list_things(),
            knowledge_graph.list_habits(),
            knowledge_graph.list_preferences(),
            knowledge_graph.list_routines(),
            knowledge_graph.list_members(include_pets=False),
        )
    except Exception:
        return empty

    members = members or []
    # Choose the current member: explicit query param wins, else first member.
    current = None
    if member_id is not None:
        current = next((m for m in members if int(m.get("id") or 0) == member_id), None)
    if current is None and members:
        current = members[0]
    current_id = int(current.get("id")) if current else None

    # Profile rows (the answers the user gave during onboarding +
    # follow-up questions). Loaded from the reflection_store/user_profile
    # table — currently the ONLY page that surfaces these. Without this,
    # answered profile questions silently sit in the DB unused — the
    # exact "are my answers being used?" complaint.
    profile_entries: list[dict[str, Any]] = []
    reflection_store = getattr(request.app.state, "reflection_store", None)
    if reflection_store is not None and hasattr(reflection_store, "list_profile"):
        try:
            raw_profile = await reflection_store.list_profile()
        except Exception as exc:
            logger.warning("about_you_profile_load_failed", error=str(exc))
            raw_profile = []
        for entry in raw_profile or []:
            key = str(entry.get("key") or "").strip()
            if not key:
                continue
            source = str(entry.get("source") or "")
            confidence = entry.get("confidence")
            try:
                confidence_value = float(confidence) if confidence is not None else None
            except (TypeError, ValueError):
                confidence_value = None
            # Skip explicitly-skipped entries from the UI by default.
            if source == "user_skipped":
                continue
            profile_entries.append({
                "key": key,
                "label": _humanize_profile_key(key),
                "value": _humanize_profile_value(entry.get("value")),
                "raw_value": entry.get("value"),
                "source": source,
                "confidence": confidence_value,
                "updated_at": str(entry.get("updated_at") or ""),
            })

    # Filter things to those owned by current member. Owner is stored in
    # attributes.owner_member_id (set by auto_setup's heuristic or via
    # POST /admin/devices/{id}/owner). Falls back to the column.
    personal_devices: list[dict[str, Any]] = []
    if current_id is not None:
        for t in things or []:
            attrs = t.get("attributes") or {}
            owner = attrs.get("owner_member_id") or t.get("owner_member_id")
            try:
                if owner is not None and int(owner) == current_id:
                    personal_devices.append(t)
            except (TypeError, ValueError):
                continue

    return {
        "members": [
            {"id": m.get("id"), "name": m.get("name"), "role": m.get("role")}
            for m in members
        ],
        "current_member": current,
        "personal_devices": personal_devices,
        "habits": habits or [],
        "preferences": preferences or [],
        "routines": routines or [],
        "profile": profile_entries,
    }


def _humanize_profile_key(key: str) -> str:
    """Turn 'sleep_time' -> 'Sleep time', 'dietary_restrictions' -> 'Dietary restrictions'."""
    cleaned = key.replace("_", " ").strip()
    return cleaned[:1].upper() + cleaned[1:] if cleaned else key


def _humanize_profile_value(value: Any) -> str:
    """Render a profile value (string, dict, list, jsonb) as a single readable line."""
    import json as _json

    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith('"') and text.endswith('"'):
            try:
                text = _json.loads(text)
            except Exception:
                text = text[1:-1]
        return str(text)
    if isinstance(value, dict):
        if not value:
            return ""
        if "value" in value and len(value) <= 2:
            return _humanize_profile_value(value.get("value"))
        return ", ".join(
            f"{k}: {v}" for k, v in value.items() if k not in {"confidence", "source"}
        )
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v not in (None, ""))
    return str(value)


async def _discovery_snapshot(request: Request) -> dict[str, Any]:
    entities, things = await asyncio.gather(
        _list_ha_entities(request),
        _list_discovery_things(request),
    )
    known_entity_ids = {
        str(entity_id)
        for thing in things
        for entity_id in (thing.get("ha_entity_ids") or [])
        if entity_id
    }
    unidentified = [
        {**entity, "suggested_type": _suggest_entity_type(entity)}
        for entity in entities
        if entity.get("entity_id") not in known_entity_ids
    ]
    return {
        "entities": unidentified,
        "types": DISCOVERY_TYPES,
        "identified_count": len(known_entity_ids),
        "total_count": len(entities),
    }


async def _list_ha_entities(request: Request) -> list[dict[str, Any]]:
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return []
    try:
        result = await registry.dispatch(
            "home_automation",
            "list_entities",
            {"include_unavailable": True},
        )
    except Exception as exc:
        logger.warning("discovery_list_entities_failed", error=str(exc))
        return []
    if isinstance(result, dict) and result.get("ok") is False:
        logger.warning("discovery_list_entities_failed", error=result.get("error"))
        return []
    payload = result.get("result") if isinstance(result, dict) and "result" in result else result
    return _flatten_entities(payload)


async def _list_discovery_things(request: Request) -> list[dict[str, Any]]:
    knowledge_graph = getattr(request.app.state, "knowledge_graph", None)
    if knowledge_graph is None:
        return []
    try:
        return await knowledge_graph.list_things()
    except Exception as exc:
        logger.warning("discovery_list_things_failed", error=str(exc))
        return []


def _flatten_entities(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [entity for item in payload if (entity := _normalize_entity(item, None))]
    if not isinstance(payload, dict):
        return []
    by_area = payload.get("by_area")
    if isinstance(by_area, dict):
        entities: list[dict[str, Any]] = []
        for area, items in by_area.items():
            if not isinstance(items, list):
                continue
            entities.extend(
                entity for item in items if (entity := _normalize_entity(item, str(area)))
            )
        return entities
    items = payload.get("items") or payload.get("entities") or []
    return [entity for item in items if (entity := _normalize_entity(item, None))]


def _normalize_entity(item: Any, area: str | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    entity_id = str(item.get("entity_id") or "").strip()
    if not entity_id:
        return None
    friendly_name = str(
        item.get("friendly_name") or item.get("name") or attrs.get("friendly_name") or entity_id
    )
    entity_area = item.get("area") or attrs.get("area") or attrs.get("area_name") or area
    return {
        "entity_id": entity_id,
        "friendly_name": friendly_name,
        "area": entity_area or "Unassigned",
        "state": item.get("state", "unknown"),
        "attributes": attrs,
    }


def _suggest_entity_type(entity: dict[str, Any]) -> str:
    entity_id = str(entity.get("entity_id") or "")
    friendly_name = str(entity.get("friendly_name") or "")
    haystack = f"{entity_id} {friendly_name}".casefold()
    appliance_types = {
        "washer": "appliance.washer",
        "dryer": "appliance.dryer",
        "vacuum": "appliance.vacuum",
        "dishwasher": "appliance.dishwasher",
        "oven": "appliance.oven",
        "coffee": "appliance.coffee_maker",
    }
    for appliance, thing_type in appliance_types.items():
        if any(needle.casefold() in haystack for needle in APPLIANCE_SYNONYMS.get(appliance, [])):
            return thing_type
    # Display devices: heuristics on entity_id + friendly_name keywords.
    display_keywords = {
        "device.tv": ("tv", "television", "lg_tv", "samsung_tv", "android_tv", "appletv", "roku"),
        "device.monitor": ("monitor", "display", "screen"),
        "device.speaker": ("speaker", "sonos", "homepod", "echo", "soundbar"),
        "device.computer": ("desktop", "laptop", "macbook", "imac", "pc", "workstation"),
        "device.game_console": ("playstation", "ps4", "ps5", "xbox", "nintendo", "switch_console"),
        "device.router": ("router", "gateway", "access_point"),
        "device.printer": ("printer",),
        "device.phone": ("iphone", "phone", "pixel", "galaxy"),
        "device.tablet": ("ipad", "tablet"),
        "appliance.fridge": ("fridge", "refrigerator", "freezer"),
        "appliance.microwave": ("microwave",),
        "appliance.air_purifier": ("air_purifier", "purifier"),
        "appliance.water_heater": ("water_heater", "boiler"),
    }
    for thing_type, keywords in display_keywords.items():
        if any(keyword in haystack for keyword in keywords):
            return thing_type
    domain = entity_id.split(".", 1)[0]
    if domain == "light":
        return "light"
    if domain in {"sensor", "binary_sensor"}:
        return "sensor"
    if domain == "media_player":
        # Distinguish TV-shaped media_players from speakers based on the name.
        if any(kw in haystack for kw in ("tv", "television", "appletv", "roku", "android_tv")):
            return "device.tv"
        return "media_player"
    if domain == "person":
        return "person.member"
    if domain == "device_tracker":
        if any(token in haystack for token in ("car", "vehicle", "tesla", "bmw", "audi")):
            return "vehicle.car"
        return "person.member"
    return "other"


@router.get("/dashboard/about-you", response_class=HTMLResponse)
async def about_you(
    request: Request, member: int | None = None
) -> HTMLResponse:
    knowledge = await _about_you_snapshot(request, member_id=member)
    return templates.TemplateResponse(
        request=request,
        name="about_you.html.j2",
        context={"knowledge": knowledge},
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    store = _reflection_store(request)
    pending_proposals = 0
    if store is not None and hasattr(store, "count_proposals"):
        try:
            pending_proposals = await store.count_proposals(status="pending")
        except Exception:
            pending_proposals = 0
    suggested_routines = 0
    try:
        stats = await _routine_lifecycle_store(request).stats()
        suggested_routines = int(stats.get("suggested") or 0)
    except Exception:
        suggested_routines = 0
    overdue_chores = 0
    try:
        chore_rows = await _chore_store(request).list_status(include_recent=False)
        overdue_chores = sum(1 for c in chore_rows if c.status == "overdue")
    except Exception:
        overdue_chores = 0
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html.j2",
        context={
            "status": await _status(request),
            "pending_proposals": pending_proposals,
            "suggested_routines": suggested_routines,
            "overdue_chores": overdue_chores,
        },
    )


@router.get("/dashboard/health", response_class=HTMLResponse)
async def health_dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="health.html.j2",
        context={"health": await _health_snapshot(request)},
    )


@router.get("/dashboard/what-i-know", response_class=HTMLResponse)
async def what_i_know(request: Request) -> HTMLResponse:
    """The 'feels intelligent' page — what the system has learned about you."""
    from .intelligence import gather_intelligence_summary

    summary = await gather_intelligence_summary(request)
    return templates.TemplateResponse(
        request=request,
        name="what_i_know.html.j2",
        context={"summary": summary},
    )


@router.get("/dashboard/data-science", response_class=HTMLResponse)
async def data_science(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="data_science.html.j2",
        context={"data_science": await _data_science_snapshot(request)},
    )


@router.get("/dashboard/discovery", response_class=HTMLResponse)
async def discovery(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="discovery.html.j2",
        context={"discovery": await _discovery_snapshot(request)},
    )


@router.get("/dashboard/devices", response_class=HTMLResponse)
async def devices_page(request: Request) -> HTMLResponse:
    """Personal vs Home devices, grouped by area, with drill-down."""
    return templates.TemplateResponse(
        request=request,
        name="devices.html.j2",
        context={},
    )


@router.get("/dashboard/onboarding", response_class=HTMLResponse)
async def onboarding(request: Request, stage: int | None = None) -> HTMLResponse:
    state = await build_onboarding_state(request, override_stage=stage)
    return templates.TemplateResponse(
        request=request,
        name="onboarding.html.j2",
        context={"onboarding": state},
    )


@router.get("/dashboard/morning-brief", response_class=HTMLResponse)
async def morning_brief(request: Request) -> HTMLResponse:
    store = _reflection_store(request)
    briefs = await store.list_briefs(limit=1)
    brief = briefs[0] if briefs else None
    body = (brief or {}).get("body_json") or {}
    # Only show proposals that are still actionable on this page. Resolved
    # ones (accepted / dismissed / auto_confirmed) belong on /dashboard/proposals
    # where they can be filtered. Without this gate the brief showed all 25
    # historical proposals as if each were waiting for input.
    proposals = await store.list_proposals(status="pending", limit=50)
    if not proposals:
        # Fall back to the snapshot embedded in the brief body for older briefs.
        proposals = [
            p
            for p in (body.get("proposals") or [])
            if str(p.get("status") or "pending") == "pending"
        ]
    reflector = getattr(request.app.state, "reflector", None)
    reflection_state = reflector.status if reflector is not None else {"running": False}
    return templates.TemplateResponse(
        request=request,
        name="morning_brief.html.j2",
        context={
            "status": await _status(request),
            "brief": brief,
            "proposals": proposals,
            "reflection_state": reflection_state,
        },
    )


@router.get("/dashboard/proposals", response_class=HTMLResponse)
async def proposals_page(
    request: Request, status: str | None = None
) -> HTMLResponse:
    """Focused 'needs your decision' view of all proposals. Shows pending
    cards by default with checkboxes + bulk Accept / Dismiss controls so
    the user can clear the backlog in one round-trip."""
    store = _reflection_store(request)
    # Pull a wide window so the JS can client-side filter without a refetch.
    proposals = await store.list_proposals(limit=500)
    counts = {
        "pending": 0,
        "accepted": 0,
        "dismissed": 0,
        "auto_confirmed": 0,
        "expired": 0,
    }
    for p in proposals:
        s = str(p.get("status") or "")
        if s in counts:
            counts[s] += 1
    return templates.TemplateResponse(
        request=request,
        name="proposals.html.j2",
        context={
            "proposals": proposals,
            "counts": counts,
            "initial_status_filter": status or "pending",
        },
    )


def _routine_lifecycle_store(request: Request) -> RoutineLifecycleStore:
    store = getattr(request.app.state, "routine_lifecycle_store", None)
    if store is not None:
        return store
    return RoutineLifecycleStore(getattr(request.app.state, "pool", None))


def _chore_store(request: Request) -> ChoreStore:
    store = getattr(request.app.state, "chore_store", None)
    if store is not None:
        return store
    return ChoreStore(getattr(request.app.state, "pool", None))


def _prep_chore(status: Any) -> dict[str, Any]:
    """Render a ChoreStatus row for the template. Picks a human label
    for the next-due copy and a deterministic CSS class for color."""
    last_done = _format_dt(getattr(status, "last_done_at", None))
    days_overdue = int(getattr(status, "days_overdue", 0))
    if status.status == "overdue":
        days_late = days_overdue
        when_label = (
            f"{days_late} day overdue" if days_late == 1
            else f"{days_late} days overdue"
        )
    elif status.status == "due_today":
        when_label = "due today"
    elif status.status == "soon":
        days_until = -days_overdue
        when_label = "due tomorrow" if days_until == 1 else f"due in {days_until} days"
    else:
        days_until = -days_overdue
        when_label = (
            "done — next in a week"
            if days_until >= 7
            else f"done — next in {days_until} day{'s' if days_until != 1 else ''}"
        )
    return {
        "template_id": status.template_id,
        "name": status.name,
        "category": status.category,
        "description": status.description or "",
        "cadence_days": status.cadence_days,
        "auto_detect_kind": status.auto_detect_kind,
        "last_done_at": last_done,
        "next_due_on": status.next_due_on.isoformat() if status.next_due_on else None,
        "when_label": when_label,
        "status": status.status,
    }


def _format_routine_ts(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def _prep_routine(row: dict[str, Any]) -> dict[str, Any]:
    """Tidy a routines row for the Jinja template — decode steps jsonb
    and stringify the timestamps."""
    out = dict(row)
    steps = out.get("steps")
    if isinstance(steps, str):
        out["steps"] = decode_json(steps, {})
    for ts_key in ("created_at", "updated_at", "promoted_at", "dismissed_at"):
        if ts_key in out:
            out[ts_key] = _format_routine_ts(out.get(ts_key))
    return out


@router.get("/dashboard/routines", response_class=HTMLResponse)
async def routines_page(request: Request) -> HTMLResponse:
    """Suggested / Active / Dismissed routines with confirm + dismiss
    buttons. Phase 6 of the routine-inference roadmap."""
    store = _routine_lifecycle_store(request)
    suggested, active, dismissed, stats = (
        await store.list_suggested(),
        await store.list_active(),
        await store.list_dismissed(),
        await store.stats(),
    )
    return templates.TemplateResponse(
        request=request,
        name="routines.html.j2",
        context={
            "suggested": [_prep_routine(r) for r in suggested],
            "active": [_prep_routine(r) for r in active],
            "dismissed": [_prep_routine(r) for r in dismissed],
            "stats": stats,
        },
    )


@router.get("/dashboard/chores", response_class=HTMLResponse)
async def chores_page(request: Request) -> HTMLResponse:
    """Recurring household chores: overdue / due today / soon / recent
    with a manual mark-done button on each. Status is computed from
    the chore_log; the template just declares cadence."""
    store = _chore_store(request)
    all_status = await store.list_status(include_recent=True)
    buckets: dict[str, list[Any]] = {
        "overdue": [], "due_today": [], "soon": [], "recent": [],
    }
    for s in all_status:
        buckets.setdefault(s.status, []).append(_prep_chore(s))
    return templates.TemplateResponse(
        request=request,
        name="chores.html.j2",
        context={
            "overdue": buckets["overdue"],
            "due_today": buckets["due_today"],
            "soon": buckets["soon"],
            "recent": buckets["recent"],
            "total": len(all_status),
        },
    )


@router.get("/dashboard/goals", response_class=HTMLResponse)
async def goals_page(request: Request) -> HTMLResponse:
    """Health goals for the household. Shows each active + paused goal
    as a prose card with its plan, latest progress, and a button to
    refresh the plan. Creating a new goal happens in plain English
    via Telegram; this page is the canonical viewer."""
    store = _health_goals_store(request)
    pool = getattr(request.app.state, "pool", None)
    members: list[dict[str, Any]] = []
    if pool is not None:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name FROM household_members ORDER BY id"
            )
        members = [dict(r) for r in rows]
    cards: list[dict[str, Any]] = []
    for m in members:
        member_goals = await store.list_all_for_member(
            m["id"], include_archived=False,
        )
        for g in member_goals:
            cards.append(await _prep_goal(g, m, store))
    return templates.TemplateResponse(
        request=request,
        name="goals.html.j2",
        context={"goals": cards, "members": members},
    )


def _health_goals_store(request: Request) -> HealthGoalsStore:
    store = getattr(request.app.state, "health_goals_store", None)
    if store is not None:
        return store
    return HealthGoalsStore(getattr(request.app.state, "pool", None))


async def _prep_goal(
    goal: dict[str, Any],
    member: dict[str, Any],
    store: HealthGoalsStore,
) -> dict[str, Any]:
    """Shape a goal row for the goals template — pulls latest progress
    + milestones in the same call so the template stays declarative."""
    latest = await store.get_progress(int(goal["id"]))
    milestones = await store.list_milestones(int(goal["id"]))
    today_progress: dict[str, Any] | None = None
    if latest:
        today_progress = {
            "label": latest.get("on_track_label") or "no_data",
            "score": latest.get("on_track_score"),
            "workout_required": latest.get("workout_required"),
            "workout_completed": latest.get("workout_completed"),
            "rest_day_excused": latest.get("rest_day_excused"),
            "day": latest.get("day").isoformat() if latest.get("day") else None,
        }
    return {
        "id": goal["id"],
        "member_id": goal["member_id"],
        "member_name": member.get("name") or f"Member {member.get('id')}",
        "title": goal["title"],
        "description": goal["description"],
        "status": goal["status"],
        "plan_text": goal.get("plan_text") or "",
        "plan_generated_at": _format_dt(goal.get("plan_generated_at")),
        "start_date": goal["start_date"].isoformat() if goal.get("start_date") else None,
        "target_date": goal["target_date"].isoformat() if goal.get("target_date") else None,
        "tracker_spec": goal.get("tracker_spec") or {"trackers": []},
        "today_progress": today_progress,
        "milestones": [
            {
                "id": ms["id"],
                "due_date": ms["due_date"].isoformat() if ms.get("due_date") else None,
                "target_description": ms["target_description"],
                "status": ms["status"],
            }
            for ms in milestones
        ],
    }
    store = _chore_store(request)
    all_status = await store.list_status(include_recent=True)
    buckets: dict[str, list[Any]] = {
        "overdue": [], "due_today": [], "soon": [], "recent": [],
    }
    for s in all_status:
        buckets.setdefault(s.status, []).append(_prep_chore(s))
    return templates.TemplateResponse(
        request=request,
        name="chores.html.j2",
        context={
            "overdue": buckets["overdue"],
            "due_today": buckets["due_today"],
            "soon": buckets["soon"],
            "recent": buckets["recent"],
            "total": len(all_status),
        },
    )


@router.get("/dashboard/orders", response_class=HTMLResponse)
async def orders_page(request: Request) -> HTMLResponse:
    """List recent Noon Minutes orders + show credential / poll status."""
    pool = getattr(request.app.state, "pool", None)
    orders: list[dict[str, Any]] = []
    status: dict[str, Any] = {}
    if pool is not None:
        async with pool.acquire() as conn:
            order_rows = await conn.fetch(
                """
                SELECT id, external_id, status, ordered_at, delivered_at,
                       total_amount, total_currency, item_count, items_json
                FROM noon_orders
                ORDER BY ordered_at DESC NULLS LAST, first_seen_at DESC
                LIMIT 50
                """
            )
            cred = await conn.fetchrow(
                """
                SELECT customer_email, cookie_expires_at, updated_at,
                       last_poll_at, last_poll_status, last_poll_error,
                       (cookies ? '_natnetidv2') AS has_token
                FROM noon_credentials WHERE id = 1
                """
            )
        orders = [_prep_noon_order(r) for r in order_rows]
        if cred is not None:
            status = {
                "configured": bool(cred["has_token"]),
                "customer_email": cred["customer_email"],
                "cookie_expires_at": _format_dt(cred["cookie_expires_at"]),
                "updated_at": _format_dt(cred["updated_at"]),
                "last_poll_at": _format_dt(cred["last_poll_at"]),
                "last_poll_status": cred["last_poll_status"],
                "last_poll_error": cred["last_poll_error"],
            }
    return templates.TemplateResponse(
        request=request,
        name="orders.html.j2",
        context={"orders": orders, "status": status},
    )


def _prep_noon_order(row: Any) -> dict[str, Any]:
    out = dict(row)
    items = out.get("items_json")
    if isinstance(items, str):
        out["items"] = decode_json(items, [])
    elif isinstance(items, list):
        out["items"] = items
    else:
        out["items"] = []
    out["ordered_at"] = _format_dt(out.get("ordered_at"))
    out["delivered_at"] = _format_dt(out.get("delivered_at"))
    return out


def _format_dt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)
