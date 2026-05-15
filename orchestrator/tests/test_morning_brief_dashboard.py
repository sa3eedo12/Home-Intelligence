from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.dashboard import router


class FakeReflectionStore:
    def __init__(self) -> None:
        self.proposal = {
            "id": 5,
            "kind": "code_change",
            "title": "Add calendar retry tests",
            "rationale": "Retries failed twice yesterday.",
            "evidence_event_ids": [11],
            "confidence": 0.8,
            "status": "pending",
        }

    async def list_briefs(self, limit: int = 1) -> list[dict]:
        return [
            {
                "id": 1,
                "generated_at": "2026-01-02T02:30:00+00:00",
                "summary": "Reflection found one code wishlist item.",
                "body_json": {
                    "yesterday": [{"id": 11, "summary": "Calendar retry failed"}],
                    "questions_for_you": [{"title": "What is your wake time?"}],
                    "suggestions_for_me": [{"title": "Clean stale proposals"}],
                    "code_wishlist": [self.proposal],
                    "proposals": [self.proposal],
                },
                "sent_at": None,
            }
        ]

    async def list_proposals(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict]:
        if status and self.proposal.get("status") != status:
            return []
        return [self.proposal]


def test_morning_brief_page_renders_synthetic_brief() -> None:
    app = FastAPI()
    app.include_router(router)

    async def _status() -> dict:
        return {"reflection": {"last_run_at": None, "age_hours": 30.0, "healthy": False}}

    app.state.status_provider = _status
    app.state.reflection_store = FakeReflectionStore()

    with TestClient(app) as client:
        resp = client.get("/dashboard/morning-brief")

    assert resp.status_code == 200
    assert "Reflection found one code wishlist item" in resp.text
    assert "Yesterday" in resp.text
    assert "Questions for you" in resp.text
    assert "Suggestions for me" in resp.text
    assert "Code wishlist" in resp.text
    assert "Copy prompt" in resp.text
    assert "Open as GitHub issue" in resp.text
    assert "Send to Copilot CLI on NAS" in resp.text
    assert "Coming in Chapter 6" not in resp.text
    assert "health-banner" in resp.text
    assert "/static/_design.css" in resp.text
    assert "/static/_app.js" in resp.text
    assert "/static/morning_brief.css" in resp.text
    assert "/static/morning_brief.js" in resp.text
    assert 'id="run-now-btn"' in resp.text
    assert "toast-stack" in resp.text


# ── Regression: only show pending proposals on the morning brief ─────────


def test_morning_brief_filters_resolved_proposals() -> None:
    """REGRESSION: dashboard audit found the brief listed all 25 historical
    proposals (including 18 dismissed + 6 accepted) as if each were
    waiting for input. The route must request status='pending' from the
    store so resolved rows don't render with Copy/Open Issue/Dispatch
    actions next to them."""
    app = FastAPI()
    app.include_router(router)

    class _Tracking:
        def __init__(self) -> None:
            self.list_proposals_calls: list[dict] = []
            self.proposal_pending = {
                "id": 99,
                "kind": "code_change",
                "title": "Pending one",
                "rationale": "x",
                "evidence_event_ids": [1],
                "confidence": 0.7,
                "status": "pending",
            }
            self.proposal_dismissed = {
                "id": 100,
                "kind": "cleanup_action",
                "title": "Dismissed one",
                "rationale": "x",
                "evidence_event_ids": [1],
                "confidence": 0.5,
                "status": "dismissed",
            }

        async def list_briefs(self, limit: int = 1) -> list[dict]:
            return [
                {
                    "id": 1,
                    "generated_at": "2026-01-02T02:30:00+00:00",
                    "summary": "Brief",
                    "body_json": {"proposals": []},
                    "sent_at": None,
                }
            ]

        async def list_proposals(
            self, status: str | None = None, limit: int = 50
        ) -> list[dict]:
            self.list_proposals_calls.append({"status": status, "limit": limit})
            if status == "pending":
                return [self.proposal_pending]
            return [self.proposal_pending, self.proposal_dismissed]

    store = _Tracking()
    app.state.reflection_store = store

    async def _status() -> dict:
        return {"reflection": {"last_run_at": None, "age_hours": 1.0, "healthy": True}}

    app.state.status_provider = _status

    with TestClient(app) as client:
        resp = client.get("/dashboard/morning-brief")

    assert resp.status_code == 200
    # The route MUST request status='pending'
    assert store.list_proposals_calls
    assert store.list_proposals_calls[0]["status"] == "pending"
    # The dismissed proposal must NOT appear in the rendered HTML
    assert "Dismissed one" not in resp.text
    assert "Pending one" in resp.text
