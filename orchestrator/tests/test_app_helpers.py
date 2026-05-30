"""Tests for the small pure helpers in orchestrator/app.py."""
from __future__ import annotations

from orchestrator.app import _is_advisor_brief


def test_is_advisor_brief_detects_type_field() -> None:
    """Briefs the advisor writes via _record_advisor_brief carry
    body_json.type='advisor' and must be filtered before pick-most-recent."""
    brief = {
        "id": 99, "summary": "Advisor skipped: quiet_hours_active",
        "body_json": {"type": "advisor", "status": "skipped",
                       "reason": "quiet_hours_active"},
    }
    assert _is_advisor_brief(brief) is True


def test_is_advisor_brief_detects_summary_prefix() -> None:
    """Belt-and-braces: catches briefs even if the body_json didn't
    have a type but the summary clearly identifies them."""
    brief = {"summary": "Advisor skipped: calendar_busy",
             "body_json": {"status": "skipped"}}
    assert _is_advisor_brief(brief) is True


def test_is_advisor_brief_accepts_real_morning_brief() -> None:
    """Real reflection briefs don't have a type field — must NOT be
    filtered out."""
    brief = {
        "id": 100, "summary": "Yesterday's events…",
        "body_json": {"summary": "...", "evidence": {"events": []},
                       "questions_for_you": []},
    }
    assert _is_advisor_brief(brief) is False


def test_is_advisor_brief_handles_missing_body() -> None:
    """Defensive: missing body_json shouldn't crash, just say no."""
    assert _is_advisor_brief({"summary": "Whatever"}) is False
    assert _is_advisor_brief({}) is False


def test_is_advisor_brief_handles_non_dict_body() -> None:
    """If body_json comes back as a string (e.g. unparsed JSONB),
    don't blow up on the .get call."""
    assert _is_advisor_brief({"body_json": "{}"}) is False
