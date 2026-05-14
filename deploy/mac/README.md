# Mac-side bridges → Home Intelligence

Two paths for getting Apple Health data into the TrueNAS orchestrator
without exposing it to the public internet. The free path lives in
`../ios/healthkit-shortcut/` since macOS has no Health app — only iOS
can read HealthKit directly.

| Path                                                    | Cost            | Mac required | Where it runs              |
|---------------------------------------------------------|-----------------|--------------|----------------------------|
| [`../ios/healthkit-shortcut/`](../ios/healthkit-shortcut/) | **$0 (free)**   | No           | iOS Shortcut on iPhone     |
| [`./healthkit-bridge/`](./healthkit-bridge/)            | ~$5 (iOS IAP)   | Yes          | Mac forwards iCloud files  |

You can run **both** simultaneously — they POST to the same
`/admin/healthkit/sync` endpoint and the orchestrator dedupes by
`(metric, timestamp, member_id)`.

## How they differ

`../ios/healthkit-shortcut/` is an **iOS Shortcut** you build once on
your iPhone. It reads HealthKit, builds JSON, and POSTs directly to
TrueNAS over your home WiFi via a Personal Automation that fires on
schedule. No Mac involvement.

`./healthkit-bridge/` is a Mac-side relay: the paid "Health Auto
Export — JSON+CSV" iOS app dumps JSON files into iCloud Drive, and a
launchd-scheduled Python script on an always-on Mac forwards those
files to TrueNAS. Useful if you've already paid for Health Auto Export
or if your iPhone is rarely on home WiFi (because the Mac queues via
iCloud).

## Common prerequisites (both paths)

- TrueNAS orchestrator reachable on your LAN at `http://<truenas>:8080`
- `HEALTHKIT_WEBHOOK_TOKEN` set on TrueNAS — see step 2 in
  `./healthkit-bridge/README.md` for how to generate and install it

The shared token authenticates the iPhone or Mac. Use the same value
for whichever path(s) you install.
