from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from datetime import UTC, datetime, time, timedelta
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from home_agents_sdk.event_log import EventLogStore
from home_agents_sdk.health_store import HealthStore
from home_agents_sdk.reflection_store import ReflectionStore
from home_agents_sdk.telemetry import get_logger

from .data_science.common import current_embedding_model, decode_json
from .github_client import GitHubClientError
from .health import HealthAutoExportNormalizer
from .safety import SafetyPolicy

router = APIRouter(tags=["admin"])
logger = get_logger("orchestrator.admin")


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _reflection_store(request: Request):
    store = getattr(request.app.state, "reflection_store", None)
    if store is not None:
        return store
    return ReflectionStore(getattr(request.app.state, "pool", None))


_HEALTH_METRIC_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")


def _health_store(request: Request) -> HealthStore:
    store = getattr(request.app.state, "health_store", None)
    if store is not None:
        return store
    return HealthStore(getattr(request.app.state, "pool", None))


def _event_log_pool(request: Request) -> Any | None:
    pool = getattr(request.app.state, "pool", None)
    if pool is not None:
        return pool
    store = getattr(request.app.state, "event_log_store", None)
    return getattr(store, "pool", None)


def _format_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _event_log_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    payload = decode_json(data.get("payload"), {})
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return {
        "id": data.get("id"),
        "ts": _format_timestamp(data.get("ts")),
        "agent": data.get("agent"),
        "capability": data.get("capability"),
        "summary": data.get("summary"),
        "payload": payload,
    }


