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


# ─── Device-query refusal regression tests ────────────────────────────
# Live bug: "What's the battery percentage of my car?" produced
# "I can't check your car's battery percentage right now. The system
# isn't providing data. Make sure your car is powered on and the
# battery is connected." — pure fabrication ("powered on", "battery
# connected") about a device the system has zero knowledge of.

from tools.chat import _HONEST_QUERY_REFUSAL, _is_device_query


@pytest.mark.asyncio
async def test_chat_refuses_car_battery_query() -> None:
    """The exact live-fired prompt that triggered the second hallucination."""
    out = await chat(text="What's the battery percentage of my car?")
    assert out["reply"] == _HONEST_QUERY_REFUSAL
    assert out["refused_device_query"] is True
    assert "won't make up" in out["reply"]


@pytest.mark.asyncio
async def test_chat_refuses_bedroom_door_status() -> None:
    out = await chat(text="Is the bedroom door locked?")
    assert out["refused_device_query"] is True


@pytest.mark.asyncio
async def test_chat_refuses_temperature_query() -> None:
    out = await chat(text="how warm is the office?")
    assert out["refused_device_query"] is True


@pytest.mark.asyncio
async def test_chat_refuses_washer_status() -> None:
    out = await chat(text="is the washer running?")
    assert out["refused_device_query"] is True


@pytest.mark.asyncio
async def test_chat_proceeds_for_general_question(monkeypatch) -> None:
    """Questions with NO device noun pass through to the LLM."""
    from unittest.mock import AsyncMock

    fake_client = type("FakeClient", (), {
        "chat": AsyncMock(
            return_value={"message": {"content": "It's about 300 km."}}
        )
    })()
    monkeypatch.setattr("tools.chat._llm", lambda: fake_client)

    out = await chat(text="How far is Dubai from Abu Dhabi?")
    assert out["reply"] == "It's about 300 km."
    assert "refused_device_query" not in out
    assert "refused_action_verb" not in out


@pytest.mark.asyncio
async def test_chat_proceeds_for_how_are_you(monkeypatch) -> None:
    """The classic 'how are you' is a question word with NO device
    noun — passes through to social chat unmolested."""
    from unittest.mock import AsyncMock

    fake_client = type("FakeClient", (), {
        "chat": AsyncMock(
            return_value={"message": {"content": "I'm doing well, thanks!"}}
        )
    })()
    monkeypatch.setattr("tools.chat._llm", lambda: fake_client)

    out = await chat(text="how are you doing today?")
    assert "refused_device_query" not in out
    assert "refused_action_verb" not in out


def test_device_query_detector_positives() -> None:
    """All the phrasings the LLM happily fabricates for."""
    for text in [
        "What's the battery percentage of my car?",
        "is the bedroom door locked?",
        "how warm is the office?",
        "what's the temperature in the kitchen?",
        "is the washer running?",
        "what's the status of the dishwasher?",
        "are the lights on?",
        "is the front door locked?",
        "what's the humidity in the bedroom?",
        "how is my car charging?",
    ]:
        assert _is_device_query(text), f"missed: {text!r}"


def test_device_query_detector_negatives() -> None:
    """Things that look question-shaped but aren't device queries."""
    for text in [
        "hi",
        "thanks",
        "how are you?",
        "what's your name?",  # question word but no device noun
        "tell me a joke",
        "what is the capital of France?",
        "how far is Mars from Earth?",
        "good morning",
    ]:
        assert not _is_device_query(text), f"false positive: {text!r}"


def test_honest_query_refusal_does_not_fabricate() -> None:
    """The query refusal must be honest — no invented device states
    or made-up troubleshooting advice."""
    text = _HONEST_QUERY_REFUSAL.lower()
    assert "won't make up" in text
    assert "logged" in text
    # Things that would be fabricated if the LLM was generating
    for invented in [
        "make sure your car",
        "powered on",
        "battery is connected",
        "try again later",
        "the device returned",
        "check your network",
    ]:
        assert invented not in text, f"query refusal contains fabricated phrase: {invented!r}"
