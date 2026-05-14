# Mac-side bridges → Home Intelligence

Apple's Health data lives on iOS / iPadOS / watchOS — there's no public
HealthKit access on macOS. Even Health Auto Export's "Mac app" is a
sync receiver, not a HealthKit reader. So the bridges that actually
read your data run on iPhone; the Mac is only ever an optional relay.

| Path                                                               | Cost                          | Apple Dev acc. | Where it reads HealthKit |
|--------------------------------------------------------------------|-------------------------------|----------------|--------------------------|
| [`../ios/healthkit-app/`](../ios/healthkit-app/)                   | $0 (Personal Team) or $99/yr  | **Required**   | iPhone (custom app)      |
| [`../ios/healthkit-shortcut/`](../ios/healthkit-shortcut/)         | **$0 (free)**                 | No             | iPhone (Shortcut only)   |
| [`./healthkit-bridge/`](./healthkit-bridge/)                       | ~$5 (one-time iOS IAP)        | No             | iPhone (paid app), Mac forwards |

You can run **any combination** simultaneously — they all POST to the same
`/admin/healthkit/sync` endpoint and the orchestrator dedupes by
`(metric, timestamp, member_id)`.

## How they differ

`../ios/healthkit-app/` is a **signed iOS app** built once in Xcode that
reads HealthKit on the iPhone using `HKHealthStore`, exposes a
"Sync Health to Home Intelligence" action to Shortcuts (via AppIntent),
and a Personal Automation triggers it on a schedule. Most reliable
free path if you have an Apple Developer account.

`../ios/healthkit-shortcut/` is an **iOS Shortcut** you build from
scratch on your iPhone. Reads HealthKit via Shortcuts' "Find Health
Samples" actions, builds JSON inline, POSTs via "Get Contents of URL".
No Xcode, no app — just the Shortcuts app.

`./healthkit-bridge/` is a Mac-side **iCloud Drive relay**: the paid
"Health Auto Export — JSON+CSV" iOS app dumps JSON files into iCloud
Drive, and a launchd-scheduled Python script on an always-on Mac
forwards them to TrueNAS.

## Common prerequisites (all paths)

- TrueNAS orchestrator reachable on your LAN at `http://<truenas>:8080`
- `HEALTHKIT_WEBHOOK_TOKEN` set on TrueNAS — see step 2 in
  `./healthkit-bridge/README.md` for how to generate and install it

The shared token authenticates whatever bridge POSTs the data. Use the
same value for whichever path(s) you install.
