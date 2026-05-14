# Native macOS HealthKit app → Home Intelligence

A small, signed macOS app that reads your HealthKit data on the Mac
(synced via iCloud from your iPhone) and POSTs it to the TrueNAS
orchestrator. Best option if you have an Apple Developer account and
Xcode installed.

```
iPhone Health  ──iCloud Health sync──▶  Mac (HealthKit framework)
                                            │
                            launchctl ▶ HomeIntelligenceHealth.app  (runs every 15 min)
                                            │
                                            ▼  POST + X-Health-Token
                                     TrueNAS orchestrator (LAN-only)
```

Why this exists: macOS has no Health *app*, but it does have the HealthKit
*framework* (since macOS 13). Apps with the `com.apple.developer.healthkit`
entitlement can read iCloud-synced Health data on the Mac without needing
the iPhone to be present.

## Requirements

- macOS 13 (Ventura) or newer (this repo built and tested on macOS 26)
- Xcode 15 or newer (tested with Xcode 26)
- An Apple Developer account (free Personal Team is enough for personal
  use; paid not required for HealthKit on Mac)
- iCloud signed in with the same Apple ID as your iPhone, with
  **Settings → [your name] → iCloud → Health** enabled on the iPhone
- `HEALTHKIT_WEBHOOK_TOKEN` set on TrueNAS — see step 2 in
  `../healthkit-bridge/README.md`

## File layout in this folder

```
healthkit-native/
├── README.md                                       ← this file
├── HomeIntelligenceHealth.xcodeproj/               ← pre-built Xcode project
├── Sources/HomeIntelligenceHealth/
│   ├── App.swift                                   ← entry point + flow control
│   ├── Config.swift                                ← parses env vars
│   ├── Authorization.swift                         ← HealthKit permission request
│   ├── HealthCollector.swift                       ← runs the HKHealthStore queries
│   ├── Snapshot.swift                              ← in-memory shape
│   ├── PayloadBuilder.swift                        ← builds Health Auto Export JSON
│   ├── Forwarder.swift                             ← URLSession POST
│   └── Log.swift                                   ← writes to ~/Library/Logs/...
├── Resources/
│   ├── Info.plist                                  ← bundle metadata + Health usage string
│   └── HomeIntelligenceHealth.entitlements         ← HealthKit + sandbox entitlements
├── build.sh                                        ← xcodebuild wrapper
├── install-launchd.sh                              ← installs the LaunchAgent
├── uninstall-launchd.sh
└── com.home-intelligence.healthkit-native.plist.template
```

## One-time setup

### 1. Set your Apple Developer team

Two options:

**Option A — Xcode GUI (recommended for first build)**

1. Open the project: `open HomeIntelligenceHealth.xcodeproj`
2. In the project navigator, select **HomeIntelligenceHealth** at the top.
3. Select the **HomeIntelligenceHealth** target → **Signing & Capabilities** tab.
4. Under **Team**, pick your team (paid Developer account or free
   Personal Team — either works for HealthKit on Mac with personal use).
