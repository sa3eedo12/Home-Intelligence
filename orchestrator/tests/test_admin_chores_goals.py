"""Tests for the /admin/chores, /admin/members/{id}/nag-windows endpoints
and the new /dashboard/chores + /dashboard/goals pages."""
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_agents_sdk.chore_store import ChoreStatus
from orchestrator.admin import router as admin_router
from orchestrator.dashboard import router as dashboard_router


# ── Helpers ──────────────────────────────────────────────────────


def _build_app(
    *,
    chore_store=None,
    nag_store=None,
    goals_store=None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(dashboard_router)
    if chore_store is not None:
        app.state.chore_store = chore_store
    if nag_store is not None:
        app.state.member_nag_windows_store = nag_store
    if goals_store is not None:
        app.state.health_goals_store = goals_store
    return app


def _stub_status(**overrides) -> ChoreStatus:
    base = dict(
        template_id=1,
        name="Vacuum the house",
        category="cleaning",
        cadence_days=7,
        grace_days=2,
        auto_detect_kind="vacuum",
        auto_detect_entity=None,
        last_done_at=datetime(2026, 5, 20, 9, 0, tzinfo=UTC),
        last_done_by=2,
        next_due_on=date(2026, 5, 27),
        days_overdue=2,
        status="overdue",
        description="Run the vacuum.",
    )
    base.update(overrides)
    return ChoreStatus(**base)


# ── /admin/chores ───────────────────────────────────────────────


def test_list_chores_returns_grouped_counts() -> None:
    store = SimpleNamespace(
        list_status=AsyncMock(return_value=[
            _stub_status(template_id=1, status="overdue"),
            _stub_status(template_id=2, name="Mop", category="cleaning",
                         status="due_today", days_overdue=0,
                         next_due_on=date(2026, 5, 29)),
            _stub_status(template_id=3, name="Plants", category="plants",
                         status="recent", days_overdue=-5,
                         next_due_on=date(2026, 6, 3)),
        ]),
    )
    with TestClient(_build_app(chore_store=store)) as client:
        res = client.get("/admin/chores")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["counts"]["overdue"] == 1
    assert body["counts"]["due_today"] == 1
    assert body["counts"]["recent"] == 1
    names = sorted(c["name"] for c in body["chores"])
    assert names == ["Mop", "Plants", "Vacuum the house"]


def test_complete_chore_logs_completion() -> None:
    store = SimpleNamespace(
        log_completion=AsyncMock(return_value=42),
    )
    with TestClient(_build_app(chore_store=store)) as client:
        res = client.post(
            "/admin/chores/1/complete",
            json={"source": "dashboard", "note": "did living room"},
        )
    assert res.status_code == 200
    assert res.json() == {"ok": True, "log_id": 42}
    store.log_completion.assert_awaited_once()
    call = store.log_completion.await_args
    assert call.args == (1,)
    assert call.kwargs["source"] == "dashboard"
    assert call.kwargs["note"] == "did living room"


def test_complete_chore_returns_503_when_pool_unavailable() -> None:
    store = SimpleNamespace(
        log_completion=AsyncMock(return_value=None),
    )
    with TestClient(_build_app(chore_store=store)) as client:
        res = client.post("/admin/chores/1/complete", json={})
    assert res.status_code == 503


# ── /admin/members/{id}/nag-windows ─────────────────────────────


def test_get_nag_windows_returns_defaults() -> None:
    store = SimpleNamespace(
        get=AsyncMock(return_value={
            "member_id": 2, "weekday_start_hour": 14, "weekday_end_hour": 21,
            "weekend_start_hour": 10, "weekend_end_hour": 21,
            "timezone": "Asia/Dubai", "is_default": True,
        }),
    )
    with TestClient(_build_app(nag_store=store)) as client:
        res = client.get("/admin/members/2/nag-windows")
    assert res.status_code == 200
    body = res.json()
    assert body["windows"]["weekday_start_hour"] == 14
    assert body["windows"]["is_default"] is True


def test_set_nag_windows_passes_only_provided_fields() -> None:
    store = SimpleNamespace(
        set=AsyncMock(return_value={
            "member_id": 2, "weekday_start_hour": 18, "weekday_end_hour": 21,
            "weekend_start_hour": 10, "weekend_end_hour": 21,
            "timezone": "Asia/Dubai", "is_default": False,
        }),
    )
    with TestClient(_build_app(nag_store=store)) as client:
        res = client.post(
            "/admin/members/2/nag-windows",
            json={"weekday_start_hour": 18},
        )
    assert res.status_code == 200
    assert res.json()["windows"]["weekday_start_hour"] == 18
    store.set.assert_awaited_once()
    call_kwargs = store.set.await_args.kwargs
    assert call_kwargs == {"weekday_start_hour": 18}


def test_set_nag_windows_rejects_validation_failure() -> None:
    store = SimpleNamespace(
        set=AsyncMock(side_effect=ValueError("weekday end (5) must be > start (18)")),
    )
    with TestClient(_build_app(nag_store=store)) as client:
        res = client.post(
            "/admin/members/2/nag-windows",
            json={"weekday_start_hour": 18, "weekday_end_hour": 5},
        )
    assert res.status_code == 400


def test_set_nag_windows_rejects_non_int_hour() -> None:
    store = SimpleNamespace(set=AsyncMock())
    with TestClient(_build_app(nag_store=store)) as client:
        res = client.post(
            "/admin/members/2/nag-windows",
            json={"weekday_start_hour": "evening"},
        )
    assert res.status_code == 400


# ── /dashboard/chores page ──────────────────────────────────────


def test_chores_dashboard_renders_each_bucket() -> None:
    store = SimpleNamespace(
        list_status=AsyncMock(return_value=[
            _stub_status(template_id=1, status="overdue", days_overdue=3),
            _stub_status(template_id=2, name="Mop", status="due_today",
                         days_overdue=0),
            _stub_status(template_id=3, name="Sheets", status="soon",
                         days_overdue=-1),
            _stub_status(template_id=4, name="Plants", status="recent",
                         days_overdue=-5),
        ]),
    )
    with TestClient(_build_app(chore_store=store)) as client:
        res = client.get("/dashboard/chores")
    assert res.status_code == 200
    html = res.text
    assert "Vacuum the house" in html
    assert "Mop" in html
    assert "Sheets" in html
    assert "Plants" in html
    # Overdue badge label appears
    assert "overdue" in html.lower()
    # Mark done button is rendered
    assert 'data-chore-action="complete"' in html


# ── /dashboard/goals page ───────────────────────────────────────


def test_goals_dashboard_shows_empty_state_with_no_goals() -> None:
    store = SimpleNamespace(
        list_all_for_member=AsyncMock(return_value=[]),
        get_progress=AsyncMock(return_value=None),
        list_milestones=AsyncMock(return_value=[]),
    )
    app = _build_app(goals_store=store)
    # No pool → empty member list → no goals
    with TestClient(app) as client:
        res = client.get("/dashboard/goals")
    assert res.status_code == 200
    assert "No active goals yet" in res.text
