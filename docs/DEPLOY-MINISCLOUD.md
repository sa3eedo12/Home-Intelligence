# Deploy on Minis Cloud OS / nerdctl GUIs (no SSH)

This is the GUI-only deployment path for appliance-style Docker Compose interfaces that sit on top of `nerdctl` / `containerd`.

Confirmed working target:

- Minis Cloud OS / Siyouyun on Minisforum N5 Pro

It should also help on other image-only Compose GUIs that cannot run local `build:` steps.

## Prerequisites

- A file manager that can download from a URL or accept uploaded files
- A Docker Compose GUI / stack editor
- No SSH or host shell required

## Step 1: Download the repo zip

In the file manager, use **Download from URL** and fetch:

- `https://github.com/sa3eedo12/Home-Intelligence/archive/refs/heads/main.zip`

## Step 2: Extract into a host folder

Extract the archive into whichever host path your OS prefers.

Example host path:

- `/home/<user>/main/syspool/docker/HomeIntelligence`

After extraction, that folder should contain the repo files, including `infra/`, `deploy/`, `docs/`, `agents/`, and `orchestrator/`.

## Step 3: Manually create the missing data folders

The zip does not include empty runtime folders, so create these by hand in the file manager:

- `data/notes`
- `data/media`

## Step 4: Create and edit your env file

Copy `deploy/.env.miniscloud.example` to `deploy/.env`, then edit it in the file manager.

Set at least these values:

- `HOME_INTEL_DIR` — the absolute host path where this repo's `infra/` and `data/` folders live
- `TELEGRAM_TOKEN`
- `TELEGRAM_ALLOWED_USER_IDS`
- `TELEGRAM_CHAT_ID`
- `HA_TOKEN`
- `POSTGRES_PASSWORD` and matching `DATABASE_URL`
- `REDIS_PASSWORD` and matching `REDIS_URL`

`HOME_INTEL_DIR` is mandatory. If it is wrong, many Compose GUIs return only a generic `Mount path is invalid` error.

## Step 5: Create the stack in the Compose GUI

1. Create a new stack / project in the GUI.
2. Paste the contents of `deploy/docker-compose.miniscloud.yml` into the stack editor.
3. Point the stack's env file at `deploy/.env`.
4. Save and deploy.

This compose file uses only prebuilt images from GHCR. It does not use any `build:` blocks.

## Step 6: Deploy

On first run, the GUI will pull:

- 8 Home-Intelligence images from GHCR
- 5 third-party images (`redis`, `postgres`, `qdrant`, `ollama`, `python` for the Lemonade stub)

## Step 7: Pull Ollama models

Use the GUI's **exec into container** feature on the `ollama` container and run:

```bash
ollama pull qwen3:8b
```

If your GUI cannot exec into containers, temporarily expose port `11434` on the `ollama` service and POST to `/api/pull` instead.

## Required bind-mount source paths

These paths must exist on the host before deployment:

- `${HOME_INTEL_DIR}/infra/redis/redis.conf`
- `${HOME_INTEL_DIR}/infra/qdrant/config.yaml`
- `${HOME_INTEL_DIR}/infra/postgres/init/`
- `${HOME_INTEL_DIR}/infra/storage_backup/backup_targets.yaml`
- `${HOME_INTEL_DIR}/data/notes/`
- `${HOME_INTEL_DIR}/data/media/`

## Limitations

- No NPU acceleration on this path; Lemonade is a placeholder HTTP stub
- The ROCm overlay is not applied here
- `system_health` keeps `/proc` and `/sys`, but Docker-socket-based introspection is disabled on containerd hosts
- `storage_backup` only sees the repo-managed `data/media` and `data/notes` folders on this GUI path

## Troubleshooting

### "Mount path is invalid"

This usually means one of these is wrong:

- `HOME_INTEL_DIR` is not the real absolute host path
- A required source file or folder does not exist yet
- The GUI stack is pointing at the wrong env file

If the GUI error is too generic, isolate the missing path with a tiny test container:

1. Create a temporary stack or container in the GUI using image `alpine:3.20`.
2. Add exactly one bind mount, for example `${HOME_INTEL_DIR}/infra/redis/redis.conf:/tmp/redis.conf:ro`.
3. Start it.
4. Repeat with the next path until you find the missing source.

That is usually faster than debugging the full stack all at once.
