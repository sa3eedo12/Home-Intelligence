# Deploy on TrueNAS SCALE (Custom Apps)

This is the deployment path for **TrueNAS SCALE Goldeye (25.10) or newer**, which uses a Docker-based app engine. It also works on Fangtooth (25.04+) but Goldeye is the tested baseline.

> ⚠️ TrueNAS SCALE Electric Eel (24.10) shipped Docker support but it was opt-in.
> TrueNAS SCALE Dragonfish (24.04) and earlier used the (now-removed) `k3s`-based app engine
> and are **not supported** by this guide. Upgrade your TrueNAS host first.

This deployment uses the GHCR-published Home-Intelligence images. The
dashboard is exposed on the TrueNAS host's LAN at port `8080` and runs
without authentication (single-user, LAN-only).

## Hardware acceleration on TrueNAS today

| Component | Status on TrueNAS 25.10 (Goldeye) | Why |
|---|---|---|
| **Radeon 890M (ROCm iGPU)** | ✅ Works | TrueNAS ships `amdgpu` + `amdkfd`. Containers share `/dev/kfd` and `/dev/dri/*`. |
| **XDNA 2 NPU** | ❌ Not available | TrueNAS 25.10's kernel build doesn't enable `CONFIG_DRM_ACCEL_AMDXDNA`, so `/dev/accel/accel0` never appears. |

The default `deploy/docker-compose.truenas.yml` therefore:
- runs Ollama on the iGPU with full ROCm passthrough,
- replaces the AMD Lemonade NPU image with a tiny CPU stub that
  satisfies the `/health` probe so the rest of the stack stays healthy,
- routes the "small" models (router + embeddings) to Ollama on the
  iGPU instead of the NPU (`ROUTER_MODEL=qwen3:0.6b`,
  `EMBED_MODEL=bge-m3` in `.env.truenas.example`).

