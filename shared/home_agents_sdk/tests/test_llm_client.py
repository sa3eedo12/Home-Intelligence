from __future__ import annotations

from typing import Any

import pytest

from home_agents_sdk.llm import OllamaClient


class _FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeAsyncClient:
    """Captures the post payload + timeout so tests can assert on them."""

    last_payload: dict[str, Any] | None = None
    last_timeout: Any = None

    def __init__(self, *, timeout: Any = None, **_: Any) -> None:
        _FakeAsyncClient.last_timeout = timeout

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False

    async def post(self, _url: str, json: dict[str, Any]) -> _FakeResponse:
        _FakeAsyncClient.last_payload = json
        return _FakeResponse({"message": {"content": "ok"}})


@pytest.mark.asyncio
async def test_chat_keep_alive_passthrough(monkeypatch) -> None:
    """keep_alive=-1 must reach Ollama so the model gets pinned in memory.
    Without this the orchestrator boot warmer cannot keep the 35B
    reasoner resident."""
    _FakeAsyncClient.last_payload = None
    monkeypatch.setattr("home_agents_sdk.llm.httpx.AsyncClient", _FakeAsyncClient)

    client = OllamaClient("http://ollama:11434")
    await client.chat(
        messages=[{"role": "user", "content": "warm"}],
        model="qwen3.6:35b-a3b",
        keep_alive=-1,
        think=False,
    )

    payload = _FakeAsyncClient.last_payload
    assert payload is not None
    assert payload["keep_alive"] == -1
    assert payload["think"] is False
    assert payload["model"] == "qwen3.6:35b-a3b"


@pytest.mark.asyncio
async def test_chat_keep_alive_omitted_by_default(monkeypatch) -> None:
    """keep_alive must NOT appear in the payload when caller didn't
    set it — otherwise we'd accidentally pin every model the
    orchestrator calls. Ollama's per-call default (KEEP_ALIVE env or
    5m) should take over."""
    _FakeAsyncClient.last_payload = None
    monkeypatch.setattr("home_agents_sdk.llm.httpx.AsyncClient", _FakeAsyncClient)

    client = OllamaClient("http://ollama:11434")
    await client.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="qwen3:8b",
    )

    payload = _FakeAsyncClient.last_payload
    assert payload is not None
    assert "keep_alive" not in payload


@pytest.mark.asyncio
async def test_chat_timeout_override(monkeypatch) -> None:
    """timeout=600 is required for the reasoner warmer because the 35B
    cold-load can exceed the default 180s OLLAMA_TIMEOUT."""
    _FakeAsyncClient.last_timeout = None
    monkeypatch.setattr("home_agents_sdk.llm.httpx.AsyncClient", _FakeAsyncClient)

    client = OllamaClient("http://ollama:11434")
    await client.chat(
        messages=[{"role": "user", "content": "warm"}],
        model="qwen3.6:35b-a3b",
        timeout=600.0,
    )

    assert _FakeAsyncClient.last_timeout == 600.0


@pytest.mark.asyncio
async def test_generate_keep_alive_passthrough(monkeypatch) -> None:
    _FakeAsyncClient.last_payload = None
    monkeypatch.setattr("home_agents_sdk.llm.httpx.AsyncClient", _FakeAsyncClient)

    client = OllamaClient("http://ollama:11434")
    await client.generate(
        prompt="warm",
        model="qwen3.6:35b-a3b",
        keep_alive=-1,
    )

    payload = _FakeAsyncClient.last_payload
    assert payload is not None
    assert payload["keep_alive"] == -1


@pytest.mark.asyncio
async def test_embed_passes_small_num_ctx_for_vulkan(monkeypatch) -> None:
    """bge-m3's default n_ctx is 8192, which requests a ~4.4 GiB compute
    buffer that exceeds Vulkan's 4 GiB per-allocation limit and panics
    with 'failed to allocate compute pp buffers'. The embed call must
    cap num_ctx at 512 so it survives on the Vulkan backend."""
    _FakeAsyncClient.last_payload = None

    class _EmbedResp(_FakeResponse):
        def __init__(self) -> None:
            super().__init__({"embedding": [0.1, 0.2, 0.3]})

    class _EmbedClient(_FakeAsyncClient):
        async def post(self, _url: str, json: dict) -> _FakeResponse:
            _FakeAsyncClient.last_payload = json
            return _EmbedResp()

    monkeypatch.setattr("home_agents_sdk.llm.httpx.AsyncClient", _EmbedClient)

    client = OllamaClient("http://ollama:11434")
    out = await client.embed("hello world", model="bge-m3")

    payload = _FakeAsyncClient.last_payload
    assert payload is not None
    assert payload["model"] == "bge-m3"
    assert payload["prompt"] == "hello world"
    assert payload.get("options", {}).get("num_ctx") == 512
    assert out == [0.1, 0.2, 0.3]


class _FakePsClient:
    """Serves a canned /api/ps body so loaded_models can be asserted on."""

    body: dict[str, Any] = {"models": []}
    raise_on_get: Exception | None = None
    last_url: str | None = None

    def __init__(self, *, timeout: Any = None, **_: Any) -> None:
        _FakePsClient.last_timeout = timeout

    async def __aenter__(self) -> "_FakePsClient":
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False

    async def get(self, url: str) -> _FakeResponse:
        _FakePsClient.last_url = url
        if _FakePsClient.raise_on_get is not None:
            raise _FakePsClient.raise_on_get
        return _FakeResponse(_FakePsClient.body)


@pytest.mark.asyncio
async def test_loaded_models_returns_tagged_and_bare_names(monkeypatch) -> None:
    """Ollama reports `name:tag` but callers hold bare tag names from env,
    so both forms must be present or the warmer re-warms models that are
    already resident on every cycle."""
    _FakePsClient.raise_on_get = None
    _FakePsClient.body = {
        "models": [
            {"name": "qwen36-moe-64k:latest"},
            {"name": "bge-m3:latest"},
        ]
    }
    monkeypatch.setattr("home_agents_sdk.llm.httpx.AsyncClient", _FakePsClient)

    names = await OllamaClient("http://ollama:11434").loaded_models()

    assert "qwen36-moe-64k" in names
    assert "qwen36-moe-64k:latest" in names
    assert "bge-m3" in names
    assert _FakePsClient.last_url == "http://ollama:11434/api/ps"


@pytest.mark.asyncio
async def test_loaded_models_empty_when_nothing_resident(monkeypatch) -> None:
    """An empty model list is a valid answer, not an error."""
    _FakePsClient.raise_on_get = None
    _FakePsClient.body = {"models": []}
    monkeypatch.setattr("home_agents_sdk.llm.httpx.AsyncClient", _FakePsClient)

    assert await OllamaClient("http://ollama:11434").loaded_models() == set()


@pytest.mark.asyncio
async def test_loaded_models_raises_when_ollama_unreachable(monkeypatch) -> None:
    """Transport failures must propagate. Returning an empty set instead
    would look identical to "nothing is loaded" and send the warmer off to
    warm every model against a dead endpoint."""
    _FakePsClient.body = {"models": []}
    _FakePsClient.raise_on_get = RuntimeError("connection refused")
    monkeypatch.setattr("home_agents_sdk.llm.httpx.AsyncClient", _FakePsClient)

    with pytest.raises(RuntimeError):
        await OllamaClient("http://ollama:11434").loaded_models()

    _FakePsClient.raise_on_get = None
