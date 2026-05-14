"""Smoke test for the /dashboard/proposals page.

Verifies the page renders pending + resolved proposals with the right
filter pills, checkboxes, accept/dismiss controls, and that the JS/CSS
assets are wired in.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.dashboard import router


class FakeReflectionStore:
    def __init__(self, proposals: list[dict]) -> None:
        self._proposals = proposals

    async def list_proposals(self, limit: int = 500) -> list[dict]:
        return list(self._proposals)


def _build_app(proposals: list[dict]) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.reflection_store = FakeReflectionStore(proposals)
    return app


def test_proposals_page_renders_pending_and_resolved_cards() -> None:
    app = _build_app([
        {
            "id": 11,
            "kind": "cleanup_action",
            "title": "Remove stale washer.cycle_completed events",
            "rationale": "12 events older than 90d.",
            "evidence_event_ids": [101, 102],
            "confidence": 0.9,
            "status": "pending",
            "created_at": "2026-05-13T22:00:00+00:00",
        },
        {
            "id": 12,
            "kind": "habit_inference",
            "title": "Wake up around 07:10 on weekdays",
            "rationale": "12 mornings in last 14d.",
            "confidence": 0.82,
            "status": "auto_confirmed",
            "created_at": "2026-05-12T22:00:00+00:00",
        },
    ])

    with TestClient(app) as client:
        resp = client.get("/dashboard/proposals")

    assert resp.status_code == 200
    text = resp.text

    # Both proposals rendered
    assert "Remove stale washer.cycle_completed events" in text
    assert "Wake up around 07:10 on weekdays" in text

    # Pending one shows accept/dismiss buttons
    assert "proposal-accept" in text
    assert "proposal-dismiss" in text
    # Auto-confirmed one shows "Resolved" instead
    assert "Resolved" in text

    # Filter pills with counts
    assert 'data-status="pending"' in text
    assert 'data-status="auto_confirmed"' in text
    assert 'data-status="dismissed"' in text
    # Counts are rendered (1 pending, 1 auto_confirmed)
    assert "Pending" in text
    assert "Auto-confirmed" in text

    # Bulk actions UI is present (hidden by default)
    assert 'id="bulk-actions"' in text
    assert 'id="bulk-accept-btn"' in text
    assert 'id="bulk-dismiss-btn"' in text
    assert 'id="select-all-checkbox"' in text

    # Checkboxes per row
    assert "proposal-checkbox" in text

    # Asset wiring
    assert "/static/_design.css" in text
    assert "/static/morning_brief.css" in text
    assert "/static/proposals.css" in text
    assert "/static/proposals.js" in text
    assert "/static/_app.js" in text


def test_proposals_page_renders_empty_state_with_no_proposals() -> None:
    app = _build_app([])
    with TestClient(app) as client:
        resp = client.get("/dashboard/proposals")
    assert resp.status_code == 200
    assert "No proposals yet" in resp.text


def test_proposals_page_honors_initial_status_query_param() -> None:
    app = _build_app([
        {"id": 1, "kind": "cleanup_action", "title": "x", "status": "dismissed"}
    ])
    with TestClient(app) as client:
        resp = client.get("/dashboard/proposals?status=dismissed")
    assert resp.status_code == 200
    # The pills container records the initial status as a data attribute so
    # the JS can highlight the right pill on first paint.
    assert 'data-initial-status="dismissed"' in resp.text


def test_proposals_page_default_status_filter_is_pending() -> None:
    app = _build_app([])
    with TestClient(app) as client:
        resp = client.get("/dashboard/proposals")
    assert 'data-initial-status="pending"' in resp.text
