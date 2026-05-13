from __future__ import annotations

import os
from typing import Any

import httpx


def _ollama_timeout() -> float:
    """Default timeout for Ollama requests. JSON-structured chats with the
    nightly reflector model can routinely take 60-90s on modest hardware,
    so the historical 60s default caused silent ReadTimeout failures
    swallowed as empty errors. 180s is a generous ceiling that lets the
    reflector finish without indefinitely hanging the orchestrator."""
    raw = os.environ.get("OLLAMA_TIMEOUT_SECONDS", "180")
    try:
        return max(10.0, float(raw))
    except (TypeError, ValueError):
        return 180.0


class OllamaClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if response_format is not None:
            payload["format"] = response_format
        async with httpx.AsyncClient(timeout=_ollama_timeout()) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.2,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if response_format is not None:
            payload["format"] = response_format
        async with httpx.AsyncClient(timeout=_ollama_timeout()) as client:
            resp = await client.post(f"{self.base_url}/api/generate", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def embed(self, text: str, model: str = "bge-m3") -> list[float]:
        async with httpx.AsyncClient(timeout=_ollama_timeout()) as client:
            resp = await client.post(
                f"{self.base_url}/api/embeddings",
                json={"model": model, "prompt": text},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("embedding", [])
