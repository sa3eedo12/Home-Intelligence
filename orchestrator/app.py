from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import yaml
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from home_agents_sdk.bus import EventBus
from home_agents_sdk.embeddings import Embedder
from home_agents_sdk.event_log import EventLogStore
from home_agents_sdk.health_store import HealthStore
from home_agents_sdk.knowledge_graph import KnowledgeGraph
from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.npu import NPUClient
from home_agents_sdk.reflection_store import ReflectionStore
from home_agents_sdk.telemetry import get_logger
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from .activity import ActivityAggregator
from .admin import router as admin_router
from .advisor import Advisor
from .dashboard import router as dashboard_router
from .data_science import LoraTrainingJob, MaintenanceJob, PatternMiner, ReembedJob, ReportGenerator
from .event_recorder import EventRecorder
from .github_client import GitHubClient
from .ha_event_bridge import build_from_env as build_ha_bridge
from .health import probe_lemonade, probe_ollama, probe_postgres, probe_qdrant, probe_redis
from .migrations import run_pending_migrations
from .notify import run_consumer, send_morning_brief
from .observers import ObserverRunner
from .observers.coffee_observer import build as build_coffee
from .observers.presence_observer import build as build_presence
from .observers.sleep_observer import build as build_sleep
from .observers.tv_observer import build as build_tv
from .observers.vacuum_observer import build as build_vacuum
from .observers.washer_observer import build as build_washer
from .policy_engine import PolicyEngine
from .reactive import Reactive
from .reflector import NightlyReflector
from .registry import CapabilityRegistry
from .router import Router
from .safety import SafetyPolicy
from .scheduler import Scheduler
from .sse import router as sse_router
from .telegram_bot import build_telegram_app, send
from .workflow import WorkflowEngine

logger = get_logger("orchestrator")


HERE = Path(__file__).resolve().parent


