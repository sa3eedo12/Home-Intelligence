# Home-Intelligence

## 1) What this is

Home-Intelligence is a local-first, multi-agent foundation for home and personal automation on Minisforum N5 Pro hardware. This PR delivers infrastructure only (compose stack, shared SDK skeletons, and docs) so orchestrator and agent implementations can land cleanly in follow-up PRs.

## 2) Hardware

Target host: **Minisforum N5 Pro** (AMD Ryzen AI 9 HX370 + Radeon 890M iGPU + XDNA 2 NPU + 96 GB RAM).

- **NPU (XDNA 2):** router LLM, embeddings, STT, vision
- **iGPU (Radeon 890M via ROCm):** chat/reasoning LLMs in Ollama
- **CPU (Zen5):** orchestration, APIs, scheduling, glue logic

## 3) Architecture diagram

```text
                    +-----------------------+
Telegram / HA  ---> |  Orchestrator (PR 2)  |
                    +-----------+-----------+
                                |
                      Redis Streams / Tasks
                                |
       +------------------------+------------------------+
       |                        |                        |
+------+-------+        +-------+------+         +-------+------+
| Agents (PR2/3)|       | Shared SDK    |         | Memory Layer |
| tool APIs     |       | bus/memory/llm|         | Redis/PG/Qdr |
+--------------+        +--------------+          +-------------+
                                |
                  +-------------+----------------+
                  |                              |
           +------+-------+               +------+------+
           | Ollama ROCm  |               | Lemonade NPU|
           | qwen3 models |               | int4/int8   |
           +--------------+               +-------------+
```

## 4) Status: PR 1 of 4 — foundation only

- [x] **PR 1 (this PR):** infra stack, compose overlays, shared SDK skeleton, docs
- [x] **PR 2 (this PR):** orchestrator, Telegram bot, first agent, capability registry bootstrap
- [x] **PR 3:** remaining agents and inter-agent workflows
- [ ] **PR 4:** proactive scheduling, quiet hours, policy/rate limits

## 5) Setup

```bash
git clone <repo-url>
cd Home-Intelligence
cp .env.example .env
# fill .env placeholders with your local values
```

**Security note:** never commit `.env`. If any token is exposed, rotate it immediately.

```bash
make pull-models
make prepare-npu-models
make up
```

`prepare-npu-models` runs host-side placeholder scripts and requires AMD Ryzen AI tooling (`lemonade-sdk`, `amd-quark`) on the host.

## 6) Verifying the stack

When running with the dev overlay (`make dev`), verify endpoints:

```bash
curl http://localhost:11434/api/version
curl http://localhost:8000/health
curl http://localhost:6333/healthz
```

## 7) Compute fabric

| Engine | Models / Workloads | Why |
|---|---|---|
| NPU (XDNA 2) | `qwen3-1.7b-int4`, `bge-m3-int8`, `distil-whisper-small.en-int8`, `yolov8n-int8` | Low-latency INT inference, offload non-chat workloads |
| iGPU (ROCm 890M) | `qwen3:8b` (default), `qwen3.6:35b-a3b` (reasoner), optional `qwen2.5vl:7b` | Better local throughput for larger chat/reasoning models |
| CPU (Zen5) | orchestration/runtime glue/scheduling | Deterministic control plane and service integration |

## 8) Troubleshooting

- **gfx1150 detection issues:** in `docker-compose.rocm.yml`, `HSA_OVERRIDE_GFX_VERSION=11.5.1` is set for Radeon 890M; fallback to `11.0.0` if needed.
- **NPU permissions:** ensure `/dev/accel/accel0` exists and your runtime can access group `render`.
- **Model conversion:** conversion scripts are placeholders; adapt exact commands to current Lemonade + AMD Quark documentation.

## 9) Try each agent

- Home Automation: "turn off the living room lights"
- Personal Assistant: "Remind me to renew my car insurance on Dec 15"
- System Health: "How is the system?"
- Storage & Backup: "Find duplicates in /mnt/media"
- Knowledge & Notes: "Search my notes for the air-fryer manual"
- Household Operations: "What's on the shopping list?"
- Entertainment: "Recommend a movie for tonight"
