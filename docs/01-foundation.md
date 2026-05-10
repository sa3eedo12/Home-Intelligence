# Foundation Design Memo (PR 1)

This PR establishes the infrastructure baseline for a local-only home intelligence platform and intentionally avoids implementing orchestrator or agent business logic. The architecture is hub-and-spoke: agents will expose capability endpoints, and an orchestrator (PR 2) will route work and coordinate shared memory. This keeps routing policy centralized while allowing agents to remain modular and independently deployable.

Redis Streams is selected as the event bus because it offers durable append-only streams, consumer groups, replay semantics, and explicit acknowledgements (`xack`) for at-least-once delivery. That model fits asynchronous home workflows where temporary failures should not drop events.

The compute fabric is explicitly tiered:

- **NPU (XDNA 2)** for low-latency INT4/INT8 router, embedding, STT, and vision workloads.
- **iGPU (ROCm, Radeon 890M)** for heavier chat/reasoning LLM inference via Ollama.
- **CPU** for orchestration, protocol glue, scheduling, and deterministic rule logic.

Lemonade is used as the NPU-serving layer to provide a consistent HTTP API over host-converted ONNX artifacts. Model conversion scripts are intentionally placeholders in this PR because they depend on host-level SDKs and direct device access.

Qdrant is selected for semantic memory because it is lightweight, performant locally, and integrates well with Python clients. Collections are created lazily by agents to avoid premature schema coupling.

Subsequent PRs fill the runtime behavior:

- **PR 2:** orchestrator service, Telegram ingress, first agent implementation, registry bootstrap.
- **PR 3:** remaining domain agents and cross-agent workflows.
- **PR 4:** proactive scheduling, quiet hours, policy/rate-limit controls.
