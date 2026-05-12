from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import orchestrator.admin as admin_module
from orchestrator.admin import router


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.policy_engine = SimpleNamespace(
        reload=AsyncMock(),
        policies={"quiet_hours": {}},
        set_quiet_override=AsyncMock(),
        clear_quiet_override=AsyncMock(),
    )
    app.state.scheduler = SimpleNamespace(
        reload=AsyncMock(return_value={"jobs": 3}), run_job_now=AsyncMock()
    )
    app.state.reactive = SimpleNamespace(reload=AsyncMock(return_value={"triggers": 2}))
    app.state.redis = SimpleNamespace(
        set=AsyncMock(return_value=True),
        delete=AsyncMock(return_value=1),
        xadd=AsyncMock(return_value="1-0"),
    )
    app.state.registry = SimpleNamespace(dispatch=AsyncMock(return_value={"ok": True}))
    app.state.reflector = SimpleNamespace(
        run_once=AsyncMock(return_value={"brief_id": 1}),
        status={"running": False, "started_at": None, "phase": None},
    )
    app.state.reflection_store = SimpleNamespace(
        list_proposals=AsyncMock(
            return_value=[
                {
                    "id": 7,
                    "kind": "code_change",
                    "title": "Add retry tests",
                    "rationale": "Calendar retries failed.",
                    "evidence_event_ids": [11, 12],
                    "confidence": 0.81,
                    "status": "pending",
                    "cost_estimate": "small",
                    "impact_estimate": "fewer missed appointments",
                }
            ]
        )
    )
    app.state.activity_aggregator = SimpleNamespace(
        snapshot=lambda: {"agents": [], "window_minutes": 5, "total_events": 0},
        recent_events=lambda limit=50: [],
    )
    app.state.knowledge_graph = SimpleNamespace(
        confirm_thing=AsyncMock(return_value={"id": 1, "last_confirmed_at": "now"}),
        confirm_habit=AsyncMock(return_value={"id": 2, "last_observed_at": "now"}),
        confirm_preference=AsyncMock(return_value={"key": "lights", "updated_at": "now"}),
        confirm_routine=AsyncMock(return_value={"id": 3, "last_run_at": "now"}),
        forget_thing=AsyncMock(return_value=True),
        forget_habit=AsyncMock(return_value=True),
        forget_preference=AsyncMock(return_value=True),
        forget_routine=AsyncMock(return_value=True),
        patch_row=AsyncMock(return_value={"id": 1, "friendly_name": "Washer"}),
        evidence_for=AsyncMock(return_value=[{"id": 9, "summary": "Washer completed"}]),
        put_thing=AsyncMock(return_value={"id": 10, "type": "appliance.washer"}),
    )
    return app


