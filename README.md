# Home-Intelligence

A local, multi-agent home intelligence system for the **Minisforum N5 Pro**
(AMD Ryzen AI 9 HX370 + Radeon 890M iGPU + XDNA 2 NPU + 96 GB RAM).

> **Status:** Repository bootstrap. The real scaffolding lands in
> **PR 1 — Foundation**, followed by:
>
> - PR 2 — Orchestrator + Telegram bot + Home Automation agent
> - PR 3 — Remaining 7 agents
> - PR 4 — Proactive scheduling, quiet hours, policies

## Goals

- 8 cooperating agents for home, personal, system, and entertainment domains.
- Single Telegram surface (text **and** voice notes) owned by the orchestrator.
- Fully local inference — no cloud LLM, embedding, or STT calls.
- Three-tier compute fabric: **NPU** (router / embeddings / STT / vision),
  **iGPU** (chat + reasoning LLMs via Ollama+ROCm), **CPU** (orchestration glue).
- Survives reboots and individual service crashes.

See PR 1 for full architecture, folder layout, and setup instructions.
