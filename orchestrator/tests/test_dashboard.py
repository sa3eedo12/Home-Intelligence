from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.dashboard import router


def test_dashboard_renders_with_empty_state() -> None:
    app = FastAPI()
    app.include_router(router)

    async def _status() -> dict:
        return {
            "stack": {
                "orchestrator": {"ok": True},
                "ollama": {"ok": False},
                "lemonade": {"ok": False},
                "redis": {"ok": False},
                "postgres": {"ok": False},
                "qdrant": {"ok": False},
            },
            "agents": [],
            "jobs": [],
            "recent_notifications": [],
            "active_mutes": [],
            "suppression_counts": {},
            "quiet_override": None,
            "models": {"npu": [], "igpu": []},
            "activity": {"window_minutes": 5, "agents": [], "total_events": 0},
            "recent_activity": [],
            "narrative": None,
            "alert_narrative": None,
        }

    app.state.status_provider = _status

    with TestClient(app) as client:
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Home Intelligence" in resp.text
    # New SPA shell elements must be present.
    assert "agent-grid" in resp.text
    assert "activity-feed" in resp.text
    assert "ha-bridge-card" in resp.text
    assert "observations-list" in resp.text
    assert "activity-refresh-btn" in resp.text
    assert "active-policies" in resp.text
    assert "nav-rail" in resp.text
    assert "toast-stack" in resp.text
    assert "data-theme-toggle" in resp.text
    assert "/static/_design.css" in resp.text
    assert "/static/_app.js" in resp.text
    assert "/static/dashboard.js" in resp.text
    assert "/dashboard/stream" not in resp.text  # the JS opens it, not the HTML directly


def test_dashboard_renders_with_curator_narrative_and_agents() -> None:
    app = FastAPI()
    app.include_router(router)

    async def _status() -> dict:
        return {
            "stack": {"orchestrator": {"ok": True}},
            "agents": ["home_automation", "system_health"],
            "jobs": [
                {
                    "id": "morning_brief",
                    "next_run_time": "2026-05-13T07:30:00",
                    "last_run_time": None,
                    "last_status": "never",
                }
            ],
            "recent_notifications": [
                {
                    "ts": "2026-05-12T12:00:00+00:00",
                    "topic": "system.cpu",
                    "severity": "warn",
                    "decision": "send",
                    "text": "CPU spike",
                }
            ],
            "active_mutes": [{"key": "home_automation", "ttl_seconds": 600}],
            "suppression_counts": {"sent": 8, "suppressed": 3, "suppressed.mute": 2},
            "quiet_override": "off",
            "models": {"npu": ["bge-m3-int8"], "igpu": ["qwen3:8b"]},
            "activity": {
                "window_minutes": 5,
                "total_events": 3,
                "agents": [
                    {
                        "agent": "home_automation",
                        "state": "working",
                        "current": {"capability": "list_entities"},
                        "ok": 2,
                        "errors": 0,
                        "avg_ms": 12.5,
                        "sparkline": [0, 1, 0, 1, 0],
                        "last_event": None,
                    }
                ],
            },
            "recent_activity": [
                {
                    "ts": "2026-05-12T12:00:00+00:00",
                    "agent": "home_automation",
                    "capability": "list_entities",
                    "status": "ok",
                    "duration_ms": 13.2,
                }
            ],
            "narrative": {
                "narrative": "All systems nominal in the last 15 minutes.",
                "generated_at": "2026-05-12T12:01:00+00:00",
            },
            "alert_narrative": None,
        }

    app.state.status_provider = _status

    with TestClient(app) as client:
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "home_automation" in resp.text
    assert "All systems nominal" in resp.text
    assert "morning_brief" in resp.text
    assert "Quiet on" in resp.text  # button exists
    assert "Suppression counts" in resp.text
    assert "Why was this blocked?" in resp.text
    assert 'id="connection-state"' in resp.text
    assert 'data-job="morning_brief"' in resp.text  # run-now button is wired


# ── Pending-proposals nav badge ─────────────────────────────────────────


def _minimal_status() -> dict:
    return {
        "stack": {
            "orchestrator": {"ok": True},
            "ollama": {"ok": False},
            "lemonade": {"ok": False},
            "redis": {"ok": False},
            "postgres": {"ok": False},
            "qdrant": {"ok": False},
        },
        "agents": [],
        "jobs": [],
        "recent_notifications": [],
        "active_mutes": [],
        "suppression_counts": {},
        "quiet_override": None,
        "models": {"npu": [], "igpu": []},
        "activity": {"window_minutes": 5, "agents": [], "total_events": 0},
        "recent_activity": [],
        "narrative": None,
        "alert_narrative": None,
    }


