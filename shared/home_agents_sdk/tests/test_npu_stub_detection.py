from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock  # noqa: F401

from home_agents_sdk.npu import NPUClient, NPUUnavailable


class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://fake/v1/chat/completions")
            raise httpx.HTTPStatusError(
                "boom", request=req, response=httpx.Response(self.status_code)
            )


class FakeAsyncClient:
    def __init__(self, *, response: FakeResponse, raises: Exception | None = None):
        self._response = response
        self._raises = raises
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def request(self, *_args, **_kwargs):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._response


@pytest.mark.asyncio
async def test_404_on_chat_marks_client_as_stub(monkeypatch) -> None:
    fake = FakeAsyncClient(response=FakeResponse(404))
    monkeypatch.setattr(
        "home_agents_sdk.npu.httpx.AsyncClient",
        lambda **_kw: fake,
    )

    client = NPUClient("http://lemonade-stub:8000")
    with pytest.raises(NPUUnavailable):
        await client.chat(model="qwen3-1.7b-int4", messages=[{"role": "user", "content": "hi"}])

    # The client should now short-circuit subsequent /v1 calls without hitting the wire.
    initial_calls = fake.calls
    with pytest.raises(NPUUnavailable, match="cached"):
        await client.chat(model="qwen3-1.7b-int4", messages=[{"role": "user", "content": "yo"}])
    assert fake.calls == initial_calls  # no new HTTP request


@pytest.mark.asyncio
async def test_real_500_does_not_mark_stub(monkeypatch) -> None:
    fake = FakeAsyncClient(response=FakeResponse(500))
    monkeypatch.setattr(
        "home_agents_sdk.npu.httpx.AsyncClient",
        lambda **_kw: fake,
    )

    client = NPUClient("http://lemonade:8000")
    with pytest.raises(NPUUnavailable):
        await client.chat(model="x", messages=[])
    # Second call should still hit the wire (not cached as stub).
    with pytest.raises(NPUUnavailable):
        await client.chat(model="x", messages=[])
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_chat_timeout_is_short(monkeypatch) -> None:
    """Validate that /v1/chat calls use a short timeout, not the default 30s."""
    captured: dict = {}

    class _Cap(FakeAsyncClient):
        def __init__(self, **kwargs):
            super().__init__(response=FakeResponse(200, {"choices": []}))
            captured.update(kwargs)

    monkeypatch.setattr("home_agents_sdk.npu.httpx.AsyncClient", _Cap)
    client = NPUClient("http://lemonade:8000")
    await client.chat(model="x", messages=[])
    assert captured.get("timeout") == 3.0
