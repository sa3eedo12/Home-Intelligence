"""Tests for the nightly synthesis phase.

Pins the contract: AFTER all other reflection phases (refinement,
generate_proposals, health, correlations, etc.) have run, we make
ONE 35B call that produces a headline + tomorrow's attention item.

Why this exists:
  The 35B reasoner (qwen3.6:35b-a3b) deadlocks under sustained
  back-to-back calls on Vulkan/RADV — GPU sits at 0% busy while the
  request hangs at the network layer. Single-shot calls work fine,
  so we reserve the 35B for ONE big synthesis at the end of the
  nightly window where the quality lift matters most (the headline
  on the morning brief).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.reflector import NightlyReflector


def _make_reflector(*, llm_response: str | None = None, raise_on_chat: Exception | None = None):
    pool = MagicMock()
    redis = MagicMock()
    llm = MagicMock()
    if raise_on_chat is not None:
        llm.chat = AsyncMock(side_effect=raise_on_chat)
    else:
        llm.chat = AsyncMock(return_value={
            "message": {"content": llm_response or "{}"}
        })
    registry = MagicMock()
    reflector = NightlyReflector(
        pool=pool, redis=redis, llm=llm, registry=registry,
        reasoner_model="qwen3.6:35b-a3b", fallback_model="qwen3:8b",
        gap_store=MagicMock(),
    )
    # Skip the Ollama unload network call in tests — pin its async
    # contract here so the synthesis path doesn't try to hit a real
    # Ollama instance.
    reflector._unload_other_ollama_models = AsyncMock(return_value=None)
    return reflector


def _sample_payload():
    """Realistic-shaped phase outputs to feed the synthesizer."""
    return {
        "refined_proposals": [
            {"id": 59, "kind": "code_change", "changed": True,
             "title": "Add ev_status tool for BYD HAN",
             "rationale": "Narrowed from 27 batteries to the 2 HAN sensors.",
             "confidence": 0.95, "notes": "refined by 8B"},
        ],
        "gap_proposals": [
            {"id": 60, "kind": "code_change",
             "title": "Add lock tools for Aqara Smart Lock A100",
             "rationale": "User asked to lock front door.", "confidence": 0.8},
        ],
        "proposals": [
            {"kind": "habit_inference", "title": "Wake-up shifted to 06:30",
             "rationale": "Apple Health shows 7-day median.", "confidence": 0.7},
        ],
        "health_summary": {"sleep_asleep_7d": [{"day": "2026-05-15", "value": 6.2}]},
        "patterns": [{"hour": 7, "agent": "personal_assistant", "count": 12}],
        "correlations": [{"insight": "Steps high on workout days"}],
        "knowledge_gaps": [{"key": "work_hours", "description": "Unknown"}],
    }


@pytest.mark.asyncio
async def test_synthesis_uses_fallback_model_due_to_gpu_pressure():
    """Synthesis runs on the 8B fallback, NOT the 35B reasoner.
    The 35B is unusable on Strix Halo when other models compete for
    the unified memory budget — even single-shot calls hang 30+ min.
    Documented in reflector.py near _synthesize_nightly_brief."""
    llm_response = json.dumps({
        "headline": "Tonight the system learned about your EV and front door.",
        "attention": "Approve proposal #59 (ev_status tool).",
        "patterns": "Multiple gap proposals cluster around device control.",
    })
    reflector = _make_reflector(llm_response=llm_response)

    await reflector._synthesize_nightly_brief(**_sample_payload())

    assert reflector.llm.chat.await_count == 1
    chat_call = reflector.llm.chat.await_args_list[0]
    assert chat_call.kwargs["model"] == "qwen3:8b"
    # Thinking ON for the nightly path: smaller models benefit more
    # from CoT, and the nightly window has hours.
    assert chat_call.kwargs["think"] is True
    # 10-min timeout: thinking adds latency but iGPU still finishes well within.
    assert chat_call.kwargs["timeout"] == 600.0


@pytest.mark.asyncio
async def test_synthesis_returns_structured_output():
    """A successful synthesis returns headline + attention + patterns."""
    llm_response = json.dumps({
        "headline": "9 proposals refined, 1 needs your attention.",
        "attention": "Review the ev_status tool spec — it's ready.",
        "patterns": "Capability gaps cluster around new HA devices.",
    })
    reflector = _make_reflector(llm_response=llm_response)

    out = await reflector._synthesize_nightly_brief(**_sample_payload())

    assert out["headline"].startswith("9 proposals")
    assert "ev_status" in out["attention"]
    assert "Capability gaps" in out["patterns"]
    assert out["model"] == "qwen3:8b"


@pytest.mark.asyncio
async def test_synthesis_skipped_when_nothing_happened():
    """If every phase produced empty outputs, don't burn a 35B call
    on emptiness — return {} so the brief falls back to the
    heuristic headline."""
    reflector = _make_reflector(llm_response="{}")

    out = await reflector._synthesize_nightly_brief(
        refined_proposals=[],
        gap_proposals=[],
        proposals=[],
        health_summary={},
        patterns=[],
        correlations=[],
        knowledge_gaps=[],
    )

    assert out == {}
    # And critically — the LLM was never called.
    reflector.llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_synthesis_swallows_llm_errors():
    """If the 35B hangs (Vulkan deadlock) or times out, the brief
    still renders — we just lose the synthesis headline this run."""
    import httpx
    reflector = _make_reflector(raise_on_chat=httpx.ReadTimeout(""))

    out = await reflector._synthesize_nightly_brief(**_sample_payload())

    assert out == {}


@pytest.mark.asyncio
async def test_synthesis_rejects_empty_response():
    """If the LLM returns valid JSON but no actual content, don't
    promote an empty string to the brief headline."""
    llm_response = json.dumps({"headline": "", "attention": "", "patterns": ""})
    reflector = _make_reflector(llm_response=llm_response)

    out = await reflector._synthesize_nightly_brief(**_sample_payload())

    assert out == {}


@pytest.mark.asyncio
async def test_synthesis_rejects_bad_json():
    """Mangled JSON from the LLM → empty dict, not a crash."""
    reflector = _make_reflector(llm_response="not json at all <think>...")

    out = await reflector._synthesize_nightly_brief(**_sample_payload())

    assert out == {}


@pytest.mark.asyncio
async def test_synthesis_headline_promotes_to_brief_summary():
    """The whole point of running the 35B once: its headline becomes
    the brief summary, replacing the heuristic _headline() output."""
    llm_response = json.dumps({
        "headline": "Tonight's reflection produced 3 refined proposals worth your attention.",
        "attention": "Approve the BYD HAN ev_status tool spec.",
        "patterns": "",
    })
    reflector = _make_reflector(llm_response=llm_response)

    synthesis = await reflector._synthesize_nightly_brief(**_sample_payload())

    body = reflector._build_brief_body(
        evidence={"events": []},
        audit={},
        gaps=[],
        patterns=[],
        health_summary={},
        proposals=[],
        errors=[],
        nightly_synthesis=synthesis,
    )

    # Summary should be the 35B headline, NOT the fallback heuristic.
    assert body["summary"].startswith("Tonight's reflection")
    # And the full synthesis is preserved for the dashboard to render.
    assert body["nightly_synthesis"]["attention"].startswith("Approve")


@pytest.mark.asyncio
async def test_synthesis_absence_falls_back_to_heuristic_headline():
    """When synthesis is empty (skipped or failed), the brief
    summary uses the existing heuristic — backwards compatible."""
    reflector = _make_reflector()

    body = reflector._build_brief_body(
        evidence={"events": []},
        audit={},
        gaps=[],
        patterns=[],
        health_summary={},
        proposals=[],
        errors=[],
        nightly_synthesis={},
    )

    # Falls back to "Reflection found no urgent gaps overnight." etc.
    assert "Reflection" in body["summary"]
    assert body["nightly_synthesis"] == {}


@pytest.mark.asyncio
async def test_synthesis_unloads_other_models_before_chat_call():
    """Before invoking the synthesis chat, free GPU memory by unloading
    any other models still resident. On Strix Halo this leaves a
    cleaner unified-memory state for the synthesis call to land on
    (and would be required if we ever switch back to the 35B for
    synthesis — see _synthesize_nightly_brief for the rationale)."""
    llm_response = json.dumps({
        "headline": "h", "attention": "a", "patterns": "",
    })
    reflector = _make_reflector(llm_response=llm_response)

    await reflector._synthesize_nightly_brief(**_sample_payload())

    # Unload must be called BEFORE the chat. Keep target is the
    # synthesis model itself (currently the 8B fallback).
    reflector._unload_other_ollama_models.assert_awaited_once()
    unload_call = reflector._unload_other_ollama_models.await_args
    assert unload_call.kwargs.get("keep") == "qwen3:8b"


@pytest.mark.asyncio
async def test_synthesis_proceeds_when_unload_raises():
    """If the Ollama unload probe itself fails (network blip, etc.),
    we still attempt the 35B call — better to try with a not-fully-
    clean GPU than skip the synthesis entirely."""
    from unittest.mock import AsyncMock as _A
    llm_response = json.dumps({
        "headline": "h", "attention": "a", "patterns": "",
    })
    reflector = _make_reflector(llm_response=llm_response)
    reflector._unload_other_ollama_models = _A(side_effect=Exception("unload boom"))

    out = await reflector._synthesize_nightly_brief(**_sample_payload())

    # The chat WAS attempted despite the unload raising — unload is
    # best-effort, synthesis is the main work.
    reflector.llm.chat.assert_awaited_once()
    assert out.get("headline") == "h"
    assert out.get("attention") == "a"


@pytest.mark.asyncio
async def test_unload_skips_when_only_keep_model_loaded():
    """If the only loaded model is the synthesis model itself, the
    unload helper makes ZERO unload requests."""
    from unittest.mock import patch

    pool = MagicMock()
    redis = MagicMock()
    llm = MagicMock()
    registry = MagicMock()
    reflector = NightlyReflector(
        pool=pool, redis=redis, llm=llm, registry=registry,
        reasoner_model="qwen3.6:35b-a3b", fallback_model="qwen3:8b",
        gap_store=MagicMock(),
    )

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = lambda: {"models": [{"name": "qwen3.6:35b-a3b"}]}
            return resp
        post = AsyncMock()

    fake_client = _FakeClient()
    with patch("orchestrator.reflector.httpx.AsyncClient", return_value=fake_client):
        await reflector._unload_other_ollama_models(keep="qwen3.6:35b-a3b")

    fake_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_unload_calls_keep_alive_zero_for_each_other_model():
    """For each loaded model that isn't ``keep``, send a generate
    request with keep_alive=0 to force-unload it from VRAM."""
    from unittest.mock import patch

    pool = MagicMock()
    redis = MagicMock()
    llm = MagicMock()
    registry = MagicMock()
    reflector = NightlyReflector(
        pool=pool, redis=redis, llm=llm, registry=registry,
        reasoner_model="qwen3.6:35b-a3b", fallback_model="qwen3:8b",
        gap_store=MagicMock(),
    )

    posts = []

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = lambda: {"models": [
                {"name": "qwen3:8b"},
                {"name": "qwen3:0.6b"},
                {"name": "qwen3.6:35b-a3b"},  # the keep target
                {"name": "bge-m3"},
            ]}
            return resp
        async def post(self, url, json=None, timeout=None):
            posts.append(json)
            return MagicMock()

    with patch("orchestrator.reflector.httpx.AsyncClient", return_value=_FakeClient()):
        await reflector._unload_other_ollama_models(keep="qwen3.6:35b-a3b")

    # 3 unload requests (everything except the keep model)
    assert len(posts) == 3
    unloaded_names = {p["model"] for p in posts}
    assert unloaded_names == {"qwen3:8b", "qwen3:0.6b", "bge-m3"}
    # Every unload must set keep_alive=0 — that's what triggers the
    # immediate VRAM eviction in Ollama.
    for p in posts:
        assert p["keep_alive"] == 0
        assert p["stream"] is False
