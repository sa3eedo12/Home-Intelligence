from __future__ import annotations

from typing import Any

import httpx


class NPUUnavailable(RuntimeError):
    pass


class NPUClient:
    def __init__(self, base_url: str, timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            raise NPUUnavailable(f"NPU service timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise NPUUnavailable(f"NPU service unavailable: {exc}") from exc

        if response.status_code >= 500:
            raise NPUUnavailable(f"NPU service 5xx error: {response.status_code}")
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
