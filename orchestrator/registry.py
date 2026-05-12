from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
from home_agents_sdk.telemetry import get_logger
from qdrant_client import AsyncQdrantClient, models

logger = get_logger("registry")

_COLLECTION = "capabilities"
_VECTOR_SIZE = 1024
_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # uuid.NAMESPACE_URL


class CapabilityRegistry:
    def __init__(
        self,
        agent_urls: dict[str, str],
        qdrant: AsyncQdrantClient,
        embedder: Any,
    ) -> None:
        self._agent_urls = agent_urls
        self._qdrant = qdrant
        self._embedder = embedder
        self._manifests: dict[str, dict] = {}
        self._capabilities: dict[tuple[str, str], dict] = {}

    async def bootstrap(self) -> None:
        await self._ensure_collection()
        for agent_id, url in self._agent_urls.items():
            await self._load_agent(agent_id, url)

    async def _ensure_collection(self) -> None:
        try:
            await self._qdrant.get_collection(_COLLECTION)
        except Exception:
            await self._qdrant.create_collection(
                _COLLECTION,
                vectors_config=models.VectorParams(
                    size=_VECTOR_SIZE, distance=models.Distance.COSINE
                ),
            )

    async def _load_agent(self, agent_id: str, url: str) -> None:
        delays = [1, 2, 4, 8, 16]
        for attempt, delay in enumerate(delays, 1):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(f"{url.rstrip('/')}/manifest")
                    resp.raise_for_status()
                    manifest = resp.json()
                self._manifests[agent_id] = manifest
                await self._index_capabilities(agent_id, manifest)
                logger.info("registry_agent_loaded", agent=agent_id)
                return
            except Exception as exc:
                if attempt == len(delays):
                    logger.warning("registry_agent_unavailable", agent=agent_id, error=str(exc))
                    return
                logger.warning(
                    "registry_agent_retry",
                    agent=agent_id,
                    attempt=attempt,
                    retry_in=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)

    async def _index_capabilities(self, agent_id: str, manifest: dict) -> None:
        points = []
        for cap in manifest.get("capabilities", []):
            cap_id = cap.get("id", "")
            description = cap.get("description", "")
            self._capabilities[(agent_id, cap_id)] = cap

            try:
                vector = await self._embedder.embed(description)
            except Exception as exc:
                logger.warning("registry_embed_failed", agent=agent_id, cap=cap_id, error=str(exc))
                continue

            point_id = str(uuid.uuid5(_NS, f"{agent_id}:{cap_id}"))
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={"agent": agent_id, "capability": cap_id, "description": description},
                )
            )

        if points:
            await self._qdrant.upsert(_COLLECTION, points=points)

    async def refresh(self) -> None:
        await self.bootstrap()

    async def dispatch(self, agent: str, capability: str, inputs: dict) -> dict:
        url = self._agent_urls.get(agent)
        if url is None:
            return {"ok": False, "error": f"Unknown agent: {agent}"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{url.rstrip('/')}/invoke",
                json={"capability": capability, "payload": inputs},
            )
            resp.raise_for_status()
            return resp.json()

    async def semantic_search(self, text: str, top_k: int = 3) -> list[dict]:
        try:
            vector = await self._embedder.embed(text)
            results = await self._qdrant.search(_COLLECTION, query_vector=vector, limit=top_k)
            return [{"score": r.score, "payload": r.payload} for r in results]
        except Exception as exc:
            logger.warning("registry_search_failed", error=str(exc))
            return []

    def agents(self) -> list[str]:
        return list(self._agent_urls.keys())

    def get_capability(self, agent: str, capability: str) -> dict | None:
        return self._capabilities.get((agent, capability))

    def list_capabilities(self) -> list[dict[str, Any]]:
        """Return all known capabilities as flat dicts for prompt-building.

        Each entry has at least `agent`, `id`, and `description` keys; other
        manifest fields (`inputs`, `cost`, `side_effects`,
        `require_confirmation`) are passed through when present.
        """
        out: list[dict[str, Any]] = []
        for (agent, cap_id), meta in self._capabilities.items():
            entry: dict[str, Any] = {
                "agent": agent,
                "id": cap_id,
                "description": meta.get("description", ""),
            }
            for k in ("inputs", "cost", "side_effects", "require_confirmation"):
                if k in meta:
                    entry[k] = meta[k]
            out.append(entry)
        out.sort(key=lambda e: (e["agent"], e["id"]))
        return out

    def capability_counts(self) -> dict[str, int]:
        counts = {agent: 0 for agent in self._agent_urls}
        for agent, _capability in self._capabilities:
            counts[agent] += 1
        return counts