def test_reload_policies_returns_counts(monkeypatch) -> None:
    app = _build_app()
    monkeypatch.setattr(
        admin_module, "_load_yaml", lambda _path: {"quiet_hours": {}, "rate_limits": []}
    )

    with TestClient(app) as client:
        resp = client.post("/admin/reload-policies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["schedules"] == 3
    assert body["triggers"] == 2


def test_run_job_invokes_stub_once() -> None:
    app = _build_app()
    app.state.scheduler.run_job_now = AsyncMock(return_value={"ok": True, "result": "done"})

    with TestClient(app) as client:
        resp = client.post("/admin/run-job/morning_brief")
    assert resp.status_code == 200
    assert resp.json()["result"]["result"] == "done"
    app.state.scheduler.run_job_now.assert_awaited_once_with("morning_brief")


def test_quiet_on_sets_override_with_ttl() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/quiet/on")
    assert resp.status_code == 200
    body = resp.json()
    assert body["quiet"] == "on"
    assert body["ttl_seconds"] == 8 * 3600
    app.state.policy_engine.set_quiet_override.assert_awaited_once_with("on", 8 * 3600)


def test_quiet_clear_calls_clear_override() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/quiet/clear")
    assert resp.status_code == 200
    assert resp.json()["quiet"] is None
    app.state.policy_engine.clear_quiet_override.assert_awaited_once()


def test_quiet_invalid_state_returns_400() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/quiet/maybe")
    assert resp.status_code == 400


def test_mute_sets_redis_key_with_ttl() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/mute", json={"key": "home_automation", "minutes": 15})
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "home_automation"
    assert body["minutes"] == 15
    app.state.redis.set.assert_awaited_once_with("policy:mute:home_automation", "1", ex=15 * 60)


def test_mute_rejects_invalid_minutes() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/mute", json={"key": "x", "minutes": 0})
    assert resp.status_code == 400


def test_mute_rejects_missing_key() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/mute", json={"minutes": 15})
    assert resp.status_code == 400


def test_unmute_deletes_redis_key() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/unmute", json={"key": "home_automation"})
    assert resp.status_code == 200
    app.state.redis.delete.assert_awaited_once_with("policy:mute:home_automation")


def test_invoke_capability_proxies_to_registry() -> None:
    app = _build_app()
    app.state.registry.dispatch = AsyncMock(return_value={"ok": True, "result": "hello"})

    with TestClient(app) as client:
        resp = client.post(
            "/admin/invoke",
            json={"agent": "home_automation", "capability": "list_entities", "payload": {}},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "home_automation"
    assert body["result"] == {"ok": True, "result": "hello"}
    app.state.registry.dispatch.assert_awaited_once_with("home_automation", "list_entities", {})


def test_invoke_capability_requires_agent_and_capability() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/invoke", json={"agent": "", "capability": "x"})
    assert resp.status_code == 400


def test_discovery_adopt_puts_thing() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/admin/discovery/adopt",
            json={
                "entity_id": "sensor.washer",
                "type": "appliance.washer",
                "friendly_name": "Washer",
                "photo_path": "/data/photos/things/sensor.washer.jpg",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["thing"]["id"] == 10
    app.state.knowledge_graph.put_thing.assert_awaited_once_with(
        type="appliance.washer",
        friendly_name="Washer",
        attributes={},
        ha_entity_ids=["sensor.washer"],
        photo_path="/data/photos/things/sensor.washer.jpg",
        confidence=1.0,
        source="discovery_user",
    )


def test_discovery_ignore_puts_ignored_thing() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/discovery/ignore", json={"entity_id": "sensor.noisy"})
    assert resp.status_code == 200
    app.state.knowledge_graph.put_thing.assert_awaited_once_with(
        type="ignored.entity",
        friendly_name="sensor.noisy",
        attributes={},
        ha_entity_ids=["sensor.noisy"],
        confidence=1.0,
        source="discovery_user",
    )


def test_knowledge_confirm_calls_matching_method() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/knowledge/confirm", json={"table": "things", "id": 1})
    assert resp.status_code == 200
    assert resp.json()["item"]["last_confirmed_at"] == "now"
    app.state.knowledge_graph.confirm_thing.assert_awaited_once_with(1)


def test_knowledge_evidence_returns_event_rows() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/admin/knowledge/evidence?table=things&id=1")
    assert resp.status_code == 200
    assert resp.json()["items"][0]["summary"] == "Washer completed"
    app.state.knowledge_graph.evidence_for.assert_awaited_once_with("things", 1)


def test_knowledge_forget_calls_matching_method() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/knowledge/forget", json={"table": "preferences", "id": "lights"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    app.state.knowledge_graph.forget_preference.assert_awaited_once_with("lights")


def test_knowledge_patch_whitelists_fields() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.patch(
            "/admin/knowledge/things/1",
            json={"friendly_name": "Washer", "ignored": "nope"},
        )
    assert resp.status_code == 200
    app.state.knowledge_graph.patch_row.assert_awaited_once_with(
        "things",
        1,
        {"friendly_name": "Washer"},
    )


def test_knowledge_patch_rejects_unknown_table() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.patch("/admin/knowledge/unknown/1", json={"name": "x"})
    assert resp.status_code == 400


def test_replay_publishes_to_stream() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/admin/replay",
            json={"stream": "events.home", "payload": {"type": "doorbell_ring"}},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["stream"] == "events.home"
    assert body["id"] == "1-0"


def test_activity_snapshot_returns_aggregator_data() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/admin/activity/snapshot")
    assert resp.status_code == 200
    body = resp.json()
    assert "agents" in body
    assert "recent" in body


def test_run_reflection_invokes_reflector() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/reflection/run")
    assert resp.status_code == 200
    body = resp.json()
    # New behavior: returns immediately with started=True (background task).
    assert body["started"] is True
    # Give the background task a moment to invoke the mocked run_once.
    import time
    for _ in range(20):
        if app.state.reflector.run_once.await_count >= 1:
            break
        time.sleep(0.05)
    app.state.reflector.run_once.assert_awaited_once()


def test_format_proposal_returns_markdown_blob() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/proposals/7/format")
    assert resp.status_code == 200
    body = resp.json()
    assert body["proposal_id"] == 7
    assert "# Add retry tests" in body["markdown"]
    assert "11, 12" in body["markdown"]


def test_format_proposal_returns_404_for_unknown_id() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.post("/admin/proposals/999/format")
    assert resp.status_code == 404


def test_reflection_run_kicks_off_background_and_returns_immediately() -> None:
    """POST /admin/reflection/run must NOT block on the LLM. It schedules a
    background task and returns the current status immediately."""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.admin import router as admin_router

    app = FastAPI()
    app.include_router(admin_router)

    long_event = asyncio.Event()
    started = asyncio.Event()

    async def _slow_run():
        started.set()
        await long_event.wait()  # would block forever if awaited inline

    app.state.reflector = SimpleNamespace(
        run_once=_slow_run,
        status={"running": False, "started_at": None, "phase": None},
        _status={},
    )

    with TestClient(app) as client:
        resp = client.post("/admin/reflection/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["started"] is True


def test_reflection_run_reports_already_running_without_starting_again() -> None:
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.admin import router as admin_router

    call_count = {"n": 0}

    async def _run():
        call_count["n"] += 1

    app = FastAPI()
    app.include_router(admin_router)
    app.state.reflector = SimpleNamespace(
        run_once=_run,
        status={"running": True, "started_at": "2026-01-01T00:00:00+00:00"},
        _status={},
    )
    with TestClient(app) as client:
        resp = client.post("/admin/reflection/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is False
    assert body["status"]["running"] is True
    assert call_count["n"] == 0


def test_reflection_status_returns_configured_false_when_missing() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.admin import router as admin_router

    app = FastAPI()
    app.include_router(admin_router)
    with TestClient(app) as client:
        resp = client.get("/admin/reflection/status")
    assert resp.status_code == 200
    assert resp.json()["configured"] is False


def test_reflection_status_proxies_reflector_status() -> None:
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.admin import router as admin_router

    app = FastAPI()
    app.include_router(admin_router)
    app.state.reflector = SimpleNamespace(
        status={
            "running": True,
            "started_at": "2026-01-01T00:00:00+00:00",
            "phase": "generate_proposals",
            "elapsed_seconds": 17.3,
            "last_finished_at": None,
            "last_brief_id": None,
            "last_error": None,
            "last_duration_seconds": None,
        }
    )
    with TestClient(app) as client:
        resp = client.get("/admin/reflection/status")
    body = resp.json()
    assert body["configured"] is True
    assert body["running"] is True
    assert body["phase"] == "generate_proposals"
    assert body["elapsed_seconds"] == 17.3
