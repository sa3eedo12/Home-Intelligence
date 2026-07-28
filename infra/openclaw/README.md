# OpenClaw on TrueNAS — operational notes

OpenClaw runs as a TrueNAS app (`ix-openclaw-openclaw-1`) and is the agent
runtime for Home-Intelligence. Config lives on the persistent volume at
`/mnt/Pool1/Docker/OpenClaw/config/.openclaw/openclaw.json`.

## Container constraints

The TrueNAS app template runs the container with `CapDrop=[ALL]` and
`no-new-privileges=true`. Consequences that bite repeatedly:

- Even `--user root` has no `CAP_DAC_OVERRIDE`, so root **cannot read
  `/home/node`** (mode 0700). Anything touching session/config files must run
  as the default `node` user.
- `apt` fails twice over: `setgroups` is blocked (needs
  `APT::Sandbox::User "root"`) and `/var/cache/apt/archives/partial` is
  `0700 _apt`. See `install-browser-deps.sh` for the working workaround.
- Scripts must be **piped in** (`cat f | docker exec -i ... sh -s`) rather than
  written under `/home/node` and executed as root.
- If the gateway is crash-looping, `docker exec` is unavailable. Recover by
  mounting the config volume in a throwaway container:
  `docker run --rm --user root -v /mnt/Pool1/Docker/OpenClaw/config:/cfg alpine/openclaw:<tag>`

## Browser / Playwright

Playwright's Chromium build ships in the image and lives on the persistent
mount, but its **system libraries do not** — they are installed into the
container overlay and are therefore **lost on every app update**.

Re-run after any update:

```sh
cat infra/openclaw/install-browser-deps.sh \
  | docker exec -i --user root ix-openclaw-openclaw-1 sh -s
```

The script is idempotent and verifies via `ldconfig` (it cannot run the browser
itself, because root cannot read node's home).

Test browser support through the **agent**, not the `openclaw browser` CLI —
the CLI needs gateway device pairing, the agent does not (it runs inside the
gateway).

## The 120s idle-timeout trap

Symptom: `LLM idle timeout (120s)`, then failover to the small model, which
overflows its context (`stopReason=length`) and hard-fails the turn.

**This fires on cold model loads and there is no config waiver for it.** The
35B MoE takes ~3 minutes to load into VRAM. Nothing streams during that time,
so the watchdog trips at 120s and kills the turn.

`resolveLlmIdleTimeoutMs` *looks* like it exempts local providers:

```js
const baseUrl = params?.model?.baseUrl;
if (typeof baseUrl === "string" && baseUrl.length > 0
    && isLocalProviderBaseUrl(baseUrl) && !isOllamaCloudModel(params?.model)) return 0;
return DEFAULT_LLM_IDLE_TIMEOUT_MS;
```

`isLocalProviderBaseUrl` does accept `localhost`, `0.0.0.0`, `::1`, `*.local`
and private IPv4 — but **this branch is dead in practice**: `params.model.baseUrl`
is not populated by the time the resolver runs, so the check never passes.

Setting a per-model `baseUrl` does **not** fix the timeout. This was tested
directly: the same cold-load request failed identically at 120s with
`http://ollama.local:11434` and with `http://172.16.4.7:11434`. Earlier runs
that appeared to prove the fix worked were confounded — **the model was already
warm**. The idle timeout measures the gap *between stream chunks*, not total
duration, so a warm request happily runs 124s without tripping it.

### The actual mitigation: keep models resident

Warmth is the whole game. The orchestrator runs a self-healing warm loop
(`_warm_models` in `orchestrator/app.py`) that polls Ollama's `/api/ps` every
`MODEL_WARM_INTERVAL_SECONDS` (default 300) and re-warms anything missing.
This matters because **Ollama drops every loaded model when it restarts**, and
the orchestrator does not restart with it — without the loop, that window stays
cold until the orchestrator happens to be redeployed, and every OpenClaw request
in the meantime hard-fails.

Warm calls request `keep_alive` of 3x the check interval so residency does not
depend on the two timers interleaving favourably. The reasoner additionally
honours `REASONER_KEEP_ALIVE=-1` to pin it permanently.

Untested lever, if cold-start failures ever need a belt-and-braces fix:
`runTimeoutMs` returns 0 (disabled) when `>= 2147e6` ms (~24.85 days). Note
that `agents.defaults.timeoutSeconds` is capped at 120s by
`clampImplicitTimeoutMs`, so it can only ever *shorten* the timeout.

## Compaction: `maxActiveTranscriptBytes` is gated

Sessions grow unbounded because auto-compaction triggers near the **context
limit**, and the primary model declares 128k. The hardware cannot process a
55k-token prompt inside the timeout, so the session deadlocks long before
OpenClaw thinks it is full. A declared context window far larger than what the
hardware can actually chew through is actively harmful.

The byte guard fixes this, but it is **silently ignored** unless the enabling
flag is set too:

```js
function resolveMaxActiveTranscriptBytes(cfg) {
  const compaction = cfg?.agents?.defaults?.compaction;
  if (compaction?.truncateAfterCompaction !== true) return;   // <-- gate
  ...
}
```

Required together:

```json
"compaction": {
  "truncateAfterCompaction": true,
  "maxActiveTranscriptBytes": 60000,
  "keepRecentTokens": 8000
}
```

Note this guard lives in the **auto-reply** path (`src/auto-reply/reply/
memory-flush.ts`), i.e. Telegram and other channels — not the `openclaw agent`
CLI path.

### Rescuing an already-bloated session

Compaction is chicken-and-egg: LLM summarisation must itself process the
oversized prompt, so it fails for exactly the sessions that need it.
`openclaw sessions compact <key> --max-lines N` truncates without the LLM, but
needs gateway device approval.

Failing that, edit the JSONL directly (as `node`, with a backup). The format
is: leading metadata lines (`session`, `model_change`, `thinking_level_change`,
`custom`) followed by `message` lines. Keep the metadata head plus a tail
starting at a clean turn boundary. A single `toolResult` line can be tens of
kilobytes — those are usually the whole problem.

## Config gotchas

- `idleTimeoutMs` is **not** valid under `agents.defaults.models.<model>`; it
  crash-loops the gateway at startup.
- Prefer `openclaw config set` (validates immediately) over hand-editing JSON.
  Note it has no `--type` flag; bare `true` is parsed as a boolean.
- Provider/model and channel changes need a restart. MCP changes hot-reload.
- Global flags like `--token` go **after** the subcommand for
  `sessions compact`, not before it.
- `openclaw agent` needs `--agent main`; use `--session-key agent:main:<name>`
  to force a fresh session.
- CLI output carries a persistent device-pair warning box; filter with
  `grep -vE "Config warning|device-pair|^│|^─|^◇|^├|^└|^╭|^╰"`.

## Skills vs MCP with local models

Skills expose only their one-line description in the system prompt and rely on
the model choosing to load the body. Qwen3 does not do this reliably — repeated
prompts failed with a `✓ ready` skill installed, while the same endpoints
exposed as **MCP tools** were used first try. Default to MCP here.
