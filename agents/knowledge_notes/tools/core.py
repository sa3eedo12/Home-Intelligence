from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import asyncpg
from bs4 import BeautifulSoup
from docx import Document
from home_agents_sdk import tool
from home_agents_sdk.embeddings import Embedder
from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.npu import NPUClient
from pypdf import PdfReader
from qdrant_client import AsyncQdrantClient, models

_POOL: asyncpg.Pool | None = None
_QDRANT: AsyncQdrantClient | None = None


async def _pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        database_url = os.getenv(
            "DATABASE_URL", "postgresql://agents:changeme@postgres:5432/agents"
        )
        _POOL = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    return _POOL


def _qdrant() -> AsyncQdrantClient:
    global _QDRANT
    if _QDRANT is None:
        _QDRANT = AsyncQdrantClient(url=os.getenv("QDRANT_URL", "http://qdrant:6333"))
    return _QDRANT


async def _embedder() -> Embedder:
    pool = await _pool()
    npu = NPUClient(os.getenv("LEMONADE_URL", "http://lemonade:8000"))
    llm = OllamaClient(os.getenv("OLLAMA_URL", "http://ollama:11434"))
    return Embedder(npu=npu, llm=llm, pool=pool, npu_model=os.getenv("EMBED_MODEL", "bge-m3-int8"))


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix in {".html", ".htm"}:
        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
        return soup.get_text("\n")
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix == ".docx":
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    return ""


def _chunk_text(text: str, chunk_tokens: int = 500, overlap: int = 80) -> list[dict[str, Any]]:
    if not text.strip():
        return []
    tokens = text.split()
    if not tokens:
        return []
    chunks: list[dict[str, Any]] = []
    step = max(1, chunk_tokens - overlap)
    for idx, start in enumerate(range(0, len(tokens), step)):
        stop = min(len(tokens), start + chunk_tokens)
        token_slice = tokens[start:stop]
        if not token_slice:
            continue
        chunk_text = " ".join(token_slice)
        chunks.append(
            {
                "chunk_index": idx,
                "start_line": start + 1,
                "end_line": stop,
                "text": chunk_text,
            }
        )
        if stop >= len(tokens):
            break
    return chunks


async def _ensure_collection() -> None:
    client = _qdrant()
    try:
        await client.get_collection("notes")
    except Exception:
        await client.create_collection(
            "notes",
            vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE),
        )


async def _upsert_chunks(path: str, mtime: float, chunks: list[dict[str, Any]]) -> int:
    emb = await _embedder()
    client = _qdrant()
    points = []
    for chunk in chunks:
        vector = await emb.embed(chunk["text"])
        point_id = hashlib.sha256(f"{path}:{chunk['chunk_index']}".encode()).hexdigest()
        points.append(
            models.PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "path": path,
                    "chunk_index": chunk["chunk_index"],
                    "start_line": chunk["start_line"],
                    "end_line": chunk["end_line"],
                    "mtime": mtime,
                    "text": chunk["text"],
                },
            )
        )
    if points:
        await client.upsert("notes", points=points)
    return len(points)


@tool("index_path", side_effects=True)
async def index_path(path: str | None = None) -> dict[str, Any]:
    await _ensure_collection()
    root = path or os.getenv("NOTES_ROOT", "/data/notes")
    if not os.path.exists(root):  # noqa: ASYNC240
        return {"ok": False, "error": f"path not found: {root}"}

    indexed = 0
    skipped = 0
    pool = await _pool()
    async with pool.acquire() as conn:
        for base, _dirs, files in os.walk(root):
            for name in files:
                file = os.path.join(base, name)
                suffix = Path(file).suffix.lower()
                if suffix not in {".md", ".txt", ".pdf", ".docx", ".html", ".htm"}:
                    continue
                text = _extract_text(Path(file))
                sha = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
                existing = await conn.fetchrow(
                    "SELECT sha256 FROM indexed_documents WHERE path = $1",
                    file,
                )
                if existing and existing["sha256"] == sha:
                    skipped += 1
                    continue
                chunks = _chunk_text(text)
                chunk_count = await _upsert_chunks(file, os.path.getmtime(file), chunks)  # noqa: ASYNC240
                await conn.execute(
                    """
                    INSERT INTO indexed_documents(path, sha256, chunk_count, indexed_at)
                    VALUES ($1, $2, $3, now())
                    ON CONFLICT (path)
                    DO UPDATE SET
                        sha256 = EXCLUDED.sha256,
                        chunk_count = EXCLUDED.chunk_count,
                        indexed_at = now()
                    """,
                    file,
                    sha,
                    chunk_count,
                )
                indexed += 1
    return {"ok": True, "indexed": indexed, "skipped": skipped}


async def _search_qdrant(query: str, top_k: int) -> list[dict[str, Any]]:
    emb = await _embedder()
    vector = await emb.embed(query)
    client = _qdrant()
    hits = await client.search("notes", query_vector=vector, limit=top_k)
    return [
        {
            "score": float(hit.score),
            "path": hit.payload.get("path"),
            "chunk_index": hit.payload.get("chunk_index"),
            "start_line": hit.payload.get("start_line"),
            "end_line": hit.payload.get("end_line"),
            "text": hit.payload.get("text"),
        }
        for hit in hits
    ]


@tool("search")
async def search(query: str, top_k: int = 5) -> dict[str, Any]:
    return {"items": await _search_qdrant(query, top_k)}


@tool("summarize")
async def summarize(path: str | None = None, query: str | None = None) -> dict[str, Any]:
    if query:
        items = await _search_qdrant(query, 5)
        text = "\n".join(item.get("text", "") for item in items)
    elif path:
        text = _extract_text(Path(path))
    else:
        return {"summary": "No input provided."}
    words = text.split()
    return {"summary": " ".join(words[:120])[:600]}


@tool("ask")
async def ask(question: str, top_k: int = 4) -> dict[str, Any]:
    items = await _search_qdrant(question, top_k)
    if not items:
        return {"answer": "I could not find relevant notes.", "sources": []}
    answer = " ".join(item.get("text", "") for item in items)[:420]
    sources = [f"[source: {item['path']}]" for item in items if item.get("path")]
    return {"answer": f"{answer}\n\n{' '.join(sources)}", "sources": sources}


@tool("list_indexed")
async def list_indexed() -> dict[str, Any]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT path, chunk_count, indexed_at FROM indexed_documents "
            "ORDER BY indexed_at DESC LIMIT 100"
        )
    return {"count": len(rows), "items": [dict(r) for r in rows]}


@tool("forget_path", side_effects=True)
async def forget_path(path: str) -> dict[str, Any]:
    client = _qdrant()
    await client.delete(
        collection_name="notes",
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="path", match=models.MatchValue(value=path))]
            )
        ),
    )
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM indexed_documents WHERE path = $1", path)
    return {"ok": True, "path": path}
