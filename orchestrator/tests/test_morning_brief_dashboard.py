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

    async def list_proposals(self, limit: int = 50) -> list[dict]:
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
    assert "/static/morning_brief.css" in resp.text
    assert "/static/morning_brief.js" in resp.text
