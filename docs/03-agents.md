# PR 3 — Remaining Domain Agents

PR 3 adds six new domain agents on top of the foundation and orchestrator delivered in PR 1 and PR 2. Each agent is a standalone FastAPI microservice built with `home_agents_sdk`, with `@tool` capabilities validated against its `manifest.yaml` at startup.

## Agent scopes

- **personal_assistant** handles reminders, renewals, appointments, and short daily brief outputs. It stores schedule-oriented records in Postgres and parses natural language time using `dateparser` with `Asia/Dubai` defaults.
- **system_health** performs one-shot host/container scans, process ranking, anomaly checks against rolling baseline samples, and operational optimization suggestions.
- **storage_backup** focuses on storage posture: disk usage, largest file scans, duplicate clusters via SHA-256 for same-size files, and backup policy validation from YAML.
- **knowledge_notes** ingests and chunks notes/documents, embeds chunks, indexes them into Qdrant for semantic retrieval, and provides summarize/ask capabilities with source markers.
- **household_ops** tracks chores, shopping list, pantry stock, and meal planning primitives with recurrence-aware chore rollovers.
- **entertainment** supports media library indexing/search, recommendation generation with recent-watch de-duplication, and history tracking.

## Agent ownership matrix

| Agent | Models used | Streams subscribe/publish | Postgres tables | Qdrant collections |
|---|---|---|---|---|
| home_automation | NPU vision (`yolov8n`), iGPU LLM (`qwen3:8b`/`qwen3.6`) | sub: `events.home`; pub: `events.home`, `notify.outbound`, `memory.updates` | existing PR2 tables (`alerts`, etc.) | `capabilities` (via orchestrator registry) |
| personal_assistant | iGPU LLM (`qwen3:8b` default) | sub: `events.system`, `events.home`; pub: `notify.outbound`, `events.system` | `reminders`, `renewals`, `appointments` | none |
| system_health | host metrics + optional ROCm CLI | sub: `events.system`; pub: `events.system`, `notify.outbound` | none | none |
| storage_backup | iGPU reasoner capability (`qwen3.6`) for suggestions | sub: `events.system`; pub: `events.system`, `notify.outbound` | none | none |
| knowledge_notes | Embedder (NPU-first `bge-m3`), iGPU LLM (`qwen3:8b`) | sub: `memory.updates`; pub: `memory.updates`, `notify.outbound` | `indexed_documents` | `notes` |
| household_ops | iGPU reasoner capability (`qwen3.6`) | sub: `events.home`; pub: `events.system`, `notify.outbound` | `chores`, `shopping_list`, `pantry`, `meal_plans` | none |
| entertainment | Embedder (NPU-first), iGPU LLM (`qwen3:8b`) | sub: `events.home`; pub: `notify.outbound`, `events.system` | `media_history` | `media_metadata` |

## Out of scope for PR 3

PR 3 intentionally does **not** add proactive scheduling, quiet-hours controls, rate limiting, or severity-based bypass behavior. Those are deferred to PR 4, which will wire scheduled jobs and outbound notification policies.