When iX Systems eventually enables `amdxdna` in a TrueNAS kernel
build, `system_health.xdna_status` (see below) will start reporting
`available` and you can switch back to the NPU-enabled Lemonade
service — see the [Future: NPU-enabled variant](#future-npu-enabled-variant)
appendix at the end of this doc.

## Confirmed target

- Minisforum N5 Pro (AMD Ryzen AI 9 HX370 + Radeon 890M + XDNA 2 NPU + 96 GB RAM)
- TrueNAS SCALE 25.10 (Goldeye) or newer
- Docker-based Apps engine

## Prerequisites

- TrueNAS admin (root) shell access (Web Shell or SSH).
- A storage pool you're happy to put app data on. The example uses `tank`.
- Public-pull access to the Home-Intelligence images on GHCR (run the
  "Make GHCR packages public" workflow once, as documented in
  [DEPLOY-MINISCLOUD.md](DEPLOY-MINISCLOUD.md)).

## Step 1 — Verify GPU is visible to the host

> ⚠️ **Do not isolate the GPU** in System Settings → Advanced → GPU.
> That setting binds the GPU to `vfio-pci` for VM passthrough and would
> stop *every* container (yours or anyone else's) from using it. Apps
> share the GPU through Docker `devices:` + `group_add:`, not through
> isolation.

In a Web Shell:

```bash
ls /dev/kfd /dev/dri
lsmod | grep -E "amdgpu|amdkfd"
```

You should see `/dev/kfd`, `/dev/dri/card0`, `/dev/dri/renderD128`,
and the `amdgpu` module loaded with all its DRM helpers.

If you also want to confirm the NPU's status (it will be missing on
current TrueNAS):

```bash
ls /dev/accel/accel0 2>/dev/null && echo "NPU available" || echo "NPU not available (expected on TrueNAS 25.10)"
```

The Home-Intelligence stack will share the GPU with any other Apps
that also use it (Plex hardware transcoding, Jellyfin, Frigate,
Immich ML, stable-diffusion, etc.). The driver multiplexes them; the
only real constraint is **VRAM contention** — the iGPU's video memory
is shared, so tune `OLLAMA_MAX_LOADED_MODELS` and `OLLAMA_KEEP_ALIVE`
(see Step 3) so Ollama doesn't starve your other GPU-using apps.

> ℹ️ If you actually want to dedicate the GPU to a VM, use TrueNAS's
> isolation setting — but then you cannot run this stack on the host.
> Pick one or the other.

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

### Sharing the iGPU with other Apps

If you already run other GPU-using containers (Plex, Jellyfin, Frigate,
Immich ML, stable-diffusion, etc.), tune Ollama's footprint so it
doesn't starve them. Add or override these in the env file:

```env
# Cap how much Ollama keeps resident on the iGPU at once:
OLLAMA_MAX_LOADED_MODELS=1     # default in compose is 2
OLLAMA_NUM_PARALLEL=1          # default is 2
OLLAMA_KEEP_ALIVE=5m           # default is 30m; eject idle models sooner
```

The amdkfd driver multiplexes work across containers, so simultaneous
use is fine — only VRAM is the bottleneck. If you have a discrete GPU
in addition to the 890M, set `HIP_VISIBLE_DEVICES` on the `ollama`
service to pin it to the iGPU and leave the dGPU for other Apps.

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
| `lemonade` | UID 568 (apps) | CPU stub on TrueNAS; no host devices needed. |
| `orchestrator` and most agents | UID 568 | Pure HTTP services, no privileged needs. |
| `system_health` | root + `pid: host` | Needs `/proc` and `/sys` from the host. |
| `storage_backup` | root | Needs to read TrueNAS pool datasets owned by various users. |

## Limitations on TrueNAS

- **The XDNA 2 NPU is unusable on TrueNAS 25.10.** TrueNAS's kernel
  build doesn't enable `CONFIG_DRM_ACCEL_AMDXDNA`, so the device
  driver never appears and `/dev/accel/accel0` doesn't exist. The
  default compose file replaces the AMD Lemonade image with a CPU
  stub. `system_health.xdna_status` will report `not_present` — when
  iX flips this kernel config in a future build, the same capability
  will report `available` and you can switch to the NPU-enabled
  variant. There's no workaround on the read-only TrueNAS root
  filesystem; out-of-tree driver builds get wiped on every TrueNAS
  update.
- The TrueNAS apps engine does **not** expose `/var/run/docker.sock` to
  apps. `system_health.container_status` therefore reports
  `docker socket unavailable` and `restart_container` returns an error.
  All other `system_health` capabilities (`scan`, `top_processes`,
  `gpu_status`, `xdna_status`, `anomaly_check`) work normally via
  `/proc` + `/sys` + `rocm-smi`.
- The dashboard has **no authentication**. Don't expose port `8080`
  outside your LAN. Put a reverse proxy with auth in front of it if you
  need remote access.

## Troubleshooting

### `Mount path is invalid` on deploy
- `TRUENAS_APPDATA` is not the real absolute host path, or
- one of the required source files (e.g. `infra/redis/redis.conf`) does
  not exist yet.

Run the `mkdir -p` and `cp` commands from Step 2 again, then retry.

### `ollama` container says "no GPU detected"
- The host doesn't expose `/dev/kfd` or `/dev/dri/*` — check
  `lsmod | grep amdgpu` and `dmesg | grep -i amdgpu` for module
  load failures.
- The host kernel doesn't load `amdgpu` / ROCm modules.
- The `HSA_OVERRIDE_GFX_VERSION` is wrong for your silicon — for
  Radeon 890M (gfx1150) the file uses `11.5.1`; fall back to `11.0.0`
  by editing the compose file and redeploying if Ollama still can't
  see the device.
- Numeric `group_add` GIDs in the compose file (`44`, `107`) don't
  match your host's `video` and `render` groups. Check with
  `getent group video render` on the host and adjust if needed.
- You isolated the GPU for VM passthrough — that takes the GPU away
  from all containers. Un-isolate it in
  System Settings → Advanced → GPU.

### `lemonade` container fails to start
- The default TrueNAS compose runs the CPU stub; it should never need
  hardware. If it fails, check **Apps → Logs** for a Python traceback.
- If you switched to the NPU-enabled variant manually, see the
  appendix below.

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

---

## Future: NPU-enabled variant

When iX Systems enables `CONFIG_DRM_ACCEL_AMDXDNA` in a future TrueNAS
kernel build, `system_health.xdna_status` will start reporting
`available`. To take advantage of it:

1. Install the AMD NPU firmware on the host
   (`apt install linux-firmware` if iX hasn't already shipped it; check
   `/lib/firmware/amdnpu/`).
2. Replace the `lemonade` service in `deploy/docker-compose.truenas.yml`
   with this NPU-enabled block:

   ```yaml
     lemonade:
       image: amd/lemonade-server:latest
       restart: unless-stopped
       env_file: .env
       devices:
         - /dev/accel/accel0
       group_add:
         - "107"  # render — verify with `getent group render` on host
       volumes:
         - ${TRUENAS_APPDATA}/infra/lemonade/models:/models:ro
         - ${TRUENAS_APPDATA}/infra/lemonade/server.yaml:/etc/lemonade/server.yaml:ro
       environment:
         LEMONADE_MODELS_DIR: /models
         LEMONADE_HOST: 0.0.0.0
         LEMONADE_PORT: "8000"
       healthcheck:
         test: ["CMD-SHELL", "curl -f http://localhost:8000/health"]
         interval: 10s
         timeout: 5s
         retries: 6
       networks: [agents]
   ```

3. Update `.env.truenas.example` (or your `.env`) to use the
   NPU-quantized small models:

   ```env
   ROUTER_MODEL=qwen3-1.7b-int4
   EMBED_MODEL=bge-m3-int8
   ```

4. Prepare the NPU model files under
   `$TRUENAS_APPDATA/infra/lemonade/models/` per AMD Lemonade docs.
5. Apps → Edit → paste updated YAML → Apply.

The dashboard's curator will start narrating activity that hits the
NPU, and `system_health.xdna_status` will continue to report `available`.
