# iOS HealthKit Shortcut → Home Intelligence (free, no paid app)

A free alternative to `../mac/healthkit-bridge/` (which requires the paid
"Health Auto Export — JSON+CSV" iOS app). This option uses an **iOS
Shortcut** that reads HealthKit directly on your iPhone and POSTs JSON
to the orchestrator over your home WiFi.

```
iPhone (Health data lives here natively)
   │
   ├─ Personal Automation: every hour at :00
   │  └─▶ Run Shortcut "HI Health Snapshot"
   │
   │       1. Read recent samples (steps, HR, sleep, ...) from HealthKit
   │       2. Build JSON dictionary
   │       3. POST → http://192.168.1.190:8080/admin/healthkit/sync
   │                  with header X-Health-Token: <your token>
   ▼
TrueNAS orchestrator (LAN-only)
```

No Mac required. No third-party app. No App Store purchase. Works as
long as your iPhone is on the same WiFi as TrueNAS when the automation
fires (usually most of the day).

## Why iOS Shortcuts (and not macOS Shortcuts)

Despite occasional confusion, **the Health app has never existed on
macOS** — Apple has only released it for iOS and iPadOS. Without
Health.app, macOS Shortcuts has no way to surface "Find Health Samples"
or any other HealthKit action. iOS is the only place where Shortcuts
can read HealthKit directly without writing a signed app.

## Requirements

- iOS 15.2 or newer (for "Run Immediately" on time-based automations,
  which avoids a tap-to-confirm prompt every hour)
- iPhone joining the same WiFi as TrueNAS most of the day
- `HEALTHKIT_WEBHOOK_TOKEN` set in TrueNAS — see
  [`../../mac/healthkit-bridge/README.md` step 2](../../mac/healthkit-bridge/README.md)
  for how to generate and install it

## Setup

### 1. Build the Shortcut on your iPhone

Open **Shortcuts** on your iPhone → **+** → **New Shortcut**. Rename it
**`HI Health Snapshot`**.

Add the actions below in order. Each "Find Health Samples" action lives
under **+ → Apps → Health → Find Health Samples**.

#### Actions 1–8: pull each metric

| #  | Type                     | Date filter                  | Aggregation          | Variable name      |
|----|--------------------------|------------------------------|----------------------|--------------------|
| 1  | Step Count               | Date is in the last 1 hour   | Sum                  | `Steps`            |
| 2  | Active Energy            | Date is in the last 1 hour   | Sum                  | `ActiveEnergy`     |
| 3  | Heart Rate               | Date is in the last 1 hour   | Average              | `HeartRate`        |
| 4  | Resting Heart Rate       | Date is in the last 24 hours | Most Recent          | `RestingHR`        |
| 5  | Heart Rate Variability   | Date is in the last 24 hours | Most Recent          | `HRV`              |
| 6  | Body Mass                | Date is in the last 7 days   | Most Recent          | `Weight`           |
| 7  | Oxygen Saturation        | Date is in the last 24 hours | Most Recent          | `BloodO2`          |
| 8  | Sleep Analysis           | Date is in the last 24 hours | Sum (asleep stages)  | `SleepAsleepMin`   |

> Tap the result of each "Find Health Samples", choose **Variable Name**,
> and type the name from the **Variable** column. You'll reference these
> in the Dictionary action below.
>
> If a metric doesn't exist on your device (e.g., no HRV from your watch),
> just skip that action — the orchestrator handles partial payloads.

#### Action 9: timestamp

Add **Date → Current Date**, then **Date → Format Date** with format
`yyyy-MM-dd'T'HH:mm:ss'Z'` and timezone **UTC**. Save its output as
variable `Now`.

#### Action 10: build the JSON envelope

Add a **Dictionary** action. The orchestrator expects the Health Auto
Export shape — `data.metrics` is an array of `{type, units, data}`
where `type` is the HealthKit identifier. You build it with nested
dictionaries.

The easiest way to keep this readable is one outer dictionary called
`Body` with this structure:

```
Body = {
  data: {
    metrics: [
      {type: "HKQuantityTypeIdentifierStepCount",
       units: "steps",
       data: [{date: <Now>, qty: <Steps>}]},
      {type: "HKQuantityTypeIdentifierActiveEnergyBurned",
       units: "kcal",
       data: [{date: <Now>, qty: <ActiveEnergy>}]},
      {type: "HKQuantityTypeIdentifierHeartRate",
       units: "bpm",
       data: [{date: <Now>, qty: <HeartRate>}]},
      {type: "HKQuantityTypeIdentifierRestingHeartRate",
       units: "bpm",
       data: [{date: <Now>, qty: <RestingHR>}]},
      {type: "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
       units: "ms",
       data: [{date: <Now>, qty: <HRV>}]},
      {type: "HKQuantityTypeIdentifierBodyMass",
       units: "kg",
       data: [{date: <Now>, qty: <Weight>}]},
      {type: "HKQuantityTypeIdentifierOxygenSaturation",
       units: "%",
       data: [{date: <Now>, qty: <BloodO2>}]},
      {type: "HKCategoryTypeIdentifierSleepAnalysis",
       data: [{startDate: <Now>, endDate: <Now>,
               stage: "asleep", qty: <SleepAsleepMin>}]}
    ]
  }
}
```

