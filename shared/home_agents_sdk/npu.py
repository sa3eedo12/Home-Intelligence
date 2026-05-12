from __future__ import annotations

import time
from typing import Any

import httpx


class NPUUnavailable(RuntimeError):
    pass


class NPUClient:
    def __init__(self, base_url: str, timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        # Once we observe the service refusing /v1 endpoints with 4xx (i.e. it's a
        # CPU stub that only handles /health), short-circuit future chat/embed
        # calls. This dramatically lowers the latency of every router request on
        # hosts where the NPU is unavailable (e.g. TrueNAS today).
        self._stub_detected = False
        self._stub_detected_at: float | None = None
        self._stub_recheck_seconds = 300  # re-probe after 5 minutes

    def _is_stub(self) -> bool:
        if not self._stub_detected:
            return False
        if self._stub_detected_at is None:
            return True
        return (time.monotonic() - self._stub_detected_at) < self._stub_recheck_seconds

    def _mark_stub(self) -> None:
        self._stub_detected = True
        self._stub_detected_at = time.monotonic()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        # Cheap short-circuit when we've already detected the service is a stub.
        if self._is_stub() and path.startswith("/v1/"):
            raise NPUUnavailable("NPU is a CPU stub (cached); skipping request")

        # Use a short timeout for /v1 calls because the CPU stub answers them
        # with a 404 instantly; only long-poll embeddings / transcriptions get
        # the full timeout.
        effective_timeout = 3.0 if path.startswith("/v1/chat") else self.timeout

        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                response = await client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            raise NPUUnavailable(f"NPU service timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise NPUUnavailable(f"NPU service unavailable: {exc}") from exc

        if response.status_code >= 500:
            raise NPUUnavailable(f"NPU service 5xx error: {response.status_code}")
        # 4xx on /v1 endpoints means the service doesn't speak that protocol
        # — record it and raise so the caller can fall back. We treat this as
        # NPUUnavailable rather than HTTPStatusError so the router's fallback
        # logic kicks in (Ollama for chat, llm.embed for embeddings).
        if 400 <= response.status_code < 500 and path.startswith("/v1/"):
            self._mark_stub()
            raise NPUUnavailable(
                f"NPU service does not implement {path} ({response.status_code})"
            )
        response.raise_for_status()
        return response.json()

    async def chat(self, model: str, messages: list[dict[str, str]], **opts: Any) -> dict[str, Any]:
        payload = {"model": model, "messages": messages, **opts}
        return await self._request("POST", "/v1/chat/completions", json=payload)

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        payload = {"model": model, "input": texts}
        data = await self._request("POST", "/v1/embeddings", json=payload)
        return [item["embedding"] for item in data.get("data", [])]

    async def transcribe(self, model: str, audio_bytes: bytes, lang: str | None = None) -> str:
        files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
        data = {"model": model}
        if lang:
            data["language"] = lang
        result = await self._request("POST", "/v1/audio/transcriptions", files=files, data=data)
        return str(result.get("text", ""))
