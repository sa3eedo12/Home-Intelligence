# Mac-side bridges → Home Intelligence

Two options for getting Apple Health data into the TrueNAS orchestrator
without exposing it to the public internet. Pick whichever fits your
budget and tolerance for one-time setup.

| Bridge                                          | Cost           | Setup        | Best for                                        |
|-------------------------------------------------|----------------|--------------|-------------------------------------------------|
| [`./healthkit-shortcuts/`](./healthkit-shortcuts/) | **$0 (free)**  | ~10 min GUI  | Anyone with macOS Sonoma 14+ (no iOS app needed)|
| [`./healthkit-bridge/`](./healthkit-bridge/)       | ~$5 (iOS IAP) | ~5 min CLI   | Already using "Health Auto Export — JSON+CSV"   |

You can run **both** simultaneously — they POST to the same
`/admin/healthkit/sync` endpoint and the orchestrator dedupes by
`(metric, timestamp, member_id)`.

## How they differ

`healthkit-bridge/` watches a folder where the **paid** "Health Auto
Export — JSON+CSV" iOS app drops JSON files via iCloud Drive. It just
forwards files; it doesn't read HealthKit itself.

`healthkit-shortcuts/` reads HealthKit on the Mac directly via macOS
Shortcuts, then POSTs. No iOS app, no folder watching. Requires macOS
Monterey 12+ for the `shortcuts` CLI and macOS Sonoma 14+ recommended
for the most complete iCloud-synced Health data on the Mac.

## Common prerequisites (both bridges)

- TrueNAS orchestrator reachable on your LAN at `http://<truenas>:8080`
- `HEALTHKIT_WEBHOOK_TOKEN` set on TrueNAS — see step 2 in
  `./healthkit-bridge/README.md` for how to generate and install it

The shared token authenticates the Mac side. Use the same value for
whichever bridge(s) you install.
