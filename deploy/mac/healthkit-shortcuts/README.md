# HealthKit (free, no paid app) → Home Intelligence bridge

A free alternative to `../healthkit-bridge/` that does **not** require
the paid "Health Auto Export — JSON+CSV" iOS app.

This bridge uses **macOS Shortcuts** (built into macOS Monterey+) to read
your HealthKit data on the Mac directly. The Health app on macOS Sonoma 14+
syncs from your iPhone via iCloud, so anything visible in the Mac Health
app — steps, sleep, heart rate, workouts, weight — is available to a
Shortcut, and from there to a small Python forwarder that POSTs to the
TrueNAS orchestrator on your LAN.

```
iPhone Health  ──iCloud sync──▶  Mac Health app
                                    │
                       launchd ▶ shortcuts run "HI Health Snapshot"
                                    │
                                    │  emits JSON to stdout
                                    ▼
                              forwarder.py
                                    │
                                    ▼  HTTP POST + X-Health-Token
                          TrueNAS orchestrator
```

No Xcode, no developer signing, no recurring App Store cost.

## Requirements

- macOS Monterey 12 or newer (Sonoma 14+ recommended — earlier versions
  have less complete HealthKit-on-Mac sync from iPhone)
- iCloud signed in with the same Apple ID as your iPhone
- The Mac Health app set up — open Health.app once, accept the welcome
  flow, and verify your iPhone data appears
- Same `HEALTHKIT_WEBHOOK_TOKEN` set in TrueNAS as you'll paste into the
  installer here (see the parent `../healthkit-bridge/README.md` step 2
  for how to set it on TrueNAS)

## One-time setup

### 1. Build the "HI Health Snapshot" Shortcut

Open the **Shortcuts** app on your Mac → File → New Shortcut. Rename it
**`HI Health Snapshot`** (the bridge looks for this exact name).

Add the actions below in order. For each "Find Health Samples" action,
configure as listed — anything you don't see in the app is optional and
can be left as the default.

> **Tip**: the "Type" dropdown in "Find Health Samples" is filterable —
> just type the metric name and pick from the suggestions. macOS's
> internal names match HealthKit's; e.g. "Active Energy" maps to
> `HKQuantityTypeIdentifierActiveEnergyBurned`.

#### Action 1–8: Pull each metric

For each row in the table below, add **one** "Find Health Samples"
action, configure as shown, then add a "Get Numbers from Input" or "Get
Details of Health Samples" action right after it (depends on what aggregation
you want). Save each result into a variable with the name in the **Variable**
column — you'll reference these in the final JSON dictionary.

| #  | Type                     | Date filter                | Aggregation | Variable           |
|----|--------------------------|----------------------------|-------------|--------------------|
| 1  | Step Count               | Date is in the last 1 hour | Sum         | `Steps`            |
| 2  | Active Energy            | Date is in the last 1 hour | Sum         | `ActiveEnergy`     |
| 3  | Heart Rate               | Date is in the last 1 hour | Average     | `HeartRate`        |
| 4  | Resting Heart Rate       | Date is in the last 24 hours | Most Recent | `RestingHR`      |
| 5  | Heart Rate Variability   | Date is in the last 24 hours | Most Recent | `HRV`            |
| 6  | Body Mass                | Date is in the last 7 days | Most Recent | `Weight`          |
| 7  | Oxygen Saturation        | Date is in the last 24 hours | Most Recent | `BloodO2`        |
| 8  | Sleep Analysis           | Date is in the last 24 hours | Sum *(of asleep stages)* | `SleepAsleepMin` |

> If the action returns "no results", that variable will be empty — the
> forwarder skips empty values, so missing metrics simply aren't sent.
> This is fine for the first run.

#### Action 9: Format the timestamp

Add **"Current Date"** → then **"Format Date"** with format
`yyyy-MM-dd'T'HH:mm:ss'Z'` and timezone **UTC**. Save as `Now`.

#### Action 10: Build the JSON dictionary

Add a **"Dictionary"** action. Add the following keys (only include the
ones you set up above — leave others out):

- `ts`               → Magic Variable: `Now`
- `window_min`       → Number `60`
- `steps`            → Magic Variable: `Steps`
- `active_energy`    → Magic Variable: `ActiveEnergy`
- `heart_rate`       → Magic Variable: `HeartRate`
- `resting_heart_rate` → Magic Variable: `RestingHR`
- `hrv`              → Magic Variable: `HRV`
- `weight`           → Magic Variable: `Weight`
- `blood_oxygen`     → Magic Variable: `BloodO2`
- `sleep_asleep_min` → Magic Variable: `SleepAsleepMin`

#### Action 11: Output

Add **"Stop and Output"** → output the dictionary, format **"JSON"**.
This is what the launchd-invoked CLI captures and pipes into `forwarder.py`.

### 2. Test the Shortcut from a terminal

```sh
shortcuts run "HI Health Snapshot"
```

You should see a JSON object printed:

```json
{"ts":"2026-05-14T08:00:00Z","window_min":60,"steps":234,"active_energy":12.4, ...}
```

