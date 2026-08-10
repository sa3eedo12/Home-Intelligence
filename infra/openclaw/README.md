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
  `0700 _apt`. You should never need `apt` (see
  [Browser / Playwright](#browser--playwright)), but if you do, write
  `/etc/apt/apt.conf.d/99-local` with `APT::Sandbox::User "root";`,
  `Dir::Cache::archives "/tmp/aptcache";` and
  `Dir::State::lists "/tmp/aptlists";` (both pre-created `777`).
- Root also lacks `CAP_KILL`, so it cannot signal node-owned processes. Kill as
  `node`, and always by explicit PID.
- Scripts must be **piped in** (`cat f | docker exec -i ... sh -s`) rather than
  written under `/home/node` and executed as root.
- If the gateway is crash-looping, `docker exec` is unavailable. Recover by
  mounting the config volume in a throwaway container:
  `docker run --rm --user root -v /mnt/Pool1/Docker/OpenClaw/config:/cfg alpine/openclaw:<tag>`

## Browser / Playwright

**Everything needed for browser automation is already in place. No `apt`, no
sidecar container.** Measured 2026-08-10.

- All Chromium system libraries **are baked into the image** (Debian 12
  bookworm): libnss3, libgbm, libatk, libcups, libdrm, libxkbcommon, libpango,
  libasound, libatspi. The earlier claim that they live in the overlay and are
  lost on update was wrong.
- `PLAYWRIGHT_BROWSERS_PATH=/home/node/.cache/ms-playwright` sits inside the
  `/mnt/Pool1/Docker/OpenClaw/config -> /home/node` bind mount, so browser
  downloads **already survive app updates**.
- `infra/openclaw/install-browser-deps.sh` was **deleted** — it existed to
  reinstall those libraries after every update, which was never necessary.

Install/repair the browser with Playwright itself, as `node`:

```sh
docker exec -u node ix-openclaw-openclaw-1 npx playwright install chromium
```

### Diagnosing

`openclaw browser doctor` is the fastest signal and **does work from inside the
container** (no device pairing needed — pairing only applies to the remote
Control UI):

```sh
docker exec -u node ix-openclaw-openclaw-1 openclaw browser doctor
```

Healthy output is five `OK` lines. `FAIL browser: not running` on a cold
gateway is normal — Chrome is launched on demand by the first browser call.

> **Never trust the agent's prose to confirm the browser works.** When the
> browser tool fails, the model silently falls back to `fetch`/`curl` and still
> returns a plausible answer. Verify with the failure-count delta or an
> artifact:
> ```sh
> grep -c "browser failed" /tmp/openclaw/openclaw-$(date +%F).log
> ```
> Run it before and after; the delta must be `0`. A saved screenshot under
> `~/.openclaw/media/browser/*.png` is proof the real browser ran.

### Failure mode: corrupt profile → Chrome exits before binding CDP

Symptom in `/tmp/openclaw/openclaw-*.log`:

```
[tools] browser failed: Chrome CDP websocket for profile "openclaw" is not
reachable after start. CDP diagnostic: http_unreachable after 2ms;
cdp=http://127.0.0.1:30273; fetch failed: connect ECONNREFUSED 127.0.0.1:30273
```

Chrome starts, prints `DevTools listening on ws://127.0.0.1:30273`, then exits
**without writing `DevToolsActivePort`** and without a crash message.

Root cause is an accumulated/corrupted profile under
`.openclaw/browser/openclaw/user-data`, not the binary, the libraries, or the
flags. Proven by isolation: identical full flag set failed on the 13 MB
inherited profile and succeeded on a fresh directory on the same ZFS mount.

Fix — reset the profile (Chrome recreates it):

```sh
docker exec -u node ix-openclaw-openclaw-1 openclaw browser reset-profile
```

**This is not a timeout problem — do not raise the timeouts.** On a healthy
profile CDP is ready in **219–256 ms**, against defaults of
`localCdpReadyTimeoutMs: 8000` and `localLaunchTimeoutMs: 15000` (~30× headroom).

### Do not pin `browser.executablePath`

It was pinned to `chromium-1234`, but the bundled Playwright resolves
`chromium-1223`; a version-pinned path breaks on every browser update. Leave it
unset and let Playwright resolve. Current config is just:

```json
"browser": { "enabled": true, "headless": true, "noSandbox": true }
```

`chrome-headless-shell` and full `chrome` both work; `headless: true` resolves
to full Chrome with `--headless=new`. Chrome's dbus/bluez/Floss errors and the
`GLib-GIO-CRITICAL` assertion are harmless container noise.

A remote CDP browser is supported (`browser.cdpUrl` + `attachOnly`) but is
**not needed here** and adds a container, a network hop, and SSRF surface.

## Session store

Transcripts live in `~/.openclaw/agents/main/sessions/`, three files per
session (`<uuid>.jsonl`, `.trajectory.jsonl`, `.trajectory-path.json`), with
`sessions.json` as a key -> `sessionId` index.

**There is no `sessions delete` command** (`cleanup`, `compact`,
`export-trajectory`, `list`, `tail` only), so pruning means editing the index
and removing files. Two traps:

1. **`sessions.json` does not list every real session.** Telegram history
   rollovers and each cron *run* get their own files under keys like
   `agent:main:cron:<id>:run:<uuid>` that never appear in the index. Deleting
   "anything not in the index" destroys real history — in one prune that was
   9 live sessions. Decide ownership by reading the `sessionKey` values
   *inside* each `.jsonl` instead.
2. **Stop the gateway first**, or it rewrites `sessions.json` from memory and
   restores what you removed. Since `docker exec` then dies with it, edit
   through a throwaway container that has full capabilities:

   ```sh
   docker stop ix-openclaw-openclaw-1
   docker run --rm -v /mnt/Pool1/Docker/OpenClaw/config:/cfg \
     -v /tmp/prune.py:/p.py python:3-alpine python /p.py --apply
   docker start ix-openclaw-openclaw-1
   ```

Restore ownership after writing (`chown 1000:1000`, `chmod 600`) — a root-owned
`sessions.json` is unreadable to the `node` user the gateway runs as.

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

Warm calls used to request `keep_alive` of 3x the check interval. That was
wrong, and it caused a real regression. `keep_alive` is **per-request and
last-write-wins**: sending one *overrides* the server's `OLLAMA_KEEP_ALIVE`
for that model. This deployment sets 24h, so the warm loop was silently
cutting residency to 15 minutes and forcing a 9.9 GB reload every ~20 min.

The loop now sends no `keep_alive` at all and instead reads `expires_at` from
`/api/ps`, re-warming anything **approaching** eviction (within
`MODEL_WARM_INTERVAL_SECONDS * 2`) rather than only what is already gone. The
original reasoning — "a warm must outlast the gap between checks" — was a valid
*floor* but was being applied as a *ceiling*. Set `MODEL_WARM_KEEP_ALIVE` only
to deliberately override server policy; it is unset by default. The reasoner
still honours `REASONER_KEEP_ALIVE=-1` to pin it permanently, since a pin is a
residency decision the operator is making on purpose.

The same anti-pattern had leaked into feature code: `health_goals` and
`engagement` passed `keep_alive=60` on **reasoner** calls and `goals_chat`
passed `keep_alive=120` on the planner. Because both resolve to the same 24 GB
MoE here, every background run rescheduled its eviction for a minute later and
left the next real request to pay a ~3 min reload — a likely contributor to the
intermittent agent timeouts. `orchestrator/tests/test_model_residency_policy.py`
now fails the build if a short `keep_alive` reappears, in either the kwarg or
the payload-dict spelling. `reflector` is exempt: its `keep_alive=0` is a
deliberate unload to free VRAM before the 35B loads.

#### Measured residency behaviour

Verified against the live server rather than assumed:

| model | TTL from a request that sends no `keep_alive` |
|---|---|
| `qwen36-moe-64k` | pinned (year 2318) via `REASONER_KEEP_ALIVE=-1` |
| `bge-m3`, `qwen3-0.6b-4k` | 86,400s — the `OLLAMA_KEEP_ALIVE=24h` default |
| `qwen3-8b-16k` | only 600–900s |

The env var *is* parsed (`server config` logs `OLLAMA_KEEP_ALIVE:24h0m0s`) and
*is* applied to most models, so the 8B's short TTL is Ollama's own scheduling,
not our code — a raw `curl` that bypasses the SDK entirely reproduces it, and
an explicit `"keep_alive":"24h"` on the same model is honoured in full. All
four model slots are occupied (`OLLAMA_MAX_LOADED_MODELS=4`), which is the
likely trigger.

This is not worth "fixing" by hardcoding a keep-alive back into the warmer:
the expiry-aware loop already refreshes the 8B every cycle, well before it
could evict, and observation over a full warm cycle confirms it never leaves
`/api/ps`. Reacting to observed expiry is what makes the loop robust to a
server-side default we do not control.

Untested lever, if cold-start failures ever need a belt-and-braces fix:
`runTimeoutMs` returns 0 (disabled) when `>= 2147e6` ms (~24.85 days). Note
that `agents.defaults.timeoutSeconds` is capped at 120s by
`clampImplicitTimeoutMs`, so it can only ever *shorten* the timeout.

## The system prompt is ~27.5k tokens

Measured, not estimated: a bare `openclaw agent --message "Reply with exactly:
ok"` in a fresh session sends **27,500 prompt tokens** and takes **108s** of
prefill. Attribution, by removing servers and re-measuring a cold session:

| source | tokens |
|---|---|
| OpenClaw system prompt + built-in tools + skills + workspace docs | ~23,400 |
| `home-assistant` MCP (27 tools) | ~4,100 |
| **total** | **~27,500** |

Consequences worth internalising before touching anything else:

1. **The 27.5k prefix is shared by every session**, so llama.cpp's KV cache
   normally serves it for free. A cold session measured **15-20s end to end**
   (including an MCP tool round-trip) while the prefix stayed cached.
2. **The 108s figure is the cache-*miss* cost.** It is what you pay when
   something evicts the shared prefix. That was the real failure mechanism:
   one session with a 54k-token transcript kept flushing the prefix, so
   *every* new session had to re-prefill 27.5k tokens and hit
   `cron: job execution timed out (last phase: model-call-started)`.
   Fixing the offending session fixes latency system-wide.
3. **The window must comfortably exceed 27.5k.** At 32k that left ~5k for
   actual conversation. The poisoned session's recorded `input` counts climb
   `28270 → 30044 → 32346 → 32760 → 32767` and stop dead at the ceiling —
   that flat-line at 32767 *is* the `stopReason=length` error. Even a *fresh*
   session is tight: the monitor job's single Amazon fetch adds ~5k tokens of
   HTML, taking turn one to **32,616 — 99.5% of a 32k window with zero
   history**. At 64k the same run sits at 50%.
4. **`--light-context` is not a lever for this.** Measured: 27,510 tokens with
   it vs 27,507 without. It trims conversation history, not the system prompt
   or tool schemas. Do not reach for it to fix cold-start cost.

### Why there is no fallback model

`qwen3-8b-16k` was configured as OpenClaw's fallback. It cannot work, for two
independent reasons:

- Its **16k window cannot hold a 27.5k prompt**, so every failover failed
  immediately with `stopReason=length`.
- Raising it to 32k does not help: the dense 8B prefills at **161 tok/s**, so
  27.5k tokens needs **171s** — past the 120s timeout no matter the window.
  It also costs +5.1 GB (vs +0.7 GB to double the MoE's window).

Counter-intuitively the 23 GB MoE is the *fast* model here. Measured on a
10,684-token prompt:

| model | prefill | prefill t/s | gen t/s |
|---|---|---|---|
| `qwen36-moe-64k` (35B-A3B) | 21.1s | 506 | 27 |
| `qwen3-8b-16k` (dense 8B) | 66.2s | 161 | 13 |

A 35B MoE activates only ~3B params per token; the dense 8B activates all 8B.
On a bandwidth-bound iGPU, **active** parameters set the speed, not total size.
So the big model is both smarter and ~3x faster — do not "optimise" by
switching to the small one. The 8B is still the right choice for the
orchestrator's short classification prompts, where prefill depth is trivial.

### Do not extrapolate that table — prefill degrades with depth

The 506 t/s above is real *at 10.7k tokens* and meaningless as a constant.
Measured on unique (cache-defeating) prompts against the live 64k model:

| prompt | prefill | rate |
|---|---|---|
| 10,684 tok | 21.1s | 506 t/s |
| 27,578 tok | 100.0s | 276 t/s |
| 65,535 tok | **431.1s** | 152 t/s |

2.4x the tokens costs 4.3x the time. Attention is quadratic in sequence
length, so the *rate itself* falls as the prompt deepens. The practical
consequence: **filling the current 64k window costs over seven minutes of
prefill on a cache miss.**

### Why a bigger context window is not the free win it looks like

The VRAM cost of more context is genuinely trivial on this model. It is a
hybrid: only **10 of its 41 layers** keep a KV cache, the rest use recurrent
state that is constant-size regardless of context.

```
llama_kv_cache:       size = 1280.00 MiB (65536 cells, 10 layers, 1/1 seqs)
llama_memory_recurrent: size =   62.81 MiB (    1 cells, 41 layers, 1 seqs)
```

That is 20 KB/token, so 64k -> 128k costs only ~1.25 GiB and the full native
262144 window ~3.75 GiB, against ~14 GB of headroom. **VRAM is not the
constraint, and freeing the 8B's 9.9 GB is not required to raise the window.**

The constraint is the table above — but only on a **cache miss**. Measured
against the live model, prompt-cache reuse does work, and matters enormously:

| request | prompt | prefill |
|---|---|---|
| unique 50k prompt (cold) | 50,019 tok | **269.2s** |
| same + 12 more tokens appended | 50,031 tok | **18.6s** |
| same + 12 more again | 50,042 tok | **18.8s** |
| byte-identical 65k prompt re-sent | 65,535 tok | **0.1s** |

So llama.cpp's `forcing full prompt re-processing due to lack of cache data
(likely due to SWA or hybrid/recurrent memory)` fires when the prefix
*diverges*, **not** on every turn. A normal conversation only appends, which is
a pure prefix extension and hits the cache.

The catch is that a cache hit is not free on this architecture: extending a 50k
context still costs **~18.6s per turn** for a 12-token delta, because the
recurrent state cannot be partially reused. That cost scales with context
depth, so **every turn of a long session pays for its length**, even cached.
Raising the window to 128k would raise both the per-turn floor and the
worst-case miss. 64k is already past the point where a full re-prefill fits in
a sane timeout — the right lever is bounding session growth, not enlarging the
window.

### Declare `contextTokens` — OpenClaw cannot infer it from Ollama

**This caused a real outage of the browser workflow.** With no `contextTokens`
on the model entries, OpenClaw assumed **200,000** tokens while the Ollama
model is built with `num_ctx 65536`. Two things follow, both bad:

- Ollama **silently truncates** at `num_ctx` rather than erroring. Measured: a
  ~96k-token prompt came back as `prompt_eval_count=65535`. Nothing warns you.
- `toolResultMaxChars` **auto-derives from the context window**
  (`resolveAutoLiveToolResultMaxChars(contextWindowTokens)`), so the 3x
  overestimate also tripled the budget allowed for a single tool result.

A single `browser` screenshot + page snapshot then pushed one session to
**111,253 tokens**. It was truncated to 64k, missed the cache, and needed a
full ~430s re-prefill — well past the model timeout, so the turn died with
`LLM request timed out`.

Fixed by declaring the real windows and letting compaction act mid-tool-loop:

```jsonc
// models.providers.ollama.models[]
{ "id": "qwen36-moe-64k", "contextTokens": 65536 }
{ "id": "qwen3-8b-16k",   "contextTokens": 16384 }

// agents.defaults.compaction
"midTurnPrecheck": { "enabled": true }   // default false
```

`midTurnPrecheck` is what catches an oversized tool result *after it is
appended but before the next model call* — turn-end compaction is too late
when one tool result blows the window on its own.

Verified on the same request that previously failed: **111,253 -> 36,560
tokens**, and a timeout became a **164s** success. Confirm the models report
their real windows with `openclaw models list` (expect `64k` and `16k`, not
`195k`).

### Why the 8B stays, despite the MoE being faster

Two measured reasons, neither of them quality:

1. **The MoE serialises; the 8B does not.** The MoE loads with `n_seq_max = 1`
   (hybrid recurrent memory supports one sequence), the 8B gets two slots. A
   short call issued 1s into a long generation:

   | | alone | during a long request |
   |---|---|---|
   | `qwen3-8b-16k` | 0.37s | 0.36s — unaffected |
   | `qwen36-moe-64k` | 0.56s | **10.47s** — queued |

   Routing classifiers, the dashboard and the doorbell at the MoE would park
   them behind every multi-minute OpenClaw turn.

2. **The MoE ignores `format: "json"`.** Grammar-constrained decoding is not
   applied on the `RENDERER/PARSER qwen3.5` path, so it emits markdown-fenced
   JSON with `think:false` and a stray `</think>` with `think:true`. The 8B
   honours the constraint. Only `goals_chat` strips fences today, so every
   other JSON call site would break.

Both are fixable — the second with a shared fence-tolerant parser — but until
then the 8B earns its 9.9 GB.

## Cron jobs must not share your chat session

A cron job's `sessionTarget` defaults to the session it was created from. An
hourly job created from Telegram therefore appended its tool output — ~10 KB of
fetched HTML per run — into the **user's own chat transcript**. That session
reached 190 KB (77% of it tool results) and ~54k tokens against a 32k window,
which broke normal conversation as well as the job.

Give recurring jobs an isolated session. Delivery still announces to the
channel, so notifications are unaffected; only the transcript is separated.

### Editing a cron job when the CLI is locked out

`openclaw cron edit` needs `operator.admin`, and the containerised CLI cannot
approve its own scope-upgrade request (`gateway closed (1008): pairing
required`) — only a paired device holding `operator.approvals` can, e.g. the
iPhone app. When that is not available, edit the store directly:

```bash
docker stop ix-openclaw-openclaw-1
# cron jobs live in the `cron_jobs` table of
# .openclaw/state/openclaw.sqlite — note `store_key` still points at a
# jobs.json that no longer exists; SQLite is the source of truth.
# Patch BOTH the flat columns and the `job_json` blob, then:
docker start ix-openclaw-openclaw-1
```

Relevant columns: `session_target` (`isolated` | `session:<key>`),
`session_key`, `payload_timeout_seconds`, `payload_light_context`.
Verify with `openclaw cron list` — that is a read and needs no upgrade.

## Editing config: the gateway owns the file

The running gateway rewrites `~/.openclaw/openclaw.json` from its in-memory
state, so **editing the file under a live gateway silently loses the change**.
Either use the `openclaw` CLI, or edit and restart immediately. Also note the
config is schema-validated on boot: an unknown key (e.g. adding one under
`meta`) makes the gateway refuse to start and crash-loop with
`Invalid config ... meta: Invalid input`. Fix by removing the key from the host
path (`/mnt/Pool1/Docker/OpenClaw/config/.openclaw/`) with a root container.

## Compaction: `maxActiveTranscriptBytes` is gated

Sessions grow unbounded because auto-compaction triggers near the **context
limit**, and an undeclared model context is assumed to be very large (200k as
measured — see [Declare `contextTokens`](#declare-contexttokens--openclaw-cannot-infer-it-from-ollama)).
The hardware cannot process a 55k-token prompt inside the timeout, so the
session deadlocks long before OpenClaw thinks it is full. A declared context
window far larger than what the hardware can actually chew through is actively
harmful — declare `contextTokens` so this trigger uses the real number.

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
  "keepRecentTokens": 8000,
  "midTurnPrecheck": { "enabled": true }
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

## Reaching the Control UI (secure-context problem)

The Control UI is served by the gateway itself on `:30262` (`/`, `/control`,
`/app`). Browsing to it via the NAS LAN IP fails with:

> Secure browser context required — this page is running over plain HTTP, so
> the browser cannot create the device identity the Gateway expects.

The UI needs WebCrypto, which browsers only expose in a *secure context*. Plain
HTTP over a LAN IP is not one. The config knob
`gateway.controlUi.allowInsecureAuth: true` sidesteps this by dropping to
token-only auth — **don't**; it removes device identity for everyone on the LAN.

Use an SSH tunnel instead. `127.0.0.1` is a "potentially trustworthy origin"
per the Secure Contexts spec, so it *is* a secure context even over plain HTTP,
and device auth keeps working:

```bash
ssh -f -N -L 18789:127.0.0.1:30262 truenas
# then open http://127.0.0.1:18789  (auth with OPENCLAW_GATEWAY_TOKEN)
```

**TrueNAS blocks this by default.** Its sshd ships `AllowTcpForwarding no`, so
the tunnel accepts the local connection then dies with
`Recv failure: Connection reset by peer`. Enable it persistently through the
middleware, not by editing `sshd_config` (which gets regenerated):

```bash
midclt call ssh.update '{"tcpfwd": true}'   # UI: Services -> SSH -> Allow TCP Port Forwarding
```

## Device scopes and the CLI bootstrap deadlock

Admin commands (`cron edit`, `cron run`, `devices approve`) need
`operator.admin`. A freshly paired CLI only gets `operator.write`, and it
**cannot approve its own upgrade request** — that needs `operator.pairing`,
which is what it is asking for. Only a device already holding
`operator.approvals` (e.g. the iOS app) can break the tie.

If no such device is reachable, grant scopes offline. Pairing state is JSON,
not just SQLite:

```bash
docker stop ix-openclaw-openclaw-1
# edit .openclaw/devices/paired.json: add the scopes to BOTH
#   scopes[] and approvedScopes[], and to tokens.operator.scopes[]
# then clear .openclaw/devices/pending.json
docker start ix-openclaw-openclaw-1
```

Note `openclaw.json` holds `channels.telegram.botToken` in plaintext, and the
gateway token is the `OPENCLAW_GATEWAY_TOKEN` env var (set from the TrueNAS app
UI, not stored in the config file). Treat both files as secrets.

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
