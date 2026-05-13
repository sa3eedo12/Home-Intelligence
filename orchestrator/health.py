from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx
from redis.asyncio import Redis

_HEALTHKIT_METRICS = {
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierActiveEnergyBurned": "active_energy",
    "HKQuantityTypeIdentifierHeartRate": "heart_rate",
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_heart_rate",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv",
    "HKQuantityTypeIdentifierBodyMass": "weight",
    "HKQuantityTypeIdentifierOxygenSaturation": "blood_oxygen",
    "HKQuantityTypeIdentifierVO2Max": "vo2_max",
    "HKCategoryTypeIdentifierMindfulSession": "mindfulness",
    "HKWorkoutTypeIdentifier": "workout",
}

_FRIENDLY_IDENTIFIERS = {
    "steps": "HKQuantityTypeIdentifierStepCount",
    "stepcount": "HKQuantityTypeIdentifierStepCount",
    "step_count": "HKQuantityTypeIdentifierStepCount",
    "activeenergy": "HKQuantityTypeIdentifierActiveEnergyBurned",
    "activeenergyburned": "HKQuantityTypeIdentifierActiveEnergyBurned",
    "heart_rate": "HKQuantityTypeIdentifierHeartRate",
    "heartrate": "HKQuantityTypeIdentifierHeartRate",
    "restingheartrate": "HKQuantityTypeIdentifierRestingHeartRate",
    "resting_heart_rate": "HKQuantityTypeIdentifierRestingHeartRate",
    "hrv": "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
    "bodymass": "HKQuantityTypeIdentifierBodyMass",
    "weight": "HKQuantityTypeIdentifierBodyMass",
    "oxygensaturation": "HKQuantityTypeIdentifierOxygenSaturation",
    "vo2max": "HKQuantityTypeIdentifierVO2Max",
    "sleepanalysis": "HKCategoryTypeIdentifierSleepAnalysis",
    "sleep": "HKCategoryTypeIdentifierSleepAnalysis",
    "mindfulsession": "HKCategoryTypeIdentifierMindfulSession",
    "mindfulness": "HKCategoryTypeIdentifierMindfulSession",
    "workout": "HKWorkoutTypeIdentifier",
}

_DEFAULT_UNITS = {
    "steps": "steps",
    "active_energy": "kcal",
    "heart_rate": "bpm",
    "resting_heart_rate": "bpm",
    "hrv": "ms",
    "weight": "kg",
    "blood_oxygen": "%",
    "vo2_max": "mL/kg/min",
    "mindfulness": "min",
    "workout": "min",
}

_SAMPLE_LIST_KEYS = ("data", "samples", "values", "entries", "records", "items")
_START_KEYS = (
    "startDate",
    "start_date",
    "started_at",
    "start",
    "from",
    "date",
    "timestamp",
    "time",
    "creationDate",
)
_END_KEYS = ("endDate", "end_date", "ended_at", "end", "to")
_VALUE_KEYS = ("qty", "value", "quantity", "sum", "average", "avg", "count", "total", "amount")
_DURATION_KEYS = (
    "durationMin",
    "duration_min",
    "durationMinutes",
    "duration_minutes",
    "durationInMinutes",
    "duration",
    "durationSec",
    "duration_sec",
    "durationSeconds",
    "duration_seconds",
    "durationInSeconds",
)
_STAGE_KEYS = ("stage", "sleepStage", "sleep_stage", "value", "category", "name")

_SLEEP_STAGE_BY_INT = {
    0: "inBed",
    1: "asleep",
    2: "awake",
    3: "core",
    4: "deep",
    5: "rem",
}

_SLEEP_STAGE_METRICS = {
    "awake": "sleep_awake",
    "inbed": "sleep_inBed",
    "asleepdeep": "sleep_deep",
    "deep": "sleep_deep",
    "asleeprem": "sleep_rem",
    "rem": "sleep_rem",
    "asleepcore": "sleep_core",
    "core": "sleep_core",
    "asleepunspecified": "sleep_asleep",
    "asleep": "sleep_asleep",
}