If it prints `{}` or is missing fields, double-check each "Find Health
Samples" action's Type and the Aggregation step. The Mac Health app must
also have data for that metric (open Health.app and verify visually).

### 3. Test the forwarder end-to-end

Pipe the Shortcut output into the forwarder manually:

```sh
ORCHESTRATOR_URL=http://truenas.local:8080 \
HEALTHKIT_TOKEN=<your token> \
  shortcuts run "HI Health Snapshot" | /usr/bin/python3 ./forwarder.py
```

Expected output (in `~/Library/Logs/healthkit-shortcuts.log`):

```
... INFO uploaded 6 metrics + 0 workouts (812 bytes) → {"ok":true,"inserted":6,...}
```

### 4. Install the launchd schedule

```sh
./install.sh
```

The installer asks for the orchestrator URL, the token, the Shortcut name
(default `HI Health Snapshot`), and the polling interval in minutes
(default 15). It writes a LaunchAgent to
`~/Library/LaunchAgents/com.home-intelligence.healthkit-shortcuts.plist`
and starts it.

### 5. Verify

Wait for the next interval, then:

```sh
tail -f ~/Library/Logs/healthkit-shortcuts.log
```

You should see a new "uploaded N metrics" line each interval. On TrueNAS
you can also confirm:

```sh
curl http://localhost:8080/admin/healthkit/recent?metric=steps
```

## Including workouts (optional)

The Shortcuts "Find Workouts" action returns a list of workouts. Building
the per-workout JSON is more involved than the simple metrics above —
each workout needs `start`, `end`, `duration_min`, and optionally
`active_energy` and `distance_m`. To add workout support:

1. Add **"Find Workouts"** with date filter "Last 1 hour".
2. Add **"Repeat with Each"** loop over the result.
3. Inside the loop, build a Dictionary per workout with the keys above,
   and append it to a list variable `WorkoutList`.
4. Add `workouts` → `WorkoutList` to the final dictionary in step 10.

The forwarder accepts the workouts list and the orchestrator's normalizer
will store them as workout rows.

If you'd rather skip workouts for now, simply omit the `workouts` key —
the forwarder tolerates missing fields.

## Sleep nuances

The simple "Sleep Analysis → Sum" works for "how many minutes was I
asleep in the last 24h?". For richer sleep tracking (in-bed window,
deep/REM breakdown), edit the Shortcut later to compute:

- `sleep_asleep_min` → sum of "Sleep Analysis = Asleep" samples
- `sleep_window`     → a sub-dictionary with `start`, `end`, `asleep_min`

The orchestrator's normalizer will pick up the window and attach correct
`startDate`/`endDate` for sleep aggregation.

## Manual one-shot

Useful when debugging:

```sh
ORCHESTRATOR_URL=http://truenas.local:8080 \
HEALTHKIT_TOKEN=<your token> \
  ./run.sh
```

## Uninstall

```sh
./uninstall.sh
```

This removes the LaunchAgent only. The `HI Health Snapshot` Shortcut and
your local logs stay in place — open the Shortcuts app to delete the
shortcut manually if you wish.

## Troubleshooting

- **`shortcuts: command not found`** — you're on a macOS older than 12.
  Upgrade to Monterey or newer.
- **Shortcut prints `{}` with no values** — open Health.app on the Mac.
  If your iPhone data isn't visible there, the Mac Health app hasn't
  finished its initial iCloud sync. Give it a few minutes after sign-in.
- **`HTTP 401 invalid X-Health-Token`** — the Mac and TrueNAS tokens
  don't match. Re-run `./install.sh` and paste the same value as on
  TrueNAS.
- **`HTTP 503 HEALTHKIT_WEBHOOK_TOKEN is not configured`** — TrueNAS
  doesn't have the env var set. See `../healthkit-bridge/README.md`
  step 2.
- **No log entries appearing** — `launchctl print
  gui/$(id -u)/com.home-intelligence.healthkit-shortcuts` shows the
  agent's state. If `state = not loaded`, re-run `./install.sh`.
- **Shortcut prompts for permission every run** — open System Settings →
  Privacy & Security → Health → enable "Shortcuts". One-time grant.

## How this differs from the paid `healthkit-bridge`

| Aspect            | `healthkit-bridge` (paid)             | `healthkit-shortcuts` (this) |
|-------------------|----------------------------------------|------------------------------|
| iOS app required  | Health Auto Export (~$5)               | None                         |
| Mac requirement   | Just iCloud Drive                      | Mac Health app (Sonoma+)     |
| Data freshness    | Whatever cadence iOS app exports       | Every 15 min (configurable)  |
| Coverage          | Anything Health Auto Export supports   | Whatever the Shortcut reads  |
| Setup complexity  | Lower (iOS app picks metrics)          | Higher (build Shortcut once) |
| Recurring cost    | App Store IAP                          | $0                           |

You can run **both** bridges side-by-side — they POST to the same
endpoint and the orchestrator dedupes by `(metric, ts, member_id)`.
