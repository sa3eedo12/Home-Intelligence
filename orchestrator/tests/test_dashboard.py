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