class HealthAutoExportNormalizer:
    @staticmethod
    def normalize(
        payload: dict[str, Any], default_member_id: int | None = None
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        rows: list[dict[str, Any]] = []
        for metric_payload in _list_value(data.get("metrics")):
            if not isinstance(metric_payload, dict):
                continue
            parent_type = _type_identifier(metric_payload)
            for sample in _samples(metric_payload):
                if not isinstance(sample, dict):
                    continue
                type_id = _type_identifier(sample) or parent_type
                if type_id == "HKCategoryTypeIdentifierSleepAnalysis":
                    row = _sleep_row(sample, metric_payload, default_member_id)
                elif type_id == "HKWorkoutTypeIdentifier":
                    row = _workout_row(sample, default_member_id, parent=metric_payload)
                elif type_id == "HKCategoryTypeIdentifierMindfulSession":
                    row = _duration_row(
                        sample,
                        type_id=type_id,
                        metric="mindfulness",
                        unit="min",
                        member_id=default_member_id,
                        parent=metric_payload,
                    )
                else:
                    row = _quantity_row(sample, type_id, default_member_id, metric_payload)
                if row is not None:
                    rows.append(row)

        for workout in _list_value(data.get("workouts")):
            if isinstance(workout, dict) and (row := _workout_row(workout, default_member_id)):
                rows.append(row)
        return rows


async def probe_ollama(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url.rstrip('/')}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            return {"ok": True, "models": models}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def probe_lemonade(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url.rstrip('/')}/v1/models")
            resp.raise_for_status()
            data = resp.json()
            models = [m.get("id", "") for m in data.get("data", [])]
            return {"ok": True, "models": models}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def probe_postgres(pool) -> dict:
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def probe_redis(redis_url: str) -> dict:
    try:
        client = Redis.from_url(redis_url, decode_responses=True)
        await client.ping()
        await client.aclose()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def probe_qdrant(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url.rstrip('/')}/healthz")
            resp.raise_for_status()
            return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _samples(metric_payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in _SAMPLE_LIST_KEYS:
        value = metric_payload.get(key)
        if isinstance(value, list):
            return [sample for sample in value if isinstance(sample, dict)]
    return [metric_payload]


def _type_identifier(payload: dict[str, Any]) -> str | None:
    for key in ("type", "identifier", "id", "metric", "name", "quantityType", "categoryType"):
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        canonical = _canonical_identifier(str(raw))
        if canonical:
            return canonical
    return None


def _canonical_identifier(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if value.startswith("HK"):
        return value
    compact = re.sub(r"[^a-z0-9]+", "", value.casefold())
    underscored = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return _FRIENDLY_IDENTIFIERS.get(compact) or _FRIENDLY_IDENTIFIERS.get(underscored) or value


def _parse_ts(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, int | float):
        value = float(raw)
        if value > 10_000_000_000:
            value /= 1000
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.isdigit():
        return _parse_ts(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _timestamp(payload: dict[str, Any], keys: tuple[str, ...]) -> datetime | None:
    for key in keys:
        if key in payload and (ts := _parse_ts(payload.get(key))) is not None:
            return ts
    return None


def _numeric(payload: dict[str, Any], keys: tuple[str, ...] = _VALUE_KEYS) -> float | None:
    for key in keys:
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


def _duration_minutes(
    payload: dict[str, Any], start: datetime, end: datetime | None
) -> float | None:
    for key in _DURATION_KEYS:
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            continue
        key_lower = key.lower()
        if "sec" in key_lower or value > 24 * 60:
            return round(value / 60, 3)
        return round(value, 3)
    if end is None:
        return None
    return round(max(0.0, (end - start).total_seconds() / 60), 3)


def _base_row(
    *,
    metric: str,
    started_at: datetime,
    ended_at: datetime | None,
    value: float | None,
    unit: str | None,
    member_id: int | None,
    metadata: dict[str, Any],
    raw: dict[str, Any],
) -> dict[str, Any]:
    return {
        "metric": metric,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat() if ended_at else None,
        "value": value,
        "unit": unit,
        "source": "health_auto_export",
        "member_id": member_id,
        "metadata": metadata,
        "raw": raw,
    }


def _raw(parent: dict[str, Any] | None, sample: dict[str, Any]) -> dict[str, Any]:
    if not parent:
        return dict(sample)
    parent_meta = {key: value for key, value in parent.items() if key not in _SAMPLE_LIST_KEYS}
    return {"metric": parent_meta, "sample": dict(sample)}


def _metadata(type_id: str | None, sample: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if type_id:
        metadata["healthkit_type"] = type_id
    for key in ("source", "sourceName", "device", "deviceName"):
        if sample.get(key) not in (None, ""):
            metadata[key] = sample[key]
    return metadata


def _unit(metric: str, sample: dict[str, Any], parent: dict[str, Any] | None = None) -> str | None:
    raw = sample.get("unit") or sample.get("units")
    if raw in (None, "") and parent:
        raw = parent.get("unit") or parent.get("units")
    if raw in (None, ""):
        return _DEFAULT_UNITS.get(metric)
    text = str(raw).strip()
    if metric == "steps" and text.casefold() in {"count", "counts", "ct"}:
        return "steps"
    if metric == "active_energy" and text.casefold() in {"cal", "calories", "kcal"}:
        return "kcal"
    if metric == "blood_oxygen" and text in {"fraction", "ratio"}:
        return "%"
    return text


def _quantity_row(
    sample: dict[str, Any],
    type_id: str | None,
    member_id: int | None,
    parent: dict[str, Any] | None,
) -> dict[str, Any] | None:
    started_at = _timestamp(sample, _START_KEYS)
    if started_at is None:
        return None
    ended_at = _timestamp(sample, _END_KEYS)
    metric = _HEALTHKIT_METRICS.get(type_id or "") or f"other.{_unknown_suffix(type_id)}"
    return _base_row(
        metric=metric,
        started_at=started_at,
        ended_at=ended_at,
        value=_numeric(sample),
        unit=_unit(metric, sample, parent),
        member_id=member_id,
        metadata=_metadata(type_id, sample),
        raw=_raw(parent, sample),
    )


def _duration_row(
    sample: dict[str, Any],
    *,
    type_id: str,
    metric: str,
    unit: str,
    member_id: int | None,
    parent: dict[str, Any] | None,
) -> dict[str, Any] | None:
    started_at = _timestamp(sample, _START_KEYS)
    if started_at is None:
        return None
    ended_at = _timestamp(sample, _END_KEYS)
    return _base_row(
        metric=metric,
        started_at=started_at,
        ended_at=ended_at,
        value=_duration_minutes(sample, started_at, ended_at),
        unit=unit,
        member_id=member_id,
        metadata=_metadata(type_id, sample),
        raw=_raw(parent, sample),
    )


def _sleep_row(
    sample: dict[str, Any], parent: dict[str, Any], member_id: int | None
) -> dict[str, Any] | None:
    started_at = _timestamp(sample, _START_KEYS)
    if started_at is None:
        return None
    ended_at = _timestamp(sample, _END_KEYS)
    stage = _sleep_stage(sample)
    metric = _sleep_metric(stage)
    metadata = _metadata("HKCategoryTypeIdentifierSleepAnalysis", sample)
    metadata["sleep_stage"] = stage
    return _base_row(
        metric=metric,
        started_at=started_at,
        ended_at=ended_at,
        value=_duration_minutes(sample, started_at, ended_at),
        unit="min",
        member_id=member_id,
        metadata=metadata,
        raw=_raw(parent, sample),
    )


def _sleep_stage(sample: dict[str, Any]) -> str:
    for key in _STAGE_KEYS:
        raw = sample.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, int) and raw in _SLEEP_STAGE_BY_INT:
            return _SLEEP_STAGE_BY_INT[raw]
        raw_text = str(raw).strip()
        if raw_text.isdigit() and int(raw_text) in _SLEEP_STAGE_BY_INT:
            return _SLEEP_STAGE_BY_INT[int(raw_text)]
        text = raw_text.split(".")[-1].replace("HKCategoryValueSleepAnalysis", "")
        if text:
            return text
    return "asleep"


def _sleep_metric(stage: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "", stage.casefold())
    return _SLEEP_STAGE_METRICS.get(compact, f"sleep_{compact or 'asleep'}")


def _workout_row(
    workout: dict[str, Any], member_id: int | None, parent: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    started_at = _timestamp(workout, _START_KEYS)
    if started_at is None:
        return None
    ended_at = _timestamp(workout, _END_KEYS)
    workout_type = (
        workout.get("workoutActivityType")
        or workout.get("activityType")
        or workout.get("activity")
        or workout.get("name")
        or "workout"
    )
    metadata = _metadata("HKWorkoutTypeIdentifier", workout)
    metadata["workout_type"] = workout_type
    for key in ("activeEnergy", "activeEnergyBurned", "totalEnergyBurned", "distance"):
        if workout.get(key) not in (None, ""):
            metadata[key] = workout[key]
    return _base_row(
        metric="workout",
        started_at=started_at,
        ended_at=ended_at,
        value=_duration_minutes(workout, started_at, ended_at),
        unit="min",
        member_id=member_id,
        metadata=metadata,
        raw=_raw(parent, workout),
    )


def _unknown_suffix(type_id: str | None) -> str:
    return str(type_id or "unknown").strip().replace(" ", "_") or "unknown"