5. Confirm **HealthKit** and **App Sandbox** capabilities are listed.
   (They should already be — they're carried by the entitlements file.)
6. Confirm under App Sandbox that **Network → Outgoing Connections (Client)**
   is checked so URLSession can reach TrueNAS.
7. **⌘S** to save. Xcode rewrites `project.pbxproj` with your team id —
   that change is local to your machine; you don't need to commit it.

**Option B — CLI**

Find your team id at https://developer.apple.com/account → Membership →
Team ID (10-char string). Then:

```sh
DEVELOPMENT_TEAM=ABC123XYZ4 ./build.sh
```

This bakes the team id into the build without modifying the project.

### 2. Build the app

```sh
./build.sh
```

The script invokes `xcodebuild`, which compiles Swift, links HealthKit,
and code-signs the bundle. Output:

```
Built: ./build/Build/Products/Release/HomeIntelligenceHealth.app
```

If Xcode complains about "Failed to register bundle identifier" — the
identifier `com.home-intelligence.healthkit-native` is already in use on
your team. Change the bundle id in **Target → General → Bundle
Identifier** to something unique (e.g. add your initials).

### 3. Install the app

Copy the bundle to a stable location:

```sh
mkdir -p ~/Applications
cp -R ./build/Build/Products/Release/HomeIntelligenceHealth.app ~/Applications/
```

### 4. First run — grant HealthKit permission

The first time the app runs, macOS shows a system prompt asking which
Health categories the app may read. Click **Turn All Categories On** →
**Allow**.

Run it once manually with a test config so you can answer the prompt:

```sh
ORCHESTRATOR_URL=http://truenas.local:8080 \
HEALTHKIT_TOKEN=<your token> \
WINDOW_MINUTES=60 \
  ~/Applications/HomeIntelligenceHealth.app/Contents/MacOS/HomeIntelligenceHealth
```

Expected log line in `~/Library/Logs/HomeIntelligenceHealth.log`:

```
... INFO orchestrator accepted: {"ok":true,"inserted":7,"skipped":0,...}
... INFO uploaded 7 metrics + 0 workouts (812 bytes)
```

If you see `HealthKit authorization failed` — the prompt didn't appear
or you denied it. Open **System Settings → Privacy & Security → Health
→ HomeIntelligenceHealth** and toggle the categories on, then re-run.

### 5. Schedule via launchd

```sh
./install-launchd.sh
```

The installer prompts for:
- App path (default `~/Applications/HomeIntelligenceHealth.app`)
- Orchestrator URL
- HealthKit token
- Window minutes (default 60 — must be ≥ interval)
- Interval minutes (default 15)
- Optional `member_id`

Writes the LaunchAgent to
`~/Library/LaunchAgents/com.home-intelligence.healthkit-native.plist`
and starts it. The app runs immediately and then every interval after.

### 6. Verify

```sh
tail -f ~/Library/Logs/HomeIntelligenceHealth.log
```

You should see a new "uploaded N metrics" line each interval. On TrueNAS:

```sh
curl http://localhost:8080/admin/healthkit/recent?metric=steps
```

## Re-building after pulling repo updates

```sh
git pull
./build.sh
cp -R ./build/Build/Products/Release/HomeIntelligenceHealth.app ~/Applications/
```

The next launchd interval picks up the new binary automatically — no
need to reload the LaunchAgent.

## Uninstall

```sh
./uninstall-launchd.sh
```

Then delete `~/Applications/HomeIntelligenceHealth.app` if you want to
remove the binary too. Toggle off in **System Settings → Privacy &
Security → Health** to revoke the HealthKit grant.

## Comparison

| Aspect              | This (native Mac app)                    | `../../ios/healthkit-shortcut/`   | `../healthkit-bridge/` (paid)         |
|---------------------|------------------------------------------|------------------------------------|----------------------------------------|
| Cost                | $0 (Personal Team) / $99/yr (paid Dev)   | $0                                | ~$5 (one-time iOS IAP)                |
| Apple Developer acc | **Required**                             | No                                 | No                                    |
| Xcode               | **Required**                             | No                                 | No                                    |
| iPhone needed live  | No (data syncs via iCloud)               | Yes (when automation fires)        | No (Mac uses iCloud-relayed files)    |
| Setup effort        | Medium (one-time, ~10 min after team)    | Medium (~15 min, build Shortcut)   | Low (~5 min)                          |
| Scheduling          | launchd (any interval)                   | iOS Personal Automation            | launchd (default 60s scan)            |
| Background reliable | High — pure macOS daemon                 | Medium — iOS may pause automations | High                                  |

You can run **any combination** of these — the orchestrator dedupes by
`(metric, timestamp, member_id)`.

## Troubleshooting

- **`HealthKit not available on this Mac`** — usually means the iCloud
  Health sync isn't enabled on your iPhone, or you're on macOS < 13.
- **`HealthKit authorization failed`** — first-run prompt was denied or
  dismissed. Open System Settings → Privacy & Security → Health →
  HomeIntelligenceHealth and re-enable.
- **`requires a provisioning profile`** during build — you haven't set
  a team in step 1.
- **`Code signing — Embedded provisioning profile not signed by Apple`**
  on launchd-invoked runs — the app was built with an Apple Development
  cert that's tied to a specific Mac. If you copied the .app from
  another Mac, rebuild on the target Mac so signing matches.
- **`network error: A server with the specified hostname could not be
  found.`** — Mac can't reach TrueNAS. Confirm `ping truenas.local`
  works and that the orchestrator is up.
- **`HTTP 401`** — `HEALTHKIT_TOKEN` mismatch. Re-run
  `./install-launchd.sh` and paste the same value as on TrueNAS.
- **No new log lines after install** — `launchctl print
  gui/$(id -u)/com.home-intelligence.healthkit-native` shows the
  agent's state and last exit status.

## How the .xcodeproj was built

For maintainers: the `HomeIntelligenceHealth.xcodeproj` was hand-crafted
(not generated by Xcode "File → New"). It uses stable UUIDs prefixed
`FAC...` for readability, and references source files relative to the
project's parent directory so this repo can update the Swift sources
without touching the project file.

The project carries the HealthKit and App Sandbox capabilities via the
entitlements file (`Resources/HomeIntelligenceHealth.entitlements`) and
via `SystemCapabilities` in `TargetAttributes`. If you need to add
another capability, do it from Xcode's "Signing & Capabilities" UI and
commit the resulting project.pbxproj diff.