async def _stream_count_since(redis: Any, stream: str, since: datetime) -> int | None:
    xrange = getattr(redis, "xrange", None)
    if not callable(xrange):
        return None
    since_ms = int(since.timestamp() * 1000)
    try:
        rows = await xrange(stream, min=f"{since_ms}-0", max="+", count=10_000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis_stream_count_failed", stream=stream, error=str(exc))
        return None
    return len(rows or [])


def _validate_healthkit_token(request: Request) -> None:
    expected = os.environ.get("HEALTHKIT_WEBHOOK_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="HEALTHKIT_WEBHOOK_TOKEN is not configured; refusing Apple Health sync",
        )
    supplied = request.headers.get("x-health-token") or ""
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid X-Health-Token")


def _query_int(
    request: Request,
    key: str,
    *,
    default: int | None = None,
    low: int = 1,
    high: int = 10_000,
) -> int | None:
    raw = request.query_params.get(key)
    if raw in (None, ""):
        return default
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{key} must be an integer") from exc
    if value < low or value > high:
        raise HTTPException(status_code=400, detail=f"{key} must be between {low} and {high}")
    return value


def _query_metric(request: Request, *, required: bool = False) -> str | None:
    raw = request.query_params.get("metric")
    metric = str(raw or "").strip()
    if not metric:
        if required:
            raise HTTPException(status_code=400, detail="metric is required")
        return None
    if not _HEALTH_METRIC_RE.fullmatch(metric):
        raise HTTPException(status_code=400, detail="metric has invalid characters")
    return metric


async def _default_health_member_id(request: Request) -> int | None:
    graph = getattr(request.app.state, "knowledge_graph", None)
    if graph is None:
        return None
    try:
        members = await graph.list_members(include_pets=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("healthkit_member_resolution_failed", error=str(exc))
        return None
    for member in members:
        if str(member.get("role") or "").casefold() == "adult":
            try:
                return int(member["id"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


async def _record_health_sync_event(
    request: Request,
    *,
    inserted: int,
    skipped: int,
    row_count: int,
    member_id: int | None,
    metrics: list[str],
) -> None:
    event_store = getattr(request.app.state, "event_log_store", None)
    if event_store is None and getattr(request.app.state, "pool", None) is not None:
        event_store = EventLogStore(pool=getattr(request.app.state, "pool"))
    if event_store is None or not callable(getattr(event_store, "record_event", None)):
        return
    try:
        await event_store.record_event(
            agent="health.sync",
            capability="healthkit_sync",
            summary=f"Apple Health sync: {inserted} new rows",
            payload={
                "inserted": inserted,
                "skipped": skipped,
                "row_count": row_count,
                "member_id": member_id,
                "metrics": metrics,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("healthkit_event_log_failed", error=str(exc))


async def _latest_health_values(store: Any, metrics: set[str]) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for metric in sorted(metrics)[:12]:
        try:
            row = await store.latest(metric)
        except Exception as exc:  # noqa: BLE001
            logger.warning("healthkit_latest_failed", metric=metric, error=str(exc))
            continue
        if row:
            latest[metric] = row
    return latest


def _format_proposal_markdown(proposal: dict[str, Any]) -> str:
    evidence = proposal.get("evidence_event_ids") or []
    lines = [
        f"# {proposal.get('title', 'Reflection proposal')}",
        "",
        f"- Kind: `{proposal.get('kind', 'unknown')}`",
        f"- Status: `{proposal.get('status', 'pending')}`",
        f"- Confidence: {float(proposal.get('confidence') or 0.0):.2f}",
        f"- Evidence event ids: {', '.join(str(item) for item in evidence) or 'n/a'}",
        "",
        "## Rationale",
        str(proposal.get("rationale") or "No rationale provided."),
        "",
        "## Implementation prompt",
        "Use the Home-Intelligence repository context. Implement the proposal above as a "
        "small, well-tested change. Cite the evidence event ids before changing code, keep "
        "the change local-first, and do not touch unrelated agent areas.",
    ]
    if proposal.get("cost_estimate"):
        lines.insert(4, f"- Cost: {proposal['cost_estimate']}")
    if proposal.get("impact_estimate"):
        lines.insert(5, f"- Impact: {proposal['impact_estimate']}")
    return "\n".join(lines)


_GITHUB_NOT_CONFIGURED_DETAIL = (
    "GitHub delivery is not configured. Set GITHUB_REPO_TOKEN and GITHUB_REPO."
)
_GITHUB_NOT_CONFIGURED_ERROR = "github not configured"
_COPILOT_WORKFLOW = "copilot-auto-pr.yml"


async def _find_proposal(store: Any, proposal_id: int) -> dict[str, Any]:
    proposals = await store.list_proposals(limit=500)
    proposal = next(
        (item for item in proposals if int(item.get("id") or 0) == proposal_id),
        None,
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Unknown proposal id: {proposal_id}")
    return proposal


def _github_client(request: Request) -> Any | None:
    client = getattr(request.app.state, "github_client", None)
    if client is None:
        return None
    if not bool(getattr(client, "is_configured", True)):
        return None
    return client


async def _record_delivery(
    store: Any,
    proposal_id: int,
    *,
    channel: str,
    github_issue_url: str | None = None,
    github_pr_url: str | None = None,
    error: str | None = None,
) -> None:
    record = getattr(store, "record_delivery", None)
    if callable(record):
        await record(
            proposal_id,
            channel=channel,
            github_issue_url=github_issue_url,
            github_pr_url=github_pr_url,
            error=error,
        )


async def _raise_github_not_configured(store: Any, proposal_id: int, *, channel: str) -> None:
    await _record_delivery(
        store,
        proposal_id,
        channel=channel,
        error=_GITHUB_NOT_CONFIGURED_ERROR,
    )
    raise HTTPException(status_code=503, detail=_GITHUB_NOT_CONFIGURED_DETAIL)


def _proposal_issue_title(proposal_id: int, proposal: dict[str, Any]) -> str:
    return f"[Reflection #{proposal_id}] {proposal.get('title') or 'Reflection proposal'}"


def _proposal_labels(proposal: dict[str, Any]) -> list[str]:
    return ["reflection", f"kind:{proposal.get('kind') or 'unknown'}"]


_KNOWLEDGE_CONFIRM_METHODS = {
    "things": "confirm_thing",
    "habits": "confirm_habit",
    "preferences": "confirm_preference",
    "routines": "confirm_routine",
}
_KNOWLEDGE_FORGET_METHODS = {
    "things": "forget_thing",
    "habits": "forget_habit",
    "preferences": "forget_preference",
    "routines": "forget_routine",
}
_KNOWLEDGE_PATCH_FIELDS = {
    "things": {
        "type",
        "friendly_name",
        "attributes",
        "ha_entity_ids",
        "photo_path",
        "confidence",
        "source",
    },
    "habits": {"subject", "pattern", "frequency", "confidence", "last_observed_at", "source"},
    "preferences": {"value", "confidence", "source"},
    "routines": {"name", "steps", "schedule", "last_run_at", "source"},
}


def _knowledge_graph(request: Request) -> Any:
    graph = getattr(request.app.state, "knowledge_graph", None)
    if graph is None:
        raise HTTPException(status_code=503, detail="knowledge graph unavailable")
    return graph


_ROUTINE_PROFILE_KEYS = ("wake_time", "sleep_time", "work_hours")
_HOUSEHOLD_ROLES = {"adult", "child", "pet", "guest"}


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return body


@router.post("/admin/healthkit/sync")
async def healthkit_sync(request: Request) -> dict[str, Any]:
    _validate_healthkit_token(request)
    payload = await _json_object(request)
    member_id = _query_int(request, "member_id", default=None, low=1, high=2_000_000_000)
    if member_id is None:
        member_id = await _default_health_member_id(request)
    try:
        rows = HealthAutoExportNormalizer.normalize(payload, default_member_id=member_id)
    except Exception as exc:  # noqa: BLE001
        detail = f"invalid Health Auto Export payload: {exc}"
        raise HTTPException(status_code=400, detail=detail) from exc
    store = _health_store(request)
    result = await store.upsert_metrics(rows)
    inserted = int(result.get("inserted") or 0)
    skipped = int(result.get("skipped") or 0)
    metrics = {str(row.get("metric")) for row in rows if row.get("metric")}
    latest = await _latest_health_values(store, metrics)
    await _record_health_sync_event(
        request,
        inserted=inserted,
        skipped=skipped,
        row_count=len(rows),
        member_id=member_id,
        metrics=sorted(metrics),
    )
    return {"ok": True, "inserted": inserted, "skipped": skipped, "latest": latest}


@router.get("/admin/healthkit/recent")
async def healthkit_recent(request: Request) -> dict[str, Any]:
    metric = _query_metric(request)
    hours = _query_int(request, "hours", default=24, low=1, high=24 * 365) or 24
    limit = _query_int(request, "limit", default=20, low=1, high=500) or 20
    rows = (await _health_store(request).list_recent(metric=metric, hours=hours))[:limit]
    return {
        "ok": True,
        "metric": metric,
        "hours": hours,
        "limit": limit,
        "items": rows,
        "count": len(rows),
    }


@router.get("/admin/healthkit/aggregate")
async def healthkit_aggregate(request: Request) -> dict[str, Any]:
    metric = _query_metric(request, required=True)
    days = _query_int(request, "days", default=30, low=1, high=365) or 30
    rows = await _health_store(request).aggregate_daily(str(metric), days=days)
    return {"ok": True, "metric": metric, "days": days, "items": rows, "count": len(rows)}


def _profile_value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and value.strip() != "(skipped)"
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _profile_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "1", "yes", "done", "complete"}
    return bool(value)


def _habit_confirmed(row: dict[str, Any]) -> bool:
    return row.get("last_confirmed_at") is not None


def _entity_count(payload: Any) -> int:
    if isinstance(payload, list):
        return sum(1 for item in payload if isinstance(item, dict) and item.get("entity_id"))
    if not isinstance(payload, dict):
        return 0
    by_area = payload.get("by_area")
    if isinstance(by_area, dict):
        return sum(
            1
            for items in by_area.values()
            if isinstance(items, list)
            for item in items
            if isinstance(item, dict) and item.get("entity_id")
        )
    items = payload.get("items") or payload.get("entities") or []
    if not isinstance(items, list):
        return 0
    return sum(1 for item in items if isinstance(item, dict) and item.get("entity_id"))


async def _ha_entity_count(request: Request) -> int:
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return 0
    try:
        result = await registry.dispatch(
            "home_automation",
            "list_entities",
            {"include_unavailable": True},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("onboarding_list_entities_failed", error=str(exc))
        return 0
    if isinstance(result, dict) and result.get("ok") is False:
        logger.warning("onboarding_list_entities_failed", error=result.get("error"))
        return 0
    payload = result.get("result") if isinstance(result, dict) and "result" in result else result
    return _entity_count(payload)


async def build_onboarding_state(
    request: Request, *, override_stage: int | str | None = None
) -> dict[str, Any]:
    graph = _knowledge_graph(request)
    store = _reflection_store(request)
    things, habits, members, profile_rows, ha_total = await asyncio.gather(
        graph.list_things(),
        graph.list_habits(),
        graph.list_members(include_pets=True),
        store.list_profile(),
        _ha_entity_count(request),
    )
    profile = {str(row.get("key")): row.get("value") for row in profile_rows}
    appliance_count = sum(
        1 for row in things if str(row.get("type") or "").startswith("appliance.")
    )
    discovery_complete = bool(things) and appliance_count >= 3
    missing_profile = [
        key for key in _ROUTINE_PROFILE_KEYS if not _profile_value_present(profile.get(key))
    ]
    routines_complete = not missing_profile
    household_complete = bool(members)
    confirmed_habits = [row for row in habits if _habit_confirmed(row)]
    unconfirmed_habits = [row for row in habits if not _habit_confirmed(row)]
    habits_complete = bool(confirmed_habits)
    completed_flag = _profile_bool(profile.get("onboarding_completed"))

    if override_stage in (1, 2, 3, 4):
        stage: int | str = override_stage
    elif completed_flag:
        stage = "complete"
    elif not discovery_complete:
        stage = 1
    elif not routines_complete:
        stage = 2
    elif not household_complete:
        stage = 3
    else:
        stage = 4

    blockers: list[str] = []
    if stage == 1:
        blockers.append("Identify at least three appliance things in Discovery.")
    elif stage == 2:
        blockers.extend(f"Missing routine profile key: {key}" for key in missing_profile)
    elif stage == 3:
        blockers.append("Add at least one household member.")
    elif stage == 4 and not habits_complete:
        if habits:
            blockers.append("Confirm at least one inferred habit.")
        else:
            blockers.append("No inferred habits yet; observers will surface candidates soon.")

    completed_count = sum(
        bool(value)
        for value in (discovery_complete, routines_complete, household_complete, habits_complete)
    )
    percent_complete = 1.0 if completed_flag else completed_count / 4
    summary = {
        "discovery_summary": {
            "identified": len(things),
            "appliances": appliance_count,
            "total": ha_total,
        },
        "missing_profile_keys": missing_profile,
        "members": members,
        "unconfirmed_habits": unconfirmed_habits[:5],
        "habit_count": len(habits),
        "confirmed_habits": len(confirmed_habits),
        "steps": {
            "discovery": {"complete": discovery_complete},
            "routines": {"complete": routines_complete},
            "household": {"complete": household_complete},
            "habits": {"complete": habits_complete},
        },
    }
    return {
        "stage": stage,
        "percent_complete": percent_complete,
        "current_blockers": blockers,
        "summary": summary,
    }


def _required_discovery_str(body: dict[str, Any], key: str) -> str:
    value = str(body.get(key) or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    return value


def _optional_discovery_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _knowledge_id(table: str, raw_id: Any) -> int | str:
    if table == "preferences":
        key = str(raw_id or "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="id is required")
        return key
    try:
        return int(raw_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="id must be an integer") from exc


def _knowledge_table(table: Any) -> str:
    parsed = str(table or "").strip()
    if parsed not in _KNOWLEDGE_PATCH_FIELDS:
        raise HTTPException(status_code=400, detail="unknown knowledge table")
    return parsed


@router.post("/admin/reload-policies")
async def reload_policies(request: Request) -> dict:
    app_state = request.app.state
    policies = _load_yaml("orchestrator/policies.yaml")
    await app_state.policy_engine.reload(policies)
    safety = getattr(app_state, "safety", None)
    safety_reload = getattr(safety, "reload", None)
    if callable(safety_reload):
        safety_reload()
    schedules_result = await app_state.scheduler.reload()
    reactive_result = await app_state.reactive.reload()
    return {
        "ok": True,
        "policies": len(policies),
        "schedules": schedules_result.get("jobs", 0),
        "triggers": reactive_result.get("triggers", 0),
    }


@router.post("/admin/run-job/{job_id}")
async def run_job(job_id: str, request: Request) -> dict:
    try:
        result = await request.app.state.scheduler.run_job_now(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown job id: {job_id}") from exc
    return {"ok": True, "job_id": job_id, "result": result}


@router.post("/admin/reflection/run")
async def run_reflection(request: Request) -> dict:
    reflector = getattr(request.app.state, "reflector", None)
    if reflector is None:
        raise HTTPException(status_code=503, detail="reflection is not configured")

    # If a run is already in progress, just report its status — don't queue another.
    if reflector.status.get("running"):
        return {"ok": True, "started": False, "status": reflector.status}

    # Kick the reflection off in the background and return immediately so the
    # browser doesn't wait minutes for the LLM. The Morning Brief page polls
    # /admin/reflection/status to know when it's done.
    asyncio.create_task(_safe_run_reflection(reflector), name="reflection-manual")
    return {"ok": True, "started": True, "status": reflector.status}


async def _safe_run_reflection(reflector: Any) -> None:
    try:
        await reflector.run_once()
    except Exception as exc:  # noqa: BLE001
        # NightlyReflector already logs and stores last_error; just swallow here
        # so the background task doesn't fire a noisy "Task exception" warning.
        try:
            reflector._status["last_error"] = str(exc)  # noqa: SLF001
        except Exception:
            pass


@router.get("/admin/reflection/status")
async def reflection_status(request: Request) -> dict:
    reflector = getattr(request.app.state, "reflector", None)
    if reflector is None:
        return {
            "configured": False,
            "running": False,
            "started_at": None,
            "phase": None,
            "elapsed_seconds": None,
            "last_finished_at": None,
            "last_brief_id": None,
            "last_error": None,
            "last_duration_seconds": None,
        }
    return {"configured": True, **reflector.status}


_DATA_SCIENCE_JOBS = {
    "maintenance": ("maintenance", "run"),
    "pattern_mining": ("pattern_miner", "run"),
    "reembed": ("reembed", "run"),
    "weekly_report": ("reports", "weekly_report"),
    "monthly_report": ("reports", "monthly_report"),
    "lora_training": ("lora_training", "run"),
}


@router.post("/admin/data-science/run/{job}")
async def run_data_science_job(job: str, request: Request) -> dict[str, Any]:
    runner = _data_science_runner(request, job)
    lock = getattr(runner["owner"], "_lock", None)
    if lock is not None and lock.locked():
        return {"ok": True, "started": False, "job": job, "status": {"status": "already_running"}}
    asyncio.create_task(
        _safe_run_data_science_job(job, runner["callable"]),
        name=f"data-science-{job}-manual",
    )
    return {"ok": True, "started": True, "job": job}


async def _safe_run_data_science_job(job: str, run_callable: Any) -> None:
    try:
        result = run_callable()
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001
        logger.warning("data_science_manual_job_failed", job=job, error=str(exc))


@router.get("/admin/data-science/status")
async def data_science_status(request: Request) -> dict[str, Any]:
    return {
        "jobs": await _data_science_job_history(request),
        "reports": await _recent_reports(request),
        "embedding": await _embedding_snapshot(request),
    }


@router.get("/admin/reports/{kind}/{period_label}", response_class=PlainTextResponse)
async def get_report_markdown(kind: str, period_label: str, request: Request) -> PlainTextResponse:
    if kind not in {"weekly", "monthly"}:
        raise HTTPException(status_code=404, detail="unknown report kind")
    reports = getattr(request.app.state, "reports", None)
    get_report = getattr(reports, "get_report", None)
    row = None
    if callable(get_report):
        try:
            row = await get_report(kind, period_label)
        except Exception as exc:  # noqa: BLE001
            logger.warning("data_science_report_store_fetch_failed", error=str(exc))
    if row is None:
        row = await _fetch_report_from_db(request, kind, period_label)
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    return PlainTextResponse(str(row.get("body_markdown") or ""), media_type="text/markdown")


def _data_science_runner(request: Request, job: str) -> dict[str, Any]:
    if job not in _DATA_SCIENCE_JOBS:
        raise HTTPException(status_code=404, detail=f"Unknown data science job: {job}")
    state_attr, method_name = _DATA_SCIENCE_JOBS[job]
    owner = getattr(request.app.state, state_attr, None)
    if owner is None:
        raise HTTPException(status_code=503, detail=f"{job} is not configured")
    run_callable = getattr(owner, method_name, None)
    if not callable(run_callable):
        raise HTTPException(status_code=503, detail=f"{job} is not runnable")
    return {"owner": owner, "callable": run_callable}


async def _data_science_job_history(request: Request) -> list[dict[str, Any]]:
    names = list(_DATA_SCIENCE_JOBS)
    latest = {
        name: {"name": name, "last_run_at": None, "last_status": "never", "last_summary": None}
        for name in names
    }
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return list(latest.values())
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (capability)
                       capability, ts, summary, payload
                FROM event_log
                WHERE agent = 'data_science'
                  AND capability = ANY($1::text[])
                ORDER BY capability, ts DESC
                """,
                names,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("data_science_status_history_failed", error=str(exc))
        return list(latest.values())
    for row in rows:
        data = dict(row)
        name = str(data.get("capability") or "")
        payload = decode_json(data.get("payload"), {})
        if not isinstance(payload, dict):
            payload = {}
        latest[name] = {
            "name": name,
            "last_run_at": str(data.get("ts")) if data.get("ts") is not None else None,
            "last_status": str(payload.get("status") or "ok"),
            "last_summary": data.get("summary"),
        }
    return [latest[name] for name in names]


async def _recent_reports(request: Request) -> list[dict[str, Any]]:
    reports = getattr(request.app.state, "reports", None)
    list_recent = getattr(reports, "list_recent_reports", None)
    if callable(list_recent):
        try:
            return await list_recent(limit=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning("data_science_reports_status_failed", error=str(exc))
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT kind, period_label, file_path, summary, generated_at
                FROM reports
                ORDER BY generated_at DESC
                LIMIT 10
                """
            )
        return [dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("data_science_reports_status_failed", error=str(exc))
        return []


async def _embedding_snapshot(request: Request) -> dict[str, Any]:
    reembed = getattr(request.app.state, "reembed", None)
    current_model = getattr(reembed, "current_model", None)
    if current_model is None:
        current_model = current_embedding_model(getattr(request.app.state, "embedder", None))
    pool = getattr(request.app.state, "pool", None)
    stale_count = 0
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                stale_count = int(
                    await conn.fetchval(
                        """
                        SELECT count(*)
                        FROM event_log
                        WHERE embedding_model IS DISTINCT FROM $1
                        """,
                        current_model,
                    )
                    or 0
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("data_science_embedding_status_failed", error=str(exc))
    return {"current_model": current_model, "stale_event_count": stale_count}


async def _fetch_report_from_db(
    request: Request,
    kind: str,
    period_label: str,
) -> dict[str, Any] | None:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT kind, period_label, file_path, summary, body_markdown, generated_at
                FROM reports
                WHERE kind = $1 AND period_label = $2
                """,
                kind,
                period_label,
            )
        return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("data_science_report_fetch_failed", error=str(exc))
        return None


@router.post("/admin/profile/upsert")
async def upsert_profile(request: Request) -> dict[str, Any]:
    body = await request.json()
    key = str(body.get("key") or "").strip()
    value = body.get("value")
    source = str(body.get("source") or "user").strip() or "user"
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise HTTPException(status_code=400, detail="value is required")
    store = _reflection_store(request)
    await store.upsert_profile(key=key, value=value, confidence=1.0, source=source)
    return {"ok": True, "key": key}


@router.post("/admin/profile/skip")
async def skip_profile(request: Request) -> dict[str, Any]:
    """Mark a knowledge gap as skipped so the reflector deprioritises it.

    Implementation: write a sentinel value with low confidence and a special
    source. The reflector's _knowledge_gaps method considers any present key
    as 'covered', so this stops the question from re-appearing.
    """
    body = await request.json()
    key = str(body.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    store = _reflection_store(request)
    await store.upsert_profile(key=key, value="(skipped)", confidence=0.0, source="user_skipped")
    return {"ok": True, "key": key}


def _optional_int(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{key} must be an integer") from exc


def _required_int(body: dict[str, Any], key: str) -> int:
    value = _optional_int(body, key)
    if value is None:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    return value


def _string_list(body: dict[str, Any], key: str) -> list[str]:
    value = body.get(key)
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        if any(not isinstance(item, str) for item in value):
            raise HTTPException(status_code=400, detail=f"{key} must be a list of strings")
        return [item.strip() for item in value if item.strip()]
    raise HTTPException(status_code=400, detail=f"{key} must be a list of strings")


def _optional_hhmm(body: dict[str, Any], key: str) -> time | None:
    value = body.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{key} must be HH:MM")
    parts = value.split(":")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail=f"{key} must be HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        return time(hour=hour, minute=minute)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{key} must be HH:MM") from exc


@router.get("/admin/onboarding/stage")
async def onboarding_stage(request: Request) -> dict[str, Any]:
    return await build_onboarding_state(request)


@router.post("/admin/onboarding/complete")
async def complete_onboarding(request: Request) -> dict[str, Any]:
    _knowledge_graph(request)
    store = _reflection_store(request)
    await store.upsert_profile(
        key="onboarding_completed",
        value=True,
        confidence=1.0,
        source="onboarding_wizard",
    )
    return {"ok": True, "stage": "complete"}


@router.get("/admin/household/list")
async def list_household(request: Request) -> dict[str, Any]:
    graph = _knowledge_graph(request)
    members = await graph.list_members(include_pets=True)
    return {"items": members, "count": len(members)}


@router.post("/admin/household/upsert")
async def upsert_household(request: Request) -> dict[str, Any]:
    body = await _json_object(request)
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    role = str(body.get("role") or "adult").strip().lower()
    if role not in _HOUSEHOLD_ROLES:
        raise HTTPException(status_code=400, detail="role must be adult|child|pet|guest")
    attributes = body.get("attributes") or {}
    if not isinstance(attributes, dict):
        raise HTTPException(status_code=400, detail="attributes must be an object")
    graph = _knowledge_graph(request)
    member = await graph.put_member(
        member_id=_optional_int(body, "id"),
        name=name,
        role=role,
        telegram_chat_id=_optional_int(body, "telegram_chat_id"),
        allergies=_string_list(body, "allergies"),
        dietary_restrictions=_string_list(body, "dietary_restrictions"),
        sleep_time=_optional_hhmm(body, "sleep_time"),
        wake_time=_optional_hhmm(body, "wake_time"),
        attributes=attributes,
    )
    if member is None:
        raise HTTPException(status_code=503, detail="household store unavailable")
    return {"ok": True, "member": member}


@router.post("/admin/household/forget")
async def forget_household(request: Request) -> dict[str, Any]:
    body = await _json_object(request)
    member_id = _required_int(body, "id")
    graph = _knowledge_graph(request)
    await graph.forget_member(member_id)
    return {"ok": True, "id": member_id}


@router.post("/admin/household/{member_id}/trackers")
async def set_member_trackers(member_id: int, request: Request) -> dict[str, Any]:
    """Link presence-tracking HA entity_ids to a household member.

    Body: ``{"tracker_entity_ids": ["person.saeed", "device_tracker.saeeds_iphone"]}``

    Stored in ``household_members.attributes.tracker_entity_ids``. The
    presence observer reads this every 60s and ONLY emits presence.changed
    events for linked entities (unless no member is linked, in which case
    it falls back to the keyword-heuristic safety net).
    """
    body = await _json_object(request)
    raw = body.get("tracker_entity_ids")
    if isinstance(raw, str):
        tracker_ids = [s.strip() for s in raw.split(",") if s.strip()]
    elif isinstance(raw, list):
        tracker_ids = [str(x).strip() for x in raw if str(x).strip()]
    else:
        raise HTTPException(
            status_code=400, detail="tracker_entity_ids must be a list or comma-separated string"
        )

    graph = _knowledge_graph(request)
    members = await graph.list_members(include_pets=True)
    target = next((m for m in members if int(m.get("id") or 0) == member_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"household member {member_id} not found")

    attrs = dict(target.get("attributes") or {})
    attrs["tracker_entity_ids"] = tracker_ids
    # list_members() returns sleep_time/wake_time as 'HH:MM' strings; put_member
    # wants datetime.time objects. Tolerate both via auto_setup._parse_hhmm.
    from .auto_setup import _parse_hhmm

    member = await graph.put_member(
        member_id=member_id,
        name=str(target.get("name") or ""),
        role=str(target.get("role") or "adult"),
        telegram_chat_id=target.get("telegram_chat_id"),
        allergies=list(target.get("allergies") or []),
        dietary_restrictions=list(target.get("dietary_restrictions") or []),
        sleep_time=_parse_hhmm(target.get("sleep_time")),
        wake_time=_parse_hhmm(target.get("wake_time")),
        attributes=attrs,
    )
    return {"ok": True, "id": member_id, "tracker_entity_ids": tracker_ids, "member": member}


@router.get("/admin/setup/auto-discover")
async def setup_auto_discover(request: Request) -> dict[str, Any]:
    """Survey HA, return a proposal of things to adopt + member presence links.

    Read-only; nothing is written to the DB. Pair with POST /admin/setup/auto-apply
    to commit the proposal (or a user-edited version of it).
    """
    from .auto_setup import discover_proposal

    graph = _knowledge_graph(request)
    return await discover_proposal(knowledge_graph=graph)


@router.post("/admin/setup/auto-apply")
async def setup_auto_apply(request: Request) -> dict[str, Any]:
    """Apply an auto-discover proposal: adopt things + link presence trackers.

    Idempotent: skips things already adopted, no-ops empty link lists.
    """
    from .auto_setup import apply_proposal

    body = await _json_object(request)
    graph = _knowledge_graph(request)
    return await apply_proposal(proposal=body, knowledge_graph=graph)


@router.post("/admin/safety/explain")
async def explain_safety(request: Request) -> dict[str, Any]:
    body = await request.json()
    agent = str(body.get("agent") or "").strip()
    capability = str(body.get("capability") or "").strip()
    inputs = body.get("inputs") or {}
    if not agent or not capability:
        raise HTTPException(status_code=400, detail="agent and capability are required")
    if not isinstance(inputs, dict):
        raise HTTPException(status_code=400, detail="inputs must be an object")
    safety = getattr(request.app.state, "safety", None) or SafetyPolicy(
        os.environ.get("SAFETY_POLICY_PATH", "policies/safety.yaml")
    )
    return {"ok": True, **safety.explain(agent, capability, inputs)}


@router.post("/admin/proposals/{proposal_id}/format")
async def format_proposal(proposal_id: int, request: Request) -> dict:
    proposal = await _find_proposal(_reflection_store(request), proposal_id)
    return {"ok": True, "proposal_id": proposal_id, "markdown": _format_proposal_markdown(proposal)}


@router.post("/admin/proposals/{proposal_id}/github-issue")
async def open_proposal_as_issue(proposal_id: int, request: Request) -> dict:
    store = _reflection_store(request)
    proposal = await _find_proposal(store, proposal_id)
    if proposal.get("github_issue_url"):
        return {
            "ok": True,
            "already_dispatched": True,
            "url": proposal["github_issue_url"],
        }

    client = _github_client(request)
    if client is None:
        await _raise_github_not_configured(store, proposal_id, channel="github_issue")

    body = _format_proposal_markdown(proposal)
    try:
        issue = await client.open_issue(
            title=_proposal_issue_title(proposal_id, proposal),
            body=body,
            labels=_proposal_labels(proposal),
        )
    except GitHubClientError as exc:
        await _record_delivery(
            store,
            proposal_id,
            channel="github_issue",
            error=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    url = str(issue.get("html_url") or "")
    await _record_delivery(
        store,
        proposal_id,
        channel="github_issue",
        github_issue_url=url,
    )
    return {
        "ok": True,
        "url": url,
        "number": issue.get("number"),
        "already_dispatched": False,
    }


@router.post("/admin/proposals/{proposal_id}/copilot-dispatch")
async def dispatch_to_copilot(proposal_id: int, request: Request) -> dict:
    store = _reflection_store(request)
    proposal = await _find_proposal(store, proposal_id)
    if proposal.get("github_pr_url") or (
        proposal.get("dispatched_at") and not proposal.get("dispatch_error")
    ):
        return {
            "ok": True,
            "already_dispatched": True,
            "issue_url": proposal.get("github_issue_url"),
            "pr_url": proposal.get("github_pr_url"),
            "message": "This proposal was already dispatched.",
        }

    client = _github_client(request)
    if client is None:
        await _raise_github_not_configured(store, proposal_id, channel="copilot_dispatch")

    body = _format_proposal_markdown(proposal)
    try:
        issue = await client.open_issue(
            title=_proposal_issue_title(proposal_id, proposal),
            body=body,
            labels=_proposal_labels(proposal),
        )
    except GitHubClientError as exc:
        await _record_delivery(
            store,
            proposal_id,
            channel="copilot_dispatch",
            error=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    issue_number = str(issue.get("number") or "")
    issue_url = str(issue.get("html_url") or "")
    inputs = {
        "proposal_id": str(proposal_id),
        "issue_number": issue_number,
        "issue_url": issue_url,
        "title": str(proposal.get("title") or "Reflection proposal"),
        "prompt_markdown": body,
    }
    try:
        await client.dispatch_workflow(
            _COPILOT_WORKFLOW,
            os.environ.get("GITHUB_WORKFLOW_REF", "main"),
            inputs,
        )
    except GitHubClientError as exc:
        await _record_delivery(
            store,
            proposal_id,
            channel="copilot_dispatch",
            github_issue_url=issue_url,
            error=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await _record_delivery(
        store,
        proposal_id,
        channel="copilot_dispatch",
        github_issue_url=issue_url,
    )
    return {
        "ok": True,
        "issue_url": issue_url,
        "already_dispatched": False,
        "message": "Copilot will respond on that issue and open a PR if it can.",
    }


@router.get("/admin/policies")
async def get_policies(request: Request) -> dict:
    return request.app.state.policy_engine.policies


@router.post("/admin/knowledge/confirm")
async def knowledge_confirm(request: Request) -> dict:
    body = await request.json()
    table = _knowledge_table(body.get("table"))
    row_id = _knowledge_id(table, body.get("id"))
    graph = _knowledge_graph(request)
    method = getattr(graph, _KNOWLEDGE_CONFIRM_METHODS[table], None)
    if method is None:
        raise HTTPException(status_code=400, detail="confirm is not supported for table")
    item = await method(row_id)
    if item is None:
        raise HTTPException(status_code=404, detail="knowledge row not found")
    return {"ok": True, "table": table, "id": row_id, "item": item}


@router.post("/admin/discovery/adopt")
async def discovery_adopt(request: Request) -> dict[str, Any]:
    body = await request.json()
    entity_id = _required_discovery_str(body, "entity_id")
    thing_type = _required_discovery_str(body, "type")
    friendly_name = _required_discovery_str(body, "friendly_name")
    photo_path = _optional_discovery_str(body, "photo_path")
    graph = _knowledge_graph(request)
    try:
        thing = await graph.put_thing(
            type=thing_type,
            friendly_name=friendly_name,
            attributes={},
            ha_entity_ids=[entity_id],
            photo_path=photo_path,
            confidence=1.0,
            source="discovery_user",
        )
    except Exception as exc:
        logger.warning("discovery_adopt_failed", entity_id=entity_id, error=str(exc))
        raise HTTPException(status_code=500, detail="discovery adopt failed") from exc
    if thing is None:
        raise HTTPException(status_code=503, detail="knowledge graph unavailable")
    return {"ok": True, "thing": thing}


@router.post("/admin/discovery/ignore")
async def discovery_ignore(request: Request) -> dict[str, Any]:
    body = await request.json()
    entity_id = _required_discovery_str(body, "entity_id")
    graph = _knowledge_graph(request)
    try:
        thing = await graph.put_thing(
            type="ignored.entity",
            friendly_name=entity_id,
            attributes={},
            ha_entity_ids=[entity_id],
            confidence=1.0,
            source="discovery_user",
        )
    except Exception as exc:
        logger.warning("discovery_ignore_failed", entity_id=entity_id, error=str(exc))
        raise HTTPException(status_code=500, detail="discovery ignore failed") from exc
    if thing is None:
        raise HTTPException(status_code=503, detail="knowledge graph unavailable")
    return {"ok": True, "thing": thing}


@router.get("/admin/knowledge/evidence")
async def knowledge_evidence(table: str, id: str, request: Request) -> dict:  # noqa: A002
    parsed_table = _knowledge_table(table)
    row_id = _knowledge_id(parsed_table, id)
    graph = _knowledge_graph(request)
    return {"items": await graph.evidence_for(parsed_table, row_id)}


@router.post("/admin/knowledge/forget")
async def knowledge_forget(request: Request) -> dict:
    body = await request.json()
    table = _knowledge_table(body.get("table"))
    row_id = _knowledge_id(table, body.get("id"))
    graph = _knowledge_graph(request)
    method = getattr(graph, _KNOWLEDGE_FORGET_METHODS[table], None)
    if method is None:
        raise HTTPException(status_code=400, detail="forget is not supported for table")
    deleted = await method(row_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="knowledge row not found")
    return {"ok": True, "table": table, "id": row_id, "deleted": True}


@router.patch("/admin/knowledge/{table}/{row_id}")
async def knowledge_patch(table: str, row_id: str, request: Request) -> dict:
    parsed_table = _knowledge_table(table)
    parsed_id = _knowledge_id(parsed_table, row_id)
    body = await request.json()
    updates = {
        key: value for key, value in body.items() if key in _KNOWLEDGE_PATCH_FIELDS[parsed_table]
    }
    if not updates:
        raise HTTPException(status_code=400, detail="no editable fields supplied")
    graph = _knowledge_graph(request)
    item = await graph.patch_row(parsed_table, parsed_id, updates)
    if item is None:
        raise HTTPException(status_code=404, detail="knowledge row not found")
    return {"ok": True, "table": parsed_table, "id": parsed_id, "item": item}


# === Dashboard button endpoints ============================================
# These power the read-write actions exposed by the live dashboard. The
# dashboard JS POSTs to them via fetch(); the resulting state changes are
# observed by the user through the SSE stream within a second or two.


@router.post("/admin/quiet/{state}")
async def set_quiet(state: str, request: Request) -> dict:
    if state not in {"on", "off", "clear"}:
        raise HTTPException(status_code=400, detail="state must be on|off|clear")
    policy_engine = request.app.state.policy_engine
    if state == "clear":
        await policy_engine.clear_quiet_override()
        return {"ok": True, "quiet": None}
    # Override TTL: 8h for `on`, 12h for `off` so the user can sleep through it.
    ttl_seconds = 8 * 3600 if state == "on" else 12 * 3600
    await policy_engine.set_quiet_override(state, ttl_seconds)
    return {"ok": True, "quiet": state, "ttl_seconds": ttl_seconds}


@router.post("/admin/mute")
async def mute(request: Request) -> dict:
    body = await request.json()
    key = str(body.get("key", "")).strip()
    minutes_raw = body.get("minutes")
    minutes = int(minutes_raw) if minutes_raw is not None else 30
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    if minutes <= 0 or minutes > 24 * 60:
        raise HTTPException(status_code=400, detail="minutes must be 1..1440")
    redis = request.app.state.redis
    await redis.set(f"policy:mute:{key}", "1", ex=minutes * 60)
    return {"ok": True, "key": key, "minutes": minutes}


@router.post("/admin/unmute")
async def unmute(request: Request) -> dict:
    body = await request.json()
    key = str(body.get("key", "")).strip()
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    redis = request.app.state.redis
    deleted = await redis.delete(f"policy:mute:{key}")
    return {"ok": True, "key": key, "deleted": int(deleted)}


@router.post("/admin/invoke")
async def invoke_capability(request: Request) -> dict:
    body = await request.json()
    agent = str(body.get("agent", "")).strip()
    capability = str(body.get("capability", "")).strip()
    payload = body.get("payload") or {}
    if not agent or not capability:
        raise HTTPException(status_code=400, detail="agent and capability are required")
    registry = request.app.state.registry
    try:
        result = await registry.dispatch(agent, capability, payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"agent error: {exc}") from exc
    return {"ok": True, "agent": agent, "capability": capability, "result": result}


@router.post("/admin/replay")
async def replay_event(request: Request) -> dict:
    body = await request.json()
    stream = str(body.get("stream", "")).strip()
    payload = body.get("payload")
    if not stream or payload is None:
        raise HTTPException(status_code=400, detail="stream and payload are required")
    redis = request.app.state.redis
    msg_id = await redis.xadd(stream, {"payload": json.dumps(payload, default=str)})
    return {"ok": True, "stream": stream, "id": str(msg_id)}


@router.get("/admin/activity/snapshot")
async def activity_snapshot(request: Request) -> dict[str, Any]:
    aggregator = request.app.state.activity_aggregator
    snapshot = aggregator.snapshot()
    snapshot["recent"] = aggregator.recent_events(limit=50)
    return snapshot


@router.get("/admin/ha-bridge/status")
async def ha_bridge_status(request: Request) -> dict[str, Any]:
    bridge = getattr(request.app.state, "ha_event_bridge", None)
    if bridge is None:
        return {
            "ok": False,
            "error": "ha_event_bridge not started",
            "enabled": False,
            "events_forwarded_last_hour": None,
        }
    status = {"ok": True, **bridge.status.snapshot()}
    stream = str(getattr(bridge, "_stream", "events.home") or "events.home")
    status["events_forwarded_last_hour"] = await _stream_count_since(
        getattr(request.app.state, "redis", None),
        stream,
        datetime.now(UTC) - timedelta(hours=1),
    )
    return status


@router.post("/admin/ha-bridge/replay")
async def ha_bridge_replay(request: Request) -> dict[str, Any]:
    bridge = getattr(request.app.state, "ha_event_bridge", None)
    if bridge is None:
        raise HTTPException(status_code=503, detail="ha_event_bridge not started")
    hours_raw = request.query_params.get("hours")
    hours: int | None = None
    if hours_raw is not None:
        try:
            hours = int(hours_raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="hours must be an integer") from None
        if hours < 0:
            raise HTTPException(status_code=400, detail="hours must be >= 0")
    return await bridge.replay_history_now(hours=hours)


@router.get("/admin/migrations/status")
async def migrations_status(request: Request) -> dict[str, Any]:
    """Report which init/*.sql migrations have been applied (and any errors).

    Useful for verifying after a deploy that the new tables exist; replaces
    the manual `psql` check that the user had to run when cycle_loads silently
    didn't exist.
    """
    results = getattr(request.app.state, "migration_results", None)
    if results is None:
        return {"ok": False, "error": "migrations have not run yet"}
    summary = {
        "applied": sum(1 for r in results if r["status"] == "applied"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "error": sum(1 for r in results if r["status"] == "error"),
    }
    return {"ok": summary["error"] == 0, "summary": summary, "results": results}


@router.get("/admin/intelligence/summary")
async def intelligence_summary(request: Request) -> dict[str, Any]:
    """One big snapshot of "what the system has actually learned about you".

    Powers the /dashboard/what-i-know page. Aggregates household members,
    confirmed habits, devices by type, recent inferences (with confirmation
    counts), observation kinds in last 24h, health-data presence, and a list
    of open questions awaiting user reply.
    """
    from .intelligence import gather_intelligence_summary

    return await gather_intelligence_summary(request)


@router.get("/admin/observations/recent")
async def observations_recent(request: Request) -> dict[str, Any]:
    limit = _query_int(request, "limit", default=10, low=1, high=50) or 10
    pool = _event_log_pool(request)
    if pool is None:
        return {"ok": True, "items": [], "count": 0, "limit": limit}
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, ts, agent, capability, summary, payload
                FROM event_log
                WHERE agent LIKE 'observer.%'
                ORDER BY ts DESC
                LIMIT $1
                """,
                limit,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("observations_recent_failed", error=str(exc))
        return {
            "ok": False,
            "items": [],
            "count": 0,
            "limit": limit,
            "error": "event_log_unavailable",
        }
    items = [_event_log_row(row) for row in rows]
    return {"ok": True, "items": items, "count": len(items), "limit": limit}