def test_dashboard_renders_proposals_nav_badge_with_pending_count() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = FastAPI()
    app.include_router(router)

    async def _status() -> dict:
        return _minimal_status()

    app.state.status_provider = _status
    app.state.reflection_store = SimpleNamespace(
        count_proposals=AsyncMock(return_value=12),
    )

    with TestClient(app) as client:
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    # Badge rendered with the count and aria-label
    assert 'class="nav-badge"' in resp.text
    assert ">12<" in resp.text  # the digit appears inside the badge span
    assert 'aria-label="12 pending"' in resp.text
    # Helper was called with status='pending' (NOT all proposals)
    assert app.state.reflection_store.count_proposals.await_args.kwargs == {
        "status": "pending"
    }


def test_dashboard_omits_proposals_badge_when_zero_pending() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = FastAPI()
    app.include_router(router)

    async def _status() -> dict:
        return _minimal_status()

    app.state.status_provider = _status
    app.state.reflection_store = SimpleNamespace(
        count_proposals=AsyncMock(return_value=0),
    )

    with TestClient(app) as client:
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    # No badge when nothing is pending — keeps the nav uncluttered
    assert "nav-badge" not in resp.text


def test_dashboard_survives_count_proposals_error() -> None:
    """If the count helper blows up, the dashboard must still render."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = FastAPI()
    app.include_router(router)

    async def _status() -> dict:
        return _minimal_status()

    app.state.status_provider = _status
    app.state.reflection_store = SimpleNamespace(
        count_proposals=AsyncMock(side_effect=Exception("boom")),
    )

    with TestClient(app) as client:
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "nav-badge" not in resp.text


def test_dashboard_renders_routines_nav_badge_with_suggested_count() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = FastAPI()
    app.include_router(router)

    async def _status() -> dict:
        return _minimal_status()

    app.state.status_provider = _status
    app.state.routine_lifecycle_store = SimpleNamespace(
        stats=AsyncMock(return_value={"suggested": 4, "active": 1, "dismissed": 0}),
    )

    with TestClient(app) as client:
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert 'href="/dashboard/routines"' in resp.text
    assert 'aria-label="4 suggested"' in resp.text
    assert ">4<" in resp.text


def test_dashboard_routines_page_renders_three_buckets() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = FastAPI()
    app.include_router(router)

    def _row(rid, status, name="x -> y"):
        return {
            "id": rid,
            "name": name,
            "status": status,
            "source": "routine_sequence_miner",
            "confirmed_count": 2 if status == "suggested" else 3,
            "steps": '{"steps":[{"trigger":"x.a"},{"action":"y.b"}],'
                     '"attributes":{"confidence":0.85,"pair_count":7,'
                     '"window_minutes":30}}',
            "schedule": None,
            "created_at": datetime(2026, 5, 21, tzinfo=UTC),
            "updated_at": datetime(2026, 5, 21, tzinfo=UTC),
            "promoted_at": datetime(2026, 5, 21, tzinfo=UTC) if status == "active" else None,
            "dismissed_at": datetime(2026, 5, 21, tzinfo=UTC) if status == "dismissed" else None,
        }

    app.state.routine_lifecycle_store = SimpleNamespace(
        list_suggested=AsyncMock(return_value=[_row(1, "suggested", "A -> B")]),
        list_active=AsyncMock(return_value=[_row(2, "active", "C -> D")]),
        list_dismissed=AsyncMock(return_value=[_row(3, "dismissed", "E -> F")]),
        stats=AsyncMock(return_value={"suggested": 1, "active": 1, "dismissed": 1}),
    )

    with TestClient(app) as client:
        resp = client.get("/dashboard/routines")
    assert resp.status_code == 200
    text = resp.text
    # All three routine names render
    assert "A -&gt; B" in text or "A -> B" in text
    assert "C -&gt; D" in text or "C -> D" in text
    assert "E -&gt; F" in text or "E -> F" in text
    # Action buttons present for each bucket
    assert "routine-confirm" in text
    assert "routine-dismiss" in text
    assert "routine-override" in text
    # Confidence rendered (0.85 -> 85%)
    assert "85%" in text


def test_dashboard_routines_page_handles_empty() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = FastAPI()
    app.include_router(router)
    app.state.routine_lifecycle_store = SimpleNamespace(
        list_suggested=AsyncMock(return_value=[]),
        list_active=AsyncMock(return_value=[]),
        list_dismissed=AsyncMock(return_value=[]),
        stats=AsyncMock(return_value={"suggested": 0, "active": 0, "dismissed": 0}),
    )
    with TestClient(app) as client:
        resp = client.get("/dashboard/routines")
    assert resp.status_code == 200
    assert "No suggested routines yet" in resp.text


def test_dashboard_survives_routine_stats_error() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = FastAPI()
    app.include_router(router)

    async def _status() -> dict:
        return _minimal_status()

    app.state.status_provider = _status
    app.state.routine_lifecycle_store = SimpleNamespace(
        stats=AsyncMock(side_effect=Exception("boom")),
    )

    with TestClient(app) as client:
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    # No badge when stats fail
    assert 'aria-label="0 suggested"' not in resp.text
