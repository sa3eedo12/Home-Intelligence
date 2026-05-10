from __future__ import annotations

import hashlib

import asyncpg

from .llm import OllamaClient
from .npu import NPUClient, NPUUnavailable


class Embedder:
    def __init__(
        self, npu: NPUClient, llm: OllamaClient, pool: asyncpg.Pool, npu_model: str
    ) -> None:
        self.npu = npu
        self.llm = llm
        self.pool = pool
        self.npu_model = npu_model

    async def embed(self, text: str) -> list[float]:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        async with self.pool.acquire() as conn:
            cached = await conn.fetchrow(
                "SELECT vector FROM embedding_cache WHERE text_hash = $1 AND model = $2",
                text_hash,
                self.npu_model,
            )
            if cached and cached["vector"] is not None:
                return list(cached["vector"])

        try:
            vector = (await self.npu.embed(self.npu_model, [text]))[0]
            model_used = self.npu_model
        except NPUUnavailable:
            vector = await self.llm.embed(text, model="bge-m3")
            model_used = "bge-m3"

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO embedding_cache(text_hash, vector, model)
                VALUES ($1, $2, $3)
                ON CONFLICT (text_hash)
                DO UPDATE SET vector = EXCLUDED.vector, model = EXCLUDED.model, created_at = now()
                """,
                text_hash,
                vector,
                model_used,
            )

        return vector
