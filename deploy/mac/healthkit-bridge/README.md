# Health Auto Export → Home Intelligence bridge (Mac)

A tiny launchd-managed Python script that watches an iCloud Drive folder for
Health Auto Export JSON files and forwards them to the orchestrator on TrueNAS
over the LAN. Useful when you don't want to expose the TrueNAS app to the
public internet but you do want HealthKit data to flow even when your phone is
away from home.

## How it works

```
iPhone (Health Auto Export)
   │
   │  exports JSON to iCloud Drive on a schedule
   ▼
iCloud Drive  ─sync→  Mac (always on local network)
                          │
                          │  every minute, launchd runs bridge.py:
                          │  POST file → http://truenas.local:8080/admin/healthkit/sync
                          ▼
                     TrueNAS orchestrator (LAN-only)
```

The phone never talks directly to TrueNAS. The Mac stays on your LAN and
forwards as soon as new files appear.

## Setup

### 1. iPhone — Health Auto Export

Install **Health Auto Export — JSON+CSV** from the App Store. Create an
automation:

- Type: **JSON file export**
- Destination: **iCloud Drive** → folder `HealthAutoExport`
- Aggregation: hourly (or whatever cadence you want)
- Metrics: sleep, steps, weight, heart rate, workouts, energy, mood

The app will write one JSON file per export to
`iCloud Drive / HealthAutoExport / *.json`.

### 2. TrueNAS — set the webhook token

If you haven't already, generate a token and set it on TrueNAS:

```sh
TOKEN=$(openssl rand -hex 32)
echo "HEALTHKIT_WEBHOOK_TOKEN=$TOKEN" | sudo tee -a /mnt/Pool1/Docker/.env
sudo docker compose --env-file /mnt/Pool1/Docker/.env -p home-intelligence \
  up -d --force-recreate orchestrator
```

Save `$TOKEN` somewhere — you'll paste it into the Mac installer in the next
step.

### 3. Mac — install the bridge

From this folder:

```sh
./install.sh
```

The installer will prompt for:

- The orchestrator URL (default `http://truenas.local:8080`)
- The HealthKit token (paste the value from step 2)
- The folder to watch (default is the iCloud Drive `HealthAutoExport` folder)
- Optional `member_id` to attribute uploads to a household member

It writes a launchd plist at
`~/Library/LaunchAgents/com.home-intelligence.healthkit-bridge.plist` and
starts it. The agent runs every 60 seconds.

### 4. Verify

Trigger an export from the iPhone (or just wait for the next scheduled one),
then watch the log:

```sh
tail -f ~/Library/Logs/healthkit-bridge.log
```

You should see lines like:

```
... INFO uploaded HealthAutoExport-2026-05-13.json (12345 bytes) in 187 ms: {"ok":true,...}
```

Successfully uploaded files are moved to `<watch-dir>/processed/`. Permanently
rejected files (bad JSON, 4xx errors) are moved to `<watch-dir>/failed/` with
the HTTP status appended to the filename. Network and 5xx errors leave the
file in place so the next minute's run retries.

## Manual one-shot

To run the bridge once without launchd (handy for debugging):

```sh
ORCHESTRATOR_URL=http://truenas.local:8080 \
HEALTHKIT_TOKEN=<your token> \
WATCH_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/HealthAutoExport" \
  /usr/bin/python3 ./bridge.py
```

## Uninstall

```sh
./uninstall.sh
```

## Troubleshooting

- **"WATCH_DIR is not a directory"** — make sure iCloud Drive is enabled and
  the `HealthAutoExport` folder has been created (it's created the first time
  the iOS app exports to iCloud).
- **`network error … leaving for retry`** — the Mac can't reach TrueNAS.
  Check `ping truenas.local` and that the orchestrator is up on port 8080.
- **HTTP 401 `invalid X-Health-Token`** — the Mac and TrueNAS tokens don't
  match. Re-run `./install.sh` and paste the same value as on TrueNAS.
- **HTTP 503 `HEALTHKIT_WEBHOOK_TOKEN is not configured`** — TrueNAS doesn't
  have the env var set. Re-do step 2.
- **Files pile up in WATCH_DIR but never get processed** — `launchctl print
  gui/$(id -u)/com.home-intelligence.healthkit-bridge` shows the agent state.
  If it says "not loaded", re-run `./install.sh`.
- **iCloud "stub" files (zero bytes / `.icloud` extension)** — iCloud Drive
  hasn't downloaded the file yet. macOS will pull it on demand the first time
  it's accessed; the bridge skips dotfiles, and JSON loads fail safely so the
  next minute's run picks them up.
