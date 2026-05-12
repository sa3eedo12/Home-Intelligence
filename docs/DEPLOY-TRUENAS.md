# Deploy on TrueNAS SCALE (Custom Apps)

This is the deployment path for **TrueNAS SCALE Fangtooth (25.04) or newer**, which uses a Docker-based app engine.

> ⚠️ TrueNAS SCALE Electric Eel (24.10) shipped Docker support but it was opt-in.
> TrueNAS SCALE Dragonfish (24.04) and earlier used the (now-removed) `k3s`-based app engine
> and are **not supported** by this guide. Upgrade your TrueNAS host first.

This deployment uses the GHCR-published Home-Intelligence images and
inline ROCm + NPU device passthrough so the iGPU and XDNA 2 NPU on a
Minisforum N5 Pro are actually used by the agent stack.

The dashboard is exposed on the TrueNAS host's LAN at port `8080` and
runs without authentication (single-user, LAN-only).

## Confirmed target

- Minisforum N5 Pro (AMD Ryzen AI 9 HX370 + Radeon 890M + XDNA 2 NPU + 96 GB RAM)
- TrueNAS SCALE 25.04 (Fangtooth) or 25.10 (Goldeye)
- Docker-based Apps engine

## Prerequisites

- TrueNAS admin (root) shell access (Web Shell or SSH).
- A storage pool you're happy to put app data on. The example uses `tank`.
- A GPU pool isolated for app use (`System Settings → Advanced → GPU → Isolated GPU Devices`).
- Public-pull access to the Home-Intelligence images on GHCR (run the
  "Make GHCR packages public" workflow once, as documented in
  [DEPLOY-MINISCLOUD.md](DEPLOY-MINISCLOUD.md)).

## Step 1 — Isolate the GPU for apps

The Radeon 890M needs to be released from the host kernel before the
Ollama ROCm container can claim `/dev/kfd` and `/dev/dri`.

1. Open the TrueNAS UI.
2. Go to **System Settings → Advanced**.
3. Under **GPU Configuration → Isolated GPU PCI IDs**, select the AMD GPU.
4. Apply, then reboot when prompted.
5. After reboot, confirm the GPU is no longer attached to the host:
   ```bash
   ls /dev/kfd /dev/dri
   ```
   Both should exist; if `/dev/kfd` is missing, the kernel ROCm modules
   may not be loaded — install them via `apt` inside a debug shell or
   move to a TrueNAS build that ships them by default.

> ℹ️ The XDNA 2 NPU (`/dev/accel/accel0`) does not require an isolation
> step on TrueNAS; it just needs the `amdxdna` kernel module, which
> ships with recent TrueNAS SCALE kernels. If `ls /dev/accel/accel0`
> fails, this guide cannot enable the NPU and you should use the
> CPU-only [MinisCloud deployment](DEPLOY-MINISCLOUD.md) instead.

## Step 2 — Create the dataset layout

In **Datasets**, create a dataset under your pool (this guide uses
`tank/AppData/HomeIntelligence`). Then, from a host shell, create the
folder structure used by the bind mounts:

```bash
APPDATA=/mnt/tank/AppData/HomeIntelligence
mkdir -p \
  "$APPDATA/infra/redis" \
  "$APPDATA/infra/qdrant" \
  "$APPDATA/infra/postgres/init" \
  "$APPDATA/infra/lemonade/models" \
  "$APPDATA/infra/storage_backup" \
  "$APPDATA/data/notes" \
  "$APPDATA/data/media"
```

Now copy the repo's stock infra files into that path (clone the repo
once into `/tmp` for this):

```bash
git clone https://github.com/sa3eedo12/Home-Intelligence /tmp/hi
cp /tmp/hi/infra/redis/redis.conf "$APPDATA/infra/redis/"
cp /tmp/hi/infra/qdrant/config.yaml "$APPDATA/infra/qdrant/"
cp -r /tmp/hi/infra/postgres/init/. "$APPDATA/infra/postgres/init/"
cp /tmp/hi/infra/storage_backup/backup_targets.yaml "$APPDATA/infra/storage_backup/"
# Lemonade server config (placeholder — adapt to your AMD Lemonade build).
cp /tmp/hi/infra/lemonade/server.yaml "$APPDATA/infra/lemonade/" 2>/dev/null || true
```

Set ownership to the TrueNAS apps user (UID/GID 568):

```bash
chown -R 568:568 "$APPDATA"
# storage_backup needs to read pool datasets, which are owned by other
# users — that container intentionally runs as root inside (see compose).
```

## Step 3 — Prepare the env file

Copy `deploy/.env.truenas.example` from the repo to a host-side path
the Apps engine can read (anywhere, e.g. `/root/home-intelligence.env`
or `$APPDATA/.env`). Edit at least these values:

- `TRUENAS_APPDATA` — must be the absolute host path you created above
  (e.g. `/mnt/tank/AppData/HomeIntelligence`).