def _parse_agent_urls(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" in part:
            name, url = part.split("=", 1)
            result[name.strip()] = url.strip()
    return result


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _parse_timestamp(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _reflection_status(app: FastAPI) -> dict[str, Any]:
    store = getattr(app.state, "reflection_store", None)
    if store is None:
        store = ReflectionStore(getattr(app.state, "pool", None))
    try:
        briefs = await store.list_briefs(limit=1)
    except Exception as exc:
        logger.warning("reflection_status_failed", error=str(exc))
        briefs = []
    if not briefs:
        return {"last_run_at": None, "age_hours": None, "healthy": False}
    last_run_at = briefs[0].get("generated_at")
    ts = _parse_timestamp(last_run_at)
    if ts is None:
        return {"last_run_at": last_run_at, "age_hours": None, "healthy": False}
    age_hours = (datetime.now(UTC) - ts.astimezone(UTC)).total_seconds() / 3600
    return {
        "last_run_at": last_run_at,
        "age_hours": round(age_hours, 2),
        "healthy": age_hours < 25,
    }


async def _people_home(app: FastAPI) -> list[str]:
    """Compute the list of household members currently at home.

    Reads the most recent ``presence.changed`` event per entity from the
    event_log and returns the friendly names whose latest state is ``home``.
    Returns an empty list when no presence events have ever been observed
    (the dashboard then falls back to the "Presence learning" placeholder).
    """
    pool = getattr(app.state, "pool", None)
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (payload->>'entity_id')
                    payload->>'entity_id' AS entity_id,
                    payload->>'state'     AS state,
                    payload->>'person'    AS person,
                    ts
                FROM event_log
                WHERE capability = 'presence.changed'
                  AND payload ? 'state'
                  AND ts > now() - interval '7 days'
                ORDER BY payload->>'entity_id', ts DESC
                """
            )
    except Exception as exc:
        logger.warning("people_home_query_failed", error=str(exc))
        return []
    home: list[str] = []
    for row in rows:
        if str(row.get("state") or "").lower() == "home":
            name = str(row.get("person") or row.get("entity_id") or "").strip()
            if name and name not in home:
                home.append(name)
    return home


async def _build_status(app: FastAPI) -> dict[str, Any]:
    results = await asyncio.gather(
        probe_ollama(app.state.ollama_url),
        probe_lemonade(app.state.lemonade_url),
        probe_postgres(app.state.pool),
        probe_redis(app.state.redis_url),
        probe_qdrant(app.state.qdrant_url),
    )
    recent = await app.state.policy_engine.get_recent_decisions(limit=20)
    recent_alerts = [
        item
        for item in recent
        if str(item.get("severity", "")).lower() in {"warn", "alert", "critical"}
    ][:10]

    outbound = await app.state.redis.xrevrange("notify.outbound", count=5)
    last_outbound: list[dict[str, Any]] = []
    for message_id, fields in outbound:
        try:
            payload = json.loads(fields.get("payload", "{}"))
        except Exception:
            payload = {"raw": fields.get("payload")}
        payload["id"] = message_id
        last_outbound.append(payload)

    quiet_override = await app.state.redis.get("policy:override:quiet")
    activity_snapshot = app.state.activity_aggregator.snapshot()
    recent_activity = app.state.activity_aggregator.recent_events(limit=30)
    people_home = await _people_home(app)

    narrative_raw = await app.state.redis.get("dashboard:narrative")
    alert_narrative_raw = await app.state.redis.get("dashboard:alert_narrative")
    narrative = json.loads(narrative_raw) if narrative_raw else None
    alert_narrative = json.loads(alert_narrative_raw) if alert_narrative_raw else None
    reflection = await _reflection_status(app)

    return {
        "stack": {
            "ollama": results[0],
            "lemonade": results[1],
            "postgres": results[2],
            "redis": results[3],
            "qdrant": results[4],
            "orchestrator": {"ok": True},
        },
        "agents": app.state.registry.agents(),
        "capability_counts": app.state.registry.capability_counts(),
        "jobs": [
            {
                "id": job.id,
                "next_run_time": job.next_run_time,
                "last_run_time": job.last_run_time,
                "last_status": job.last_status,
            }
            for job in app.state.scheduler.list_jobs()
        ],
        "recent_alerts": recent_alerts,
        "recent_notifications": recent,
        "last_outbound": last_outbound,
        "suppression_counts": await app.state.policy_engine.get_stats(),
        "active_mutes": await app.state.policy_engine.get_active_mutes(),
        "quiet_override": quiet_override,
        "models": {
            "igpu": results[0].get("models", []),
            "npu": results[1].get("models", []),
        },
        "activity": activity_snapshot,
        "recent_activity": recent_activity,
        "narrative": narrative,
        "alert_narrative": alert_narrative,
        "reflection": reflection,
        "people_home": people_home,
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://agents:changeme@postgres:5432/agents"
    )
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    ollama_url = os.environ.get("OLLAMA_URL", "http://ollama:11434")
    lemonade_url = os.environ.get("LEMONADE_URL", "http://lemonade:8000")
    router_model = os.environ.get("ROUTER_MODEL", "qwen3-1.7b-int4")
    embed_model = os.environ.get("EMBED_MODEL", "bge-m3-int8")
    telegram_token = os.environ.get("TELEGRAM_TOKEN", "")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "0")
    allowed_ids_raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
    agent_urls_raw = os.environ.get("AGENT_URLS", "")
    admin_base_url = os.environ.get("ORCHESTRATOR_BASE_URL", "http://localhost:8080")
    github_repo_token = os.environ.get("GITHUB_REPO_TOKEN") or None
    github_repo = os.environ.get("GITHUB_REPO") or None

    allowed_ids: set[int] = {
        int(uid.strip()) for uid in allowed_ids_raw.split(",") if uid.strip().isdigit()
    }

    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
    # Run any pending migrations BEFORE anything else queries the DB. Each
    # init/*.sql file is idempotent (CREATE TABLE IF NOT EXISTS etc.) and we
    # also track applied_migrations so re-runs are cheap.
    migration_results = await run_pending_migrations(pool)
    app.state.migration_results = migration_results
    bus = EventBus(redis_url)
    try:
        await bus.connect()
    except Exception as exc:
        logger.warning("bus_connect_failed", error=str(exc))

    redis = Redis.from_url(redis_url, decode_responses=True)
    await redis.set("config:telegram_chat_id", telegram_chat_id)

    npu = NPUClient(lemonade_url)
    llm = OllamaClient(ollama_url)
    qdrant = AsyncQdrantClient(url=qdrant_url)
    embedder = Embedder(npu=npu, llm=llm, pool=pool, npu_model=embed_model)

    workflow_engine = WorkflowEngine(pool)
    agent_urls = _parse_agent_urls(agent_urls_raw)
    registry = CapabilityRegistry(agent_urls=agent_urls, qdrant=qdrant, embedder=embedder)
    await registry.bootstrap()
    default_model = os.environ.get("DEFAULT_MODEL", "qwen3:8b")
    humanizer_model = os.environ.get("HUMANIZER_MODEL", default_model)
    safety = SafetyPolicy(path=os.environ.get("SAFETY_POLICY_PATH", "policies/safety.yaml"))
    reflection_store = ReflectionStore(pool)
    knowledge_graph = KnowledgeGraph(pool=pool)
    github_client = GitHubClient(github_repo_token, github_repo)
    event_log_store = EventLogStore(pool=pool, qdrant=qdrant, embedder=embedder)
    health_store = HealthStore(pool=pool)
    reembed = ReembedJob(
        pool=pool, qdrant=qdrant, embedder=embedder, event_log_store=event_log_store
    )
    pattern_miner = PatternMiner(
        pool=pool,
        knowledge_graph=knowledge_graph,
        event_log_store=event_log_store,
    )
    reports = ReportGenerator(pool=pool, llm=llm, event_log_store=event_log_store)
    maintenance = MaintenanceJob(pool=pool, redis=redis, event_log_store=event_log_store)
    lora_training = LoraTrainingJob(pool=pool, llm=llm, event_log_store=event_log_store)
    router = Router(
        npu=npu,
        registry=registry,
        router_model=router_model,
        llm=llm,
        llm_fallback_model=default_model,
        humanizer_model=humanizer_model,
        safety=safety,
        proposal_store=reflection_store,
    )

    policy_engine = PolicyEngine(_load_yaml(HERE / "policies.yaml"), redis)
    reasoner_model = os.environ.get("REASONER_MODEL", "qwen3.6:35b-a3b")
    reflector = NightlyReflector(
        pool=pool,
        redis=redis,
        llm=llm,
        registry=registry,
        reasoner_model=reasoner_model,
        fallback_model=default_model,
    )
    reflector.store = reflection_store
    reflector.health_store = health_store
    advisor = Advisor(
        pool=pool,
        redis=redis,
        llm=llm,
        registry=registry,
        safety=safety,
        default_model=default_model,
    )
    advisor.store = reflection_store
    tg_app_holder: dict[str, Any] = {}

    async def _run_reflector(_inputs: dict[str, Any]) -> dict[str, Any]:
        return await reflector.run_once()

    async def _run_advisor(_inputs: dict[str, Any]) -> dict[str, Any]:
        return await advisor.run_once()

    async def _send_reflection_digest(inputs: dict[str, Any]) -> dict[str, Any]:
        tg_app_ref = tg_app_holder.get("app")
        if tg_app_ref is None:
            return {"ok": False, "error": "telegram_unavailable"}
        chat_id_raw = inputs.get("chat_id") or await redis.get("config:telegram_chat_id")
        try:
            chat_id = int(chat_id_raw or telegram_chat_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "telegram_chat_id_invalid"}
        if chat_id <= 0:
            return {"ok": False, "error": "telegram_chat_id_missing"}
        briefs = await reflector.store.list_briefs(limit=1)
        if not briefs:
            await reflector.run_once()
            briefs = await reflector.store.list_briefs(limit=1)
        if not briefs:
            return {"ok": False, "error": "no_morning_brief"}
        brief = briefs[0]
        await send_morning_brief(tg_app_ref, brief, chat_id)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE morning_brief SET sent_at = now() WHERE id = $1", brief["id"]
                )
        except Exception as exc:
            logger.warning("morning_brief_sent_mark_failed", error=str(exc))
        return {"ok": True, "brief_id": brief.get("id"), "chat_id": chat_id}

    async def _run_maintenance(_inputs: dict[str, Any]) -> dict[str, Any]:
        return await maintenance.run()

    async def _run_pattern_mining(_inputs: dict[str, Any]) -> dict[str, Any]:
        return await pattern_miner.run()

    async def _run_reembed(_inputs: dict[str, Any]) -> dict[str, Any]:
        return await reembed.run()

    async def _run_weekly_report(_inputs: dict[str, Any]) -> dict[str, Any]:
        return await reports.weekly_report()

    async def _run_monthly_report(_inputs: dict[str, Any]) -> dict[str, Any]:
        return await reports.monthly_report()

    async def _run_lora_training(_inputs: dict[str, Any]) -> dict[str, Any]:
        return await lora_training.run()

    scheduler = Scheduler(
        registry=registry,
        redis=redis,
        schedules_path=str(HERE / "schedules.yaml"),
        timezone=os.environ.get("TZ", "Asia/Dubai"),
        internal_callbacks={
            "reflector.run": _run_reflector,
            "advisor.run": _run_advisor,
            "morning_brief.send": _send_reflection_digest,
            "data_science.maintenance": _run_maintenance,
            "data_science.pattern_mining": _run_pattern_mining,
            "data_science.reembed": _run_reembed,
            "data_science.weekly_report": _run_weekly_report,
            "data_science.monthly_report": _run_monthly_report,
            "data_science.lora_training": _run_lora_training,
        },
    )
    await scheduler.start()
    reactive = Reactive(
        registry=registry, redis=redis, triggers_path=str(HERE / "reactive_triggers.yaml")
    )
    await reactive.start()

    tg_app = await build_telegram_app(
        token=telegram_token,
        allowed_ids=allowed_ids,
        router=router,
        workflow_engine=workflow_engine,
        policy_engine=policy_engine,
        scheduler=scheduler,
        admin_base_url=admin_base_url,
        redis=redis,
        knowledge_graph=knowledge_graph,
        proposal_store=reflection_store,
    )
    tg_app_holder["app"] = tg_app
    await tg_app.initialize()
    await tg_app.start()
    if telegram_token and telegram_token != "replace-with-token-from-botfather":
        await tg_app.updater.start_polling(drop_pending_updates=True)

    async def _send_fn(chat_id: int, text: str, keyboard: Any) -> None:
        await send(tg_app, chat_id, text, reply_markup=keyboard)

    notify_task = asyncio.create_task(
        run_consumer(redis=redis, policy_engine=policy_engine, send_fn=_send_fn)
    )

    activity_aggregator = ActivityAggregator(redis)
    await activity_aggregator.start()

    async def _warm_models() -> None:
        """Send a tiny inference to Ollama on startup so the first user
        request doesn't pay the cold-load tax. Best-effort; never blocks
        startup if Ollama is slow."""
        for model in {router_model, default_model}:
            if not model:
                continue
            try:
                await llm.chat(
                    messages=[{"role": "user", "content": "warm"}],
                    model=model,
                    temperature=0.0,
                )
                logger.info("orchestrator_warm_model_ok", model=model)
            except Exception as exc:
                logger.info("orchestrator_warm_model_skipped", model=model, error=str(exc))

    warmup_task = asyncio.create_task(_warm_models(), name="orchestrator-warmup")

    event_recorder = EventRecorder(redis, event_log_store)
    await event_recorder.start()

    # Observer events are surfaced through events.activity; the dashboard activity feed
    # provides the observer tile/surface instead of adding static dashboard tiles.
    observer_runner = ObserverRunner(
        bus=bus,
        event_log_store=event_log_store,
        registry=registry,
        observers=[
            build_washer(),
            build_vacuum(),
            build_presence(),
            build_sleep(),
            build_coffee(),
            build_tv(),
        ],
    )
    await observer_runner.start()

    ha_event_bridge = build_ha_bridge(redis)
    await ha_event_bridge.start()

    app.state.pool = pool
    app.state.registry = registry
    app.state.router = router
    app.state.tg_app = tg_app
    app.state.notify_task = notify_task
    app.state.scheduler = scheduler
    app.state.policy_engine = policy_engine
    app.state.safety = safety
    app.state.reflector = reflector
    app.state.advisor = advisor
    app.state.reflection_store = reflection_store
    app.state.github_client = github_client
    app.state.reactive = reactive
    app.state.redis = redis
    app.state.redis_url = redis_url
    app.state.qdrant_url = qdrant_url
    app.state.ollama_url = ollama_url
    app.state.lemonade_url = lemonade_url
    app.state.activity_aggregator = activity_aggregator
    app.state.event_log_store = event_log_store
    app.state.health_store = health_store
    app.state.event_recorder = event_recorder
    app.state.observer_runner = observer_runner
    app.state.ha_event_bridge = ha_event_bridge
    app.state.knowledge_graph = knowledge_graph
    app.state.embedder = embedder
    app.state.reembed = reembed
    app.state.pattern_miner = pattern_miner
    app.state.reports = reports
    app.state.maintenance = maintenance
    app.state.lora_training = lora_training
    app.state.status_provider = lambda: _build_status(app)

    async def _reload_from_signal() -> None:
        await policy_engine.reload(_load_yaml(HERE / "policies.yaml"))
        safety.reload()
        await scheduler.reload()
        await reactive.reload()

    def _handle_sighup(_sig: int, _frame: Any) -> None:
        asyncio.create_task(_reload_from_signal())

    try:
        signal.signal(signal.SIGHUP, _handle_sighup)
    except Exception:
        logger.warning("sighup_handler_unavailable")

    logger.info("orchestrator_started")
    yield

    await observer_runner.stop()
    await ha_event_bridge.stop()
    await reactive.stop()
    await event_recorder.stop()
    await activity_aggregator.stop()
    await scheduler.shutdown()
    notify_task.cancel()
    warmup_task.cancel()
    try:
        await tg_app.updater.stop()
    except Exception:
        pass
    await tg_app.stop()
    await tg_app.shutdown()
    await redis.aclose()
    await pool.close()
    logger.info("orchestrator_stopped")


app = FastAPI(title="orchestrator", lifespan=lifespan)
app.include_router(admin_router)
app.include_router(dashboard_router)
app.include_router(sse_router)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.get("/health")
async def health() -> dict:
    registry: CapabilityRegistry = app.state.registry
    return {"ok": True, "agents": registry.agents()}


@app.get("/status")
async def status() -> dict:
    return await _build_status(app)


@app.post("/route")
async def route(body: dict) -> dict:
    text = body.get("text", "")
    user_id = body.get("user_id", "api")
    autonomous = bool(body.get("autonomous", False))
    router: Router = app.state.router
    return await router.handle(text, user_id, autonomous=autonomous)
