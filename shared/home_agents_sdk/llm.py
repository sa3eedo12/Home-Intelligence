from __future__ import annotations

from typing import Any

import httpx


class OllamaClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        format: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if format is not None:
            payload["format"] = format
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.2,
        format: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if format is not None:
            payload["format"] = format
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{self.base_url}/api/generate", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def embed(self, text: str, model: str = "bge-m3") -> list[float]:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/api/embeddings",
                json={"model": model, "prompt": text},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("embedding", [])