- `POSTGRES_PASSWORD` and the matching `DATABASE_URL`.
- `REDIS_PASSWORD` and the matching `REDIS_URL`.
- `TELEGRAM_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, `TELEGRAM_CHAT_ID`.
- `HA_TOKEN` (Home Assistant long-lived access token).

If `TRUENAS_APPDATA` is wrong, app deployment fails with a generic
`Mount path is invalid` error.

## Step 4 — Deploy via the Apps GUI

1. **Apps → Discover Apps → Custom App**.
2. Name the app `home-intelligence`.
3. Paste the contents of `deploy/docker-compose.truenas.yml`.
4. Point **Environment File** at the `.env` you created in Step 3.
5. Save and deploy.

On first run, Apps will pull:
- The 9 Home-Intelligence images from GHCR
  (orchestrator + 7 functional agents + dashboard_curator).
- 5 third-party images: `redis`, `postgres`, `qdrant`,
  `ollama/ollama:rocm`, `amd/lemonade-server`.

## Step 5 — Pull Ollama models

After the stack is healthy, open the Apps shell on the `ollama`
container and pull at least the default model:

```bash
ollama pull qwen3:8b
# Optional larger reasoner — needs ~22 GB VRAM-equivalent
ollama pull qwen3.6:35b-a3b
```

## Step 6 — Verify

From any LAN client:

```bash
curl http://<truenas-ip>:8080/health
```

Then open the live dashboard in a browser:

```
http://<truenas-ip>:8080/dashboard
```

You should see the agent grid with each agent tile, a connection status
of `live`, and within ~60 seconds the **Curator** card at the top will
populate with an LLM-narrated summary.

## Permissions matrix

| Service | Runs as | Why |
|---|---|---|
| `redis`, `postgres`, `qdrant` | UID 568 (apps) | Standard apps user; data dirs chowned. |
| `ollama` | root | Needs `/dev/kfd` + `/dev/dri` and ROCm device groups. |
| `lemonade` | root | Needs `/dev/accel/accel0`. |
| `orchestrator` and most agents | UID 568 | Pure HTTP services, no privileged needs. |
| `system_health` | root + `pid: host` | Needs `/proc` and `/sys` from the host. |
| `storage_backup` | root | Needs to read TrueNAS pool datasets owned by various users. |

## Limitations on TrueNAS

- The TrueNAS apps engine does **not** expose `/var/run/docker.sock` to
  apps. `system_health.container_status` therefore reports
  `docker socket unavailable` and `restart_container` returns an error.
  All other `system_health` capabilities (`scan`, `top_processes`,
  `gpu_status`, `anomaly_check`) work normally via `/proc` + `/sys` +
  `rocm-smi`.
- The dashboard has **no authentication**. Don't expose port `8080`
  outside your LAN. Put a reverse proxy with auth in front of it if you
  need remote access.
- The Lemonade NPU image tag in this compose file is a placeholder;
  AMD's `amd/lemonade-server` image name and tag may change. Confirm
  against the latest AMD Lemonade documentation before deploying.

## Troubleshooting

### `Mount path is invalid` on deploy
- `TRUENAS_APPDATA` is not the real absolute host path, or
- one of the required source files (e.g. `infra/redis/redis.conf`) does
  not exist yet.

Run the `mkdir -p` and `cp` commands from Step 2 again, then retry.

### `ollama` container says "no GPU detected"
- The GPU is not isolated (Step 1).
- The host kernel doesn't load `amdgpu` / ROCm modules.
- The `HSA_OVERRIDE_GFX_VERSION` is wrong for your silicon — for
  Radeon 890M (gfx1150) the file uses `11.5.1`; fall back to `11.0.0`
  by editing the compose file and redeploying if Ollama still can't
  see the device.

### `lemonade` container fails to start
- `/dev/accel/accel0` is missing — confirm with `ls /dev/accel`.
- Your build of TrueNAS does not include the `amdxdna` kernel module.
  In that case, remove the `lemonade` `devices:` block and let
  Lemonade fall back to CPU; or switch to the
  [MinisCloud deployment](DEPLOY-MINISCLOUD.md) which uses a placeholder
  Lemonade stub.

### Dashboard shows `connecting…` indefinitely
- The orchestrator container is not running; check **Apps → Logs**.
- A reverse proxy in front of TrueNAS is buffering SSE responses;
  add `proxy_buffering off;` (nginx) or `disable_proxy_buffering=true`
  (Traefik).

### Curator narrative never appears
- The `dashboard_curator` agent depends on `ollama`. Check its logs in
  the Apps GUI; it falls back to a deterministic template if the LLM
  is down, but only after the first scheduled run (~60s after start).
- Confirm the orchestrator's scheduler is running:
  `curl http://<truenas-ip>:8080/status | jq '.jobs[] | select(.id|startswith("dashboard_"))'`.
