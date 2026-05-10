# PR 2 — Orchestrator & Home Automation Agent

## Overview

PR 2 delivers the orchestrator service, the Telegram bot interface, the first agent (`home_automation`), and the capability registry that connects them.

## Architecture

```
Telegram User
     │
     ▼
┌─────────────────────────────────────────┐
│  orchestrator/app.py  (FastAPI)         │
│  ┌──────────────┐  ┌──────────────┐    │
│  │ telegram_bot │  │  /route API  │    │
│  └──────┬───────┘  └──────┬───────┘    │
│         │                 │            │
│         ▼                 ▼            │
│       Router  ◄──── CapabilityRegistry │
│         │          (Qdrant embeddings) │
│         │                             │
│         ▼                             │
│    WorkflowEngine  (asyncpg/pg)       │
└─────────────────────────────────────────┘
          │  HTTP /invoke
          ▼
┌─────────────────────────────────────────┐
│  agents/home_automation/app.py          │
│  tools: core, scenes, doorbell,         │
│         anomaly, suggest                │
└─────────────────────────────────────────┘
          │
          ▼
     Home Assistant REST API
```

## Key components

### orchestrator/

| File | Purpose |
|------|---------|
| `app.py` | FastAPI app with lifespan; wires all services |
| `router.py` | NPU-based intent classifier + semantic fallback |
| `registry.py` | Fetches agent manifests, embeds capabilities into Qdrant |
| `workflow.py` | Postgres-backed workflow state machine |
| `telegram_bot.py` | python-telegram-bot v20 handlers (text, voice, photo, callbacks) |
| `voice.py` | Downloads and transcribes Telegram voice notes via NPU Whisper |
| `notify.py` | Redis Streams consumer → sends Telegram messages |
| `health.py` | Async health probes for all infrastructure |
| `scheduler.py` | APScheduler placeholder (jobs added in PR 4) |
| `policies.yaml` | Quiet hours and confirmation policy config |

### agents/home_automation/

| File | Purpose |
|------|---------|
| `app.py` | FastAPI agent; subscribes to `events.home` stream |
| `manifest.yaml` | Declares all 10 capabilities |
| `tools/core.py` | `list_entities`, `get_entity_state`, `call_service` |
| `tools/scenes.py` | `list_scenes`, `set_scene` |
| `tools/doorbell.py` | `doorbell.snapshot`, `doorbell.summarize_event`, `doorbell.last_visitor` |
| `tools/anomaly.py` | `anomaly.scan` (z-score placeholder) |
| `tools/suggest.py` | `suggest_automation` (LLM-powered YAML proposals) |
| `tools/ha_client.py` | Async Home Assistant REST client |

## Routing flow

1. User sends message via Telegram or `POST /route`
2. Router sends message + available agents to NPU router LLM (Qwen3-1.7B INT4)
3. LLM returns `{agent, capability, inputs, needs_confirmation, reason}` as JSON
4. If LLM returns `null` agent: semantic fallback via Qdrant cosine search (threshold 0.55)
5. If `needs_confirmation`: create workflow in Postgres, send inline keyboard to user
6. On confirmation: resume workflow, dispatch to agent `/invoke`
7. Agent executes tool, returns result via HTTP

## Capability registry

- On startup, orchestrator fetches `/manifest` from each agent URL
- Each capability description is embedded via NPU (`bge-m3-int8`) and upserted into Qdrant
- Cached in Postgres `embedding_cache` table for reuse across restarts
- `GET /status` reports health of all infrastructure + registered agents

## First end-to-end test via Telegram

After `make dev` and filling `.env`:

```bash
# In Telegram, send to your bot:
turn off the living room lights
# → Router identifies home_automation/call_service
# → Agent calls HA REST API
# → Bot replies with result

# Voice note (Arabic or English)
# → NPU Whisper transcribes → same routing flow

# Status check
/status
# → Lists registered agents
```

## PR 2 status

- [x] Orchestrator FastAPI service with lifespan management
- [x] Telegram bot (text, voice, photo, inline keyboards)
- [x] Intent router with NPU LLM + Qdrant semantic fallback
- [x] Capability registry with embedding indexing
- [x] Postgres workflow engine (start/resume/mark_done/mark_failed)
- [x] Redis Streams notification consumer
- [x] home_automation agent with 10 capabilities
- [x] Home Assistant async REST client
- [x] Doorbell vision pipeline (NPU yolov8n + Ollama summarization)
- [x] Anomaly scan and automation suggestion stubs
- [x] Unit tests for router, workflow, registry, HA client, manifest
- [ ] Quiet hours enforcement (PR 4)
- [ ] Rate limiting (PR 4)
- [ ] Scheduled proactive checks (PR 4)
