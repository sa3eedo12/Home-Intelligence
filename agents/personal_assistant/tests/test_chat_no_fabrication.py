"""The hallucination-prevention regression suite.

Pins the contract that chat.chat() MUST refuse action-verb requests
with an honest reply rather than letting the LLM fabricate a fake
execution narrative (the exact bug that produced 'There was an error
with the thermostat (climate.thermostat_2)' for a request the system
never actually attempted).
"""

from __future__ import annotations

import pytest

from tools.chat import _HONEST_REFUSAL, _is_action_verb, chat


@pytest.mark.asyncio
async def test_chat_refuses_action_verb_temperature() -> None:
    """The exact phrasing that triggered the original hallucination."""
    out = await chat(text="Reduce the temperature in the bedroom")
    assert out["reply"] == _HONEST_REFUSAL
    assert out["refused_action_verb"] is True
    assert out["already_natural"] is True


@pytest.mark.asyncio
async def test_chat_refuses_action_verb_lights() -> None:
    out = await chat(text="Turn off all the lights please")
    assert out["reply"] == _HONEST_REFUSAL
    assert out["refused_action_verb"] is True


@pytest.mark.asyncio
async def test_chat_refuses_action_verb_with_polite_framing() -> None:
    """Even polite phrasing must trigger the refusal."""
    out = await chat(text="Could you please open the bedroom blinds?")
    assert out["refused_action_verb"] is True


@pytest.mark.asyncio
async def test_chat_proceeds_normally_for_greeting(monkeypatch) -> None:
    """Greetings reach the LLM normally."""
    from unittest.mock import AsyncMock

    fake_client = type(
        "FakeClient",
        (),
        {
            "chat": AsyncMock(
                return_value={"message": {"content": "Hello! How can I help?"}}
            )
        },
    )()
    monkeypatch.setattr("tools.chat._llm", lambda: fake_client)

    out = await chat(text="hi there")

    assert out["reply"] == "Hello! How can I help?"
    assert out["already_natural"] is True
    assert "refused_action_verb" not in out


@pytest.mark.asyncio
async def test_chat_proceeds_normally_for_question(monkeypatch) -> None:
    """Information-seeking questions still get answered."""
    from unittest.mock import AsyncMock

    fake_client = type(
        "FakeClient",
        (),
        {
            "chat": AsyncMock(
                return_value={"message": {"content": "Paris is the capital of France."}}
            )
        },
    )()
    monkeypatch.setattr("tools.chat._llm", lambda: fake_client)

    out = await chat(text="What is the capital of France?")

    assert "Paris" in out["reply"]
    assert "refused_action_verb" not in out


def test_action_verb_detector_positive_cases() -> None:
    for text in [
        "Reduce the temperature in the bedroom",
        "Turn off the lights",
        "Open the blinds",
        "Play music in the kitchen",
        "Set the AC to 22",
        "Lower the heat",
        "Could you turn on the fan?",
        "Please dim the lights",
    ]:
        assert _is_action_verb(text), f"missed: {text!r}"


def test_action_verb_detector_negative_cases() -> None:
    for text in [
        "hi",
        "thanks",
        "good morning",
        "What's the weather like?",
        "Tell me a joke",
        "How are you?",
        "What can you do?",
    ]:
        assert not _is_action_verb(text), f"false positive: {text!r}"


def test_honest_refusal_does_not_fabricate() -> None:
    """The refusal text must explicitly own the limitation and NOT
    invent a fake error narrative."""
    text = _HONEST_REFUSAL.lower()
    # Things the refusal must say (owns the limitation)
    assert "won't pretend" in text
    assert "logged" in text
    # Things the refusal must NOT say — these are the fabrication
    # patterns we observed in the original bug. Check phrasing the LLM
    # would use to invent an execution narrative.
    fabrication_patterns = [
        "the device returned",
        "there was an error with",
        "i was unable to set",
        "i attempted to",
        "the system reported",
        "the thermostat couldn",
        "the lights couldn",
    ]
    for pattern in fabrication_patterns:
        assert pattern not in text, f"refusal text contains fabrication pattern: {pattern!r}"
