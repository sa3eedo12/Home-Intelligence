# Mac-side bridges → Home Intelligence

Three paths for getting Apple Health data into the TrueNAS orchestrator
without exposing it to the public internet. Pick whichever fits your
budget and tolerance for one-time setup.

| Path                                                               | Cost                          | Apple Dev acc. | Mac required |
|--------------------------------------------------------------------|-------------------------------|----------------|--------------|
| [`./healthkit-native/`](./healthkit-native/)                       | $0 (free Personal Team)       | **Required**   | Yes          |
| [`../ios/healthkit-shortcut/`](../ios/healthkit-shortcut/)         | **$0 (free)**                 | No             | No           |
| [`./healthkit-bridge/`](./healthkit-bridge/)                       | ~$5 (one-time iOS IAP)        | No             | Yes          |

You can run **any combination** simultaneously — they all POST to the same
`/admin/healthkit/sync` endpoint and the orchestrator dedupes by
`(metric, timestamp, member_id)`.

## How they differ

`./healthkit-native/` is a **signed macOS app** built once in Xcode that
uses the HealthKit framework directly to read iCloud-synced health data
on the Mac. Most reliable for background scheduling. Requires an Apple
Developer account (free Personal Team is enough) and Xcode.

`../ios/healthkit-shortcut/` is an **iOS Shortcut** you build once on
your iPhone. Reads HealthKit, builds JSON, and POSTs directly to TrueNAS
over your home WiFi via a Personal Automation. No Mac, no Xcode.

`./healthkit-bridge/` is a Mac-side **iCloud Drive relay**: the paid
"Health Auto Export — JSON+CSV" iOS app dumps JSON files into iCloud
Drive, and a launchd-scheduled Python script on an always-on Mac
forwards them to TrueNAS.

## Common prerequisites (all paths)

- TrueNAS orchestrator reachable on your LAN at `http://<truenas>:8080`
- `HEALTHKIT_WEBHOOK_TOKEN` set on TrueNAS — see step 2 in
  `./healthkit-bridge/README.md` for how to generate and install it

The shared token authenticates the iPhone or Mac. Use the same value
for whichever path(s) you install.
