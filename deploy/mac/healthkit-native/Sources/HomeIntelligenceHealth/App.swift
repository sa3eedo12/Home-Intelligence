// HomeIntelligenceHealth — read recent HealthKit samples on macOS and POST
// them to the Home Intelligence orchestrator on TrueNAS.
//
// File is named App.swift (not main.swift) on purpose: when a Swift file is
// named main.swift, Swift treats it as a top-level script and rejects the
// @main attribute. Any other filename lets @main fire normally.
//
// Designed as a one-shot CLI-style app (read → post → exit), driven by a
// LaunchAgent on a fixed interval. The app *bundle* exists so we can carry
// the HealthKit entitlement (HKHealthStore is gated behind that
// entitlement, which only signed app bundles can have).
//
// Env vars (set in the LaunchAgent plist or the shell that invokes it):
//   ORCHESTRATOR_URL    e.g. http://truenas.local:8080  (required)
//   HEALTHKIT_TOKEN     must match HEALTHKIT_WEBHOOK_TOKEN on TrueNAS  (required)
//   MEMBER_ID           optional household_members.id (integer)
//   WINDOW_MINUTES      lookback window for "in the last N minutes" (default 60)
//   REQUEST_TIMEOUT     seconds, default 30
//
// Exit codes:
//   0  success (or nothing to send, also success)
//   2  bad/missing config
//   3  HealthKit unavailable or authorization refused
//   4  retriable network or 5xx error
//   5  permanent rejection (4xx other than 408/425/429)

import Foundation
import HealthKit

@main
struct HomeIntelligenceHealth {
    static func main() async {
        let exitCode = await run()
        exit(exitCode)
    }

    static func run() async -> Int32 {
        let config: Config
        do {
            config = try Config.load()
        } catch {
            Log.error("config: \(error.localizedDescription)")
            return 2
        }

        guard HKHealthStore.isHealthDataAvailable() else {
            Log.error("HealthKit not available on this Mac")
            return 3
        }
        let store = HKHealthStore()

        do {
            try await Authorization.request(store: store)
        } catch {
            Log.error("HealthKit authorization failed: \(error.localizedDescription)")
            return 3
        }

        let collector = HealthCollector(store: store, windowMinutes: config.windowMinutes)
        let snapshot: Snapshot
        do {
            snapshot = try await collector.collect()
        } catch {
            Log.error("HealthKit query failed: \(error.localizedDescription)")
            return 3
        }

        let payload = PayloadBuilder.build(snapshot)
        if payload.isEmpty {
            Log.info("no samples in the last \(config.windowMinutes) min — nothing to send")
            return 0
        }

        do {
            try await Forwarder.post(payload: payload, config: config)
        } catch let error as ForwardError {
            switch error {
            case .retriable(let status, let body):
                Log.warn("retriable HTTP \(status): \(body.prefix(200))")
                return 4
            case .permanent(let status, let body):
                Log.error("permanent HTTP \(status): \(body.prefix(500))")
                return 5
            case .network(let underlying):
                Log.warn("network error: \(underlying.localizedDescription) — will retry next run")
                return 4
            }
        } catch {
            Log.warn("unexpected error: \(error.localizedDescription)")
            return 4
        }

        Log.info(
            "uploaded \(snapshot.metricCount) metrics + " +
            "\(snapshot.workouts.count) workouts (\(payload.byteCount) bytes)"
        )
        return 0
    }
}