In the Dictionary action UI, this means:

1. Top-level dictionary with **one** key: `data` → Dictionary
2. Inside `data`, one key: `metrics` → List
3. Inside `metrics`, **one Dictionary per metric** with keys
   `type` (Text, hard-coded HK identifier),
   `units` (Text, the unit),
   and `data` (List with one Dictionary containing `date` (Magic
   Variable: `Now`) and `qty` (Magic Variable: the metric variable))

It's tedious to enter the first time but you can copy-paste a Dictionary
within Shortcuts and just edit the type/unit/variable, which makes the
remaining 7 metrics fast.

#### Action 11: POST to the orchestrator

Add **Web → Get Contents of URL**:

- **URL**: `http://192.168.1.190:8080/admin/healthkit/sync`
  (replace with your TrueNAS host/IP)
- **Method**: `POST`
- **Headers**:
  - `Content-Type` → `application/json`
  - `X-Health-Token` → *paste the same token you set on TrueNAS*
- **Request Body**: **JSON** → Magic Variable: `Body`

#### Action 12 (optional): show or log the response

Add **Show Notification** with the contents of "URL Contents" so you can
see "ok: true, inserted: 8" the first few runs. Once it's working you
can delete this action or change it to silent.

### 2. Test the Shortcut once manually

Tap the Run button in the Shortcut editor. You should see a
notification like `{"ok":true,"inserted":8,"skipped":0,"latest":{...}}`.

If you see `401 invalid X-Health-Token` — the token doesn't match
TrueNAS. If you see `503 HEALTHKIT_WEBHOOK_TOKEN is not configured` —
the env var isn't set on TrueNAS yet (see step 2 of
`../../mac/healthkit-bridge/README.md`). If the request times out —
your iPhone isn't on the same network as TrueNAS, or the orchestrator
isn't running.

### 3. Schedule it as a Personal Automation

Open Shortcuts → **Automation** tab → **+** → **Create Personal
Automation**:

- Trigger: **Time of Day**, e.g. **Every hour at :00**, or every 4 hours
- Action: **Run Shortcut** → pick `HI Health Snapshot`
- Toggle **Run Immediately** **ON** (iOS 15.2+ — skips the confirmation
  prompt)
- Toggle **Notify When Run** **OFF** (so you don't get notified each
  time it runs)
- Save.

The automation will fire on schedule from now on, even when the phone
is locked, as long as the phone is connected to a network. If the phone
is off home WiFi when the automation fires, the request fails silently
and the next scheduled run will succeed.

### 4. Verify on TrueNAS

```sh
curl http://localhost:8080/admin/healthkit/recent?metric=steps
```

You should see your most recent steps samples. After a few hours you
can also check:

```sh
curl http://localhost:8080/admin/healthkit/aggregate?metric=steps&days=1
```

## Adding workouts later

The simplest "Find Workouts" → "Repeat with Each" loop builds a
`workouts` list inside the dictionary. Each workout entry needs `type`,
`start`, `end`, `duration_min`, and optionally `active_energy` and
`distance_m`. Add to the outer body as:

```
data.workouts = [{type: "Walking", start: ..., end: ..., duration_min: 28}]
```

## Troubleshooting

- **"Find Health Samples" action is greyed out** — the Shortcuts app
  needs Health permission. Settings → Privacy & Security → Health →
  Shortcuts → enable everything you want exposed.
- **`{}` or empty values** — your phone doesn't have data for that
  metric in the requested window (e.g., no recent HRV). The orchestrator
  silently drops missing metrics, so this is fine.
- **Automation fires but nothing reaches TrueNAS** — confirm the iPhone
  is on the same WiFi (open Safari and visit `http://192.168.1.190:8080/dashboard`).
  Apple sometimes pauses background automations to save power; running
  the Shortcut manually once a day re-validates the schedule.
- **403 / 401 from TrueNAS** — `HEALTHKIT_WEBHOOK_TOKEN` mismatch.
  Re-paste the same token in both places.

## Comparison: this vs `../../mac/healthkit-bridge/`

| Aspect              | This (free iOS Shortcut)              | `mac/healthkit-bridge/` (paid)        |
|---------------------|----------------------------------------|----------------------------------------|
| iOS app cost        | **$0**                                 | ~$5 ("Health Auto Export — JSON+CSV") |
| Data path           | iPhone → TrueNAS (direct)              | iPhone → iCloud → Mac → TrueNAS       |
| Mac required        | No                                     | Yes (always-on Mac on LAN)             |
| Off-home behavior   | Skips that run, retries next           | iCloud queues files until Mac sees them|
| Setup complexity    | Higher (build Shortcut once, ~15 min)  | Lower (install iOS app, ~3 min)        |
| Coverage            | Whatever you put in the Shortcut       | Whatever the iOS app exports           |

You can run **both** simultaneously — the orchestrator dedupes by
`(metric, timestamp, member_id)`.
