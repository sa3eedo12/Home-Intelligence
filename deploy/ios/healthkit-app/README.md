# Native iOS HealthKit app → Home Intelligence

A signed iOS app that reads your Apple Health data on iPhone and POSTs
it to the TrueNAS orchestrator over your home WiFi. Best free option if
you have an Apple Developer account (paid not required — the free
Personal Team works for personal sideload).

```
iPhone HealthKit  ──HKHealthStore──▶  HomeIntelligenceHealth.app
                                            │
                              Shortcuts AppIntent ◀── Personal Automation (every hour)
                                            │
                                            ▼  POST + X-Health-Token
                                   TrueNAS orchestrator (LAN-only)
```

Why an iOS app instead of a Mac app: macOS has no public way to read
HealthKit. Even Health Auto Export's "Mac app" is a sync receiver, not
a HealthKit reader (per their App Store listing: *"Sync to Mac makes
your health metrics and workouts available on Mac by syncing from
iPhone"*). HealthKit lives on iOS / iPadOS / watchOS — and that's where
the read has to happen.

## Requirements

- iOS 17 or newer on the iPhone you'll install to
- Xcode 15 or newer on the Mac doing the build (tested with Xcode 26)
- An Apple Developer account (free Personal Team works for personal
  sideload; sideloaded apps from a free team must be re-signed every
  7 days, paid Developer accounts have no expiry)
- iPhone joining the same WiFi as TrueNAS most of the day
- `HEALTHKIT_WEBHOOK_TOKEN` set on TrueNAS — see step 2 in
  `../../mac/healthkit-bridge/README.md`

## File layout in this folder

```
healthkit-app/
├── README.md                                       ← this file
├── HomeIntelligenceHealth.xcodeproj/               ← pre-built Xcode project
├── Sources/HomeIntelligenceHealth/
│   ├── App.swift                                   ← @main SwiftUI App
│   ├── ContentView.swift                           ← settings UI + Sync now button
│   ├── Settings.swift                              ← UserDefaults-backed config
│   ├── SyncCoordinator.swift                       ← orchestrates one sync cycle
│   ├── SyncIntent.swift                            ← AppIntent exposed to Shortcuts
│   ├── Authorization.swift                         ← HKHealthStore.requestAuthorization
│   ├── HealthCollector.swift                       ← runs the HKHealthStore queries
│   ├── Snapshot.swift                              ← in-memory shape
│   ├── PayloadBuilder.swift                        ← builds Health Auto Export JSON
│   └── Forwarder.swift                             ← URLSession POST
├── Resources/
│   ├── Info.plist                                  ← bundle metadata + Health usage strings
│   └── HomeIntelligenceHealth.entitlements         ← HealthKit entitlement
└── build.sh                                        ← xcodebuild wrapper
```

## One-time setup

### 1. Set your Apple Developer team

Two options:

**Option A — Xcode GUI (recommended)**

1. Open the project: `open HomeIntelligenceHealth.xcodeproj`
2. Select **HomeIntelligenceHealth** at the top of the navigator.
3. Select the **HomeIntelligenceHealth** target → **Signing & Capabilities** tab.
4. Under **Team**, pick your team.
5. Confirm **HealthKit** capability is listed (it's already there from
   the entitlements file). If Xcode shows a warning about missing
   capability registration with Apple, click **Try Again** — Xcode will
   auto-create a provisioning profile.
6. **⌘S** to save.

**Option B — CLI**

Find your team id at https://developer.apple.com/account → Membership →
Team ID. Then:

```sh
DEVELOPMENT_TEAM=ABC123XYZ4 ./build.sh
```

### 2. Build the app for your iPhone

Two options:

**Option A — Xcode (easiest)**

1. Plug your iPhone into your Mac via USB (or pair over WiFi:
   Window → Devices and Simulators → check "Connect via network").
2. In Xcode's run-destination dropdown (top of the window), pick your
   iPhone instead of "Any iOS Device".
3. Press **⌘R**. Xcode builds, signs, installs, and launches the app.
   First time you do this on a new device you'll have to:
   - Trust the developer profile: **Settings → General → VPN & Device
     Management → [your name] → Trust**
   - Acknowledge the HealthKit prompt that appears in the app

**Option B — CLI**

```sh
./build.sh
xcrun devicectl list devices                          # find your iPhone's UDID
xcrun devicectl device install app --device <udid> \
    ./build/Build/Products/Release-iphoneos/HomeIntelligenceHealth.app
```

### 3. Configure inside the app

Open **HI Health** on your iPhone:

- **URL**: `http://<truenas-ip>:8080`  (e.g. `http://192.168.1.190:8080`)
- **X-Health-Token**: paste the same `HEALTHKIT_WEBHOOK_TOKEN` value
  from TrueNAS
- **Member ID**: optional, your `household_members.id` (e.g. `2` for
  Saeed)
- **Sync window**: how far back each run looks (default 60 min — set to
  match your automation interval)

Tap **Sync now**. iOS asks which Health categories to grant — tap
**Turn All Categories On** → **Allow**.

You should see a result line like:

```
Uploaded 7 metrics + 0 workouts (812 bytes).
Last run: just now
```

If you see an error, the message is hopefully self-explanatory; common
ones are listed in Troubleshooting below.

### 4. Schedule it via Shortcuts

This is the magic that makes it run on its own without you opening the
app each time.

1. Open the **Shortcuts** app on iPhone.
2. **Automation** tab → **+** (top right) → **Create Personal
   Automation**.
3. Pick **Time of Day** → e.g. **Every hour at :00** → **Next**.
4. **Add Action** → search for **"Sync Health to Home Intelligence"**
   (this is the AppIntent exposed by our app).
5. Toggle **Run Immediately** → **ON** (iOS 15.2+ — skips the
   tap-to-confirm prompt every time).
6. Toggle **Notify When Run** → **OFF** (so it's silent unless you
   want to debug).
7. **Done**.

The automation will fire on schedule from now on, even when the iPhone
is locked, as long as it's connected to a network. If the phone is off
home WiFi when the automation fires, the upload fails silently and the
next scheduled run succeeds.

### 5. Verify on TrueNAS

```sh
curl http://localhost:8080/admin/healthkit/recent?metric=steps
```

You should see your most recent steps samples. Within a day:

```sh
curl http://localhost:8080/admin/healthkit/aggregate?metric=steps&days=1
```

## Re-building after pulling repo updates

```sh
git pull
./build.sh
# Then re-install via Xcode ⌘R or `xcrun devicectl device install app ...`
```

The Personal Automation keeps working — it references the Shortcut by
name, not by app version.

## Comparison to other paths

| Aspect              | This (iOS app)                            | `../healthkit-shortcut/` (pure Shortcut) | `../../mac/healthkit-bridge/` (paid)  |
|---------------------|-------------------------------------------|------------------------------------------|----------------------------------------|
| Cost                | $0 (Personal Team) / $99/yr (paid Dev)    | $0                                        | ~$5 (one-time iOS IAP)                |
| Apple Developer acc | **Required**                              | No                                        | No                                    |
| Xcode               | **Required**                              | No                                        | No                                    |
| Coverage            | All HealthKit you authorize               | What the Shortcut reads                   | What Health Auto Export exports       |
| Re-signing cadence  | Every 7 days (free Personal Team)         | n/a                                       | n/a                                   |
| Setup complexity    | Medium (~15 min, one-time)                | Medium (~15 min, build Shortcut by hand)  | Low (~5 min, install paid app)        |
| Background reliable | High (Shortcuts Personal Automation)      | Medium (same, but more fragile JSON)      | High (Mac launchd)                    |

You can run **any combination** — the orchestrator dedupes by
`(metric, timestamp, member_id)`.

## Troubleshooting

- **"Settings missing: Orchestrator URL"** — open the app and fill in
  the URL field; it has to be a complete URL like `http://192.168.1.190:8080`.
- **"Couldn't reach the orchestrator: A server with the specified
  hostname could not be found"** — your iPhone isn't on the same WiFi
  as TrueNAS. Confirm by opening Safari on iPhone and visiting
  `http://<truenas-ip>:8080/dashboard` — if Safari can't load it, the
  app can't either.
- **"Server rejected the upload (HTTP 401)"** — `HEALTHKIT_TOKEN`
  doesn't match TrueNAS. Re-paste the value from the TrueNAS `.env`.
- **"Server rejected the upload (HTTP 503)"** —
  `HEALTHKIT_WEBHOOK_TOKEN` isn't set on TrueNAS. See step 2 of the
  paid-bridge README for how to set it.
- **"HealthKit isn't available on this device"** — you're trying to run
  on a Mac via Designed for iPad. HealthKit doesn't work in that
  context (only on actual iOS/iPadOS hardware). Install on your iPhone
  instead.
- **Personal Automation doesn't fire on schedule** — iOS sometimes
  throttles automations to save power. Open Shortcuts → Automation →
  tap your automation → "Run Immediately" must be ON. Tapping the
  manual play button once also re-validates the schedule.
- **"This app cannot be installed because its integrity could not be
  verified"** — your Personal Team certificate expired (free signing
  expires every 7 days). Re-build and re-install via Xcode, or upgrade
  to a paid Developer account.
- **App icon shows a red badge with "1" but app does nothing** — that's
  iOS background-fetch retry behavior. Open the app once to clear it.

## How the .xcodeproj was built

For maintainers: the `HomeIntelligenceHealth.xcodeproj` was hand-crafted
(not generated by Xcode "File → New"). It uses stable UUIDs prefixed
`FAC...` for readability, and references source files relative to the
project's parent directory so this repo can update the Swift sources
without touching the project file.

Verified: builds cleanly against iPhoneSimulator26.5 SDK with codesign
disabled. The HealthKit entitlement is carried via the entitlements
file and via `SystemCapabilities` in `TargetAttributes` — Xcode picks
both up when you set your team.
