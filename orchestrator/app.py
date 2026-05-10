from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI
from home_agents_sdk.bus import EventBus
from home_agents_sdk.embeddings import Embedder
from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.npu import NPUClient
from home_agents_sdk.telemetry import get_logger
from qdrant_client import AsyncQdrantClient

from .health import probe_lemonade, probe_ollama, probe_postgres, probe_qdrant, probe_redis
from .notify import run_consumer
from .registry import CapabilityRegistry
from .router import Router
from .scheduler import build_scheduler
from .telegram_bot import build_telegram_app, send
from .workflow import WorkflowEngine

logger = get_logger("orchestrator")


def _parse_agent_urls(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" in part:
            name, url = part.split("=", 1)
            result[name.strip()] = url.strip()
    return result


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    database_url = os.environ.get("DATABASE_URL", "postgresql://agents:changeme@postgres:5432/agents")
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    ollama_url = os.environ.get("OLLAMA_URL", "http://ollama:11434")
    lemonade_url = os.environ.get("LEMONADE_URL", "http://lemonade:8000")
    router_model = os.environ.get("ROUTER_MODEL", "qwen3-1.7b-int4")
    embed_model = os.environ.get("EMBED_MODEL", "bge-m3-int8")
    telegram_token = os.environ.get("TELEGRAM_TOKEN", "")
    allowed_ids_raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
    agent_urls_raw = os.environ.get("AGENT_URLS", "")

    allowed_ids: set[int] = set()
    for uid in allowed_ids_raw.split(","):
        uid = uid.strip()
        if uid.isdigit():
            allowed_ids.add(int(uid))

    # Infrastructure
    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
    bus = EventBus(redis_url)
    try:
        await bus.connect()
    except Exception as exc:
        logger.warning("bus_connect_failed", error=str(exc))

    # Clients
    npu = NPUClient(lemonade_url)
    llm = OllamaClient(ollama_url)
    qdrant = AsyncQdrantClient(url=qdrant_url)
    embedder = Embedder(npu=npu, llm=llm, pool=pool, npu_model=embed_model)

    # Core services
    workflow_engine = WorkflowEngine(pool)
    agent_urls = _parse_agent_urls(agent_urls_raw)
    registry = CapabilityRegistry(agent_urls=agent_urls, qdrant=qdrant, embedder=embedder)
    await registry.bootstrap()
    router = Router(npu=npu, registry=registry, router_model=router_model)

    # Telegram
    tg_app = await build_telegram_app(
        token=telegram_token,
        allowed_ids=allowed_ids,
        router=router,
        workflow_engine=workflow_engine,
    )
    await tg_app.initialize()
    await tg_app.start()
    if telegram_token and telegram_token != "replace-with-token-from-botfather":
        await tg_app.updater.start_polling(drop_pending_updates=True)

    # Background tasks
    async def _send_fn(chat_id: int, text: str, keyboard: Any) -> None:
        await send(tg_app, chat_id, text)

    notify_task = asyncio.create_task(run_consumer(redis_url, _send_fn))

    # Scheduler (no jobs - Jobs added in PR 4)
    scheduler = build_scheduler()
    scheduler.start()

    # Store state
    app.state.pool = pool
    app.state.registry = registry
    app.state.router = router
    app.state.tg_app = tg_app
    app.state.notify_task = notify_task
    app.state.scheduler = scheduler
    app.state.redis_url = redis_url
    app.state.qdrant_url = qdrant_url
    app.state.ollama_url = ollama_url
    app.state.lemonade_url = lemonade_url

    logger.info("orchestrator_started")

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    notify_task.cancel()
    try:
        await tg_app.updater.stop()
    except Exception:
        pass
    await tg_app.stop()
    await tg_app.shutdown()
    await pool.close()
    logger.info("orchestrator_stopped")


app = FastAPI(title="orchestrator", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    registry: CapabilityRegistry = app.state.registry
    return {"ok": True, "agents": registry.agents()}


@app.get("/status")
async def status() -> dict:
    results = await asyncio.gather(
        probe_ollama(app.state.ollama_url),
        probe_lemonade(app.state.lemonade_url),
        probe_postgres(app.state.pool),
        probe_redis(app.state.redis_url),
        probe_qdrant(app.state.qdrant_url),
    )
    return {
        "ollama": results[0],
        "lemonade": results[1],
        "postgres": results[2],
        "redis": results[3],
        "qdrant": results[4],
        "agents": app.state.registry.agents(),
    }


@app.post("/route")
async def route(body: dict) -> dict:
    text = body.get("text", "")
    user_id = body.get("user_id", "api")
    router: Router = app.state.router
    return await router.handle(text, user_id)
