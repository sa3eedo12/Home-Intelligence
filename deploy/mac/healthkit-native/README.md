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
the iPhone to be present. The entitlement requires Apple Developer
signing — hence the one-time Xcode setup below.

## Requirements

- macOS 13 (Ventura) or newer
- Xcode 15 or newer (this repo built with Xcode 26)
- An Apple Developer account (free Personal Team is enough for personal
  use; paid not required for HealthKit on Mac)
- iCloud signed in with the same Apple ID as your iPhone, with
  **Settings → [your name] → iCloud → Health** enabled on the iPhone
- `HEALTHKIT_WEBHOOK_TOKEN` set on TrueNAS — see step 2 in
  `../healthkit-bridge/README.md`

## File layout in this folder

```
healthkit-native/
├── README.md
├── Sources/HomeIntelligenceHealth/      ← Swift source (drop into Xcode)
│   ├── main.swift                       ← entry point + flow control
│   ├── Config.swift                     ← parses env vars
│   ├── Authorization.swift              ← HealthKit permission request
│   ├── HealthCollector.swift            ← runs the HKHealthStore queries
│   ├── Snapshot.swift                   ← in-memory shape
│   ├── PayloadBuilder.swift             ← builds Health Auto Export JSON
│   ├── Forwarder.swift                  ← URLSession POST
│   └── Log.swift                        ← writes to ~/Library/Logs/...
├── Resources/
│   ├── Info.plist                       ← bundle metadata + Health usage string
│   └── HomeIntelligenceHealth.entitlements
├── install-launchd.sh                   ← installs the LaunchAgent
├── uninstall-launchd.sh
└── com.home-intelligence.healthkit-native.plist.template
```

## One-time setup

### 1. Create the Xcode project

1. Open **Xcode** → **File → New → Project…**
2. Choose **macOS → App** → Next.
3. Settings:
   - **Product Name**: `HomeIntelligenceHealth`
   - **Team**: select your Apple Developer team
   - **Organization Identifier**: `com.home-intelligence`
     (this gives the bundle id `com.home-intelligence.HomeIntelligenceHealth`
     — you'll change it to `com.home-intelligence.healthkit-native` in
     step 4)
   - **Interface**: **AppKit** (or SwiftUI; we don't draw a UI)
   - **Language**: **Swift**
   - Storage / Tests: **off**
4. Save the `.xcodeproj` somewhere convenient — e.g.
   `~/Developer/HomeIntelligenceHealth/`. **Do NOT** save it inside this
   git repo; the repo only carries the source files, not Xcode's
   per-machine project state.

### 2. Drop in the source files

Delete the boilerplate Xcode generated (`AppDelegate.swift`,
`ContentView.swift`, `Assets.xcassets`, `*.storyboard`, the default
`Info.plist`, etc.) — leaving an empty target.

Drag-and-drop into the Xcode project navigator from this folder:

- All of `Sources/HomeIntelligenceHealth/*.swift`
- `Resources/Info.plist`
- `Resources/HomeIntelligenceHealth.entitlements`

When prompted: **"Copy items if needed" — OFF**, so Xcode references the
source files in place. That way changes you pull from this repo flow into
the project automatically.

### 3. Project settings

In the project's target settings:

- **General → Identity → Bundle Identifier**: `com.home-intelligence.healthkit-native`
- **General → Deployment Info → Minimum Deployment**: `macOS 13.0`
- **General → App Category**: Healthcare & Fitness (optional)
- **Info → Custom macOS Application Target Properties**: ensure the
  `Info.plist` from `Resources/` is the file Xcode is using
  (Build Settings → "Info.plist File" should point to it). Verify
  `NSHealthShareUsageDescription` is present.
- **Signing & Capabilities → Signing**:
  - Team: pick your team
  - Signing Certificate: **Apple Development** (auto-managed)
- **Signing & Capabilities → + Capability → HealthKit**. Confirm Xcode
  shows "HealthKit" added. You'll see this writes
  `com.apple.developer.healthkit = YES` into the entitlements; the
  pre-shipped `.entitlements` already has it but Xcode needs to enable
  the matching app capability via the developer portal.
- **Signing & Capabilities → + Capability → App Sandbox**. Ensure
  **Network → Outgoing Connections (Client)** is checked so URLSession
  can reach TrueNAS.
- **Build Settings → Code Signing Entitlements**: should auto-set to
  `Resources/HomeIntelligenceHealth.entitlements`. If not, set it
  manually.

### 4. Build

Product → **Build** (⌘B). Should produce `HomeIntelligenceHealth.app`
in Xcode's derived data folder. To find the binary:

```sh
xcodebuild -project ~/Developer/HomeIntelligenceHealth/HomeIntelligenceHealth.xcodeproj \
  -scheme HomeIntelligenceHealth \
  -configuration Release \
  -showBuildSettings | grep BUILT_PRODUCTS_DIR
```

Then copy the .app to a stable location:

```sh
mkdir -p ~/Applications
cp -R "<BUILT_PRODUCTS_DIR>/HomeIntelligenceHealth.app" ~/Applications/
```

### 5. First run — grant HealthKit permission

The first time the app runs, macOS shows a system prompt asking which
Health categories the app may read (steps, heart rate, sleep, …). Click
**Turn All Categories On** → **Allow**.

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

### 6. Schedule via launchd

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

### 7. Verify

```sh
tail -f ~/Library/Logs/HomeIntelligenceHealth.log
```

You should see a new "uploaded N metrics" line each interval. On TrueNAS:

```sh
curl http://localhost:8080/admin/healthkit/recent?metric=steps
```

## Re-building after pulling repo updates

Because the source files in this repo are referenced (not copied) by your
Xcode project, you only need to:

1. `git pull` in this repo
2. **Product → Build** in Xcode
3. `cp -R <BUILT_PRODUCTS_DIR>/HomeIntelligenceHealth.app ~/Applications/`
4. The next launchd interval picks up the new binary automatically

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
| Setup effort        | High (one-time, ~30 min)                 | Medium (~15 min, build Shortcut)   | Low (~5 min)                          |
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
