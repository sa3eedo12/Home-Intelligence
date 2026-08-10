from __future__ import annotations

import os
from datetime import datetime, timezone
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
        think: bool | None = None,
        keep_alive: int | str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a chat completion request to Ollama.

        ⚠ DO NOT add num_ctx to options. Models are loaded at boot with
        their training default context (262144 for the 35B, 40960 for
        the 8B). Passing a different num_ctx forces Ollama to RELOAD
        the model with the new context — on the 35B this is a ~170s
        stall. If you genuinely need a different context budget, set
        OLLAMA_CONTEXT_LENGTH globally and let the boot warmer pick it
        up. The embed() method below is the one exception: bge-m3 must
        cap num_ctx at 512 to fit inside Vulkan's 4 GiB allocation
        limit, and bge-m3 is only ever called for embeddings (single
        ctx everywhere) so the reload risk doesn't apply.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if response_format is not None:
            payload["format"] = response_format
        # Qwen3 family: setting think=False skips the <thinking> trace,
        # which can save 30-90s on a 35B model when the task is purely
        # structured-JSON generation that doesn't benefit from chain-of-thought.
        if think is not None:
            payload["think"] = think
        # keep_alive controls how long Ollama keeps the model resident
        # after the call. -1 = pin forever (until ollama restart). Used
        # by the orchestrator startup warmer to keep the 35B reasoner
        # always available without paying the 3-minute cold-load tax.
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        effective_timeout = timeout if timeout is not None else _ollama_timeout()
        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.2,
        response_format: str | None = None,
        think: bool | None = None,
        keep_alive: int | str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if response_format is not None:
            payload["format"] = response_format
        if think is not None:
            payload["think"] = think
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        effective_timeout = timeout if timeout is not None else _ollama_timeout()
        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            resp = await client.post(f"{self.base_url}/api/generate", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def embed(self, text: str, model: str = "bge-m3") -> list[float]:
        # num_ctx=512 keeps the compute buffer under Vulkan's 4 GiB
        # per-allocation limit. bge-m3's default n_ctx is 8192 which
        # asks Ollama for a ~4.4 GiB buffer and crashes on Vulkan with
        # "failed to allocate compute pp buffers". 512 is the standard
        # sentence-embedder context and ample for our event-log /
        # knowledge-graph entries (a few sentences each). The ROCm
        # backend never hit this limit, so the bug only surfaced after
        # the Vulkan switch.
        async with httpx.AsyncClient(timeout=_ollama_timeout()) as client:
            resp = await client.post(
                f"{self.base_url}/api/embeddings",
                json={
                    "model": model,
                    "prompt": text,
                    "options": {"num_ctx": 512},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("embedding", [])

    async def loaded_models(self) -> set[str]:
        """Names of the models Ollama currently holds resident.

        Ollama reports tags as `name:tag`; callers generally hold the bare
        tag name, so both forms are returned. Raises on transport errors so
        callers can distinguish "Ollama is down" from "nothing is loaded" —
        treating an unreachable Ollama as an empty set would trigger a
        pointless warm of every model against a dead endpoint.
        """
        return set(await self.model_residency())

    async def model_residency(self) -> dict[str, datetime | None]:
        """Resident model names mapped to when Ollama will evict them.

        Absence alone is a lagging signal: by the time a model has dropped
        out, the next real request already pays the cold-load tax. Exposing
        the expiry lets callers act on the *approaching* eviction instead.

        Both `name:tag` and bare-tag keys are returned, as for
        `loaded_models`. The value is None when Ollama omits or cannot parse
        `expires_at`, which callers should read as "no expiry known" rather
        than "expires now". Pinned models (keep_alive=-1) report a date
        centuries out, which compares correctly without special-casing.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.base_url}/api/ps")
            resp.raise_for_status()
            residency: dict[str, datetime | None] = {}
            for entry in resp.json().get("models") or []:
                name = entry.get("name") or entry.get("model")
                if not name:
                    continue
                expires: datetime | None = None
                raw = entry.get("expires_at")
                if isinstance(raw, str):
                    try:
                        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    except ValueError:
                        expires = None
                    else:
                        if expires.tzinfo is None:
                            expires = expires.replace(tzinfo=timezone.utc)
                residency[name] = expires
                residency[name.split(":", 1)[0]] = expires
            return residency
