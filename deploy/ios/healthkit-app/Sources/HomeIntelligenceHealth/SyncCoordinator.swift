import Foundation
import HealthKit
import os

// One-place coordinator that the SwiftUI "Sync now" button AND the
// AppIntent both call. Records the result back into Settings so the UI
// can show "Last sync: 12 minutes ago — uploaded 7 metrics".
struct SyncCoordinator {
    static let logger = Logger(
        subsystem: "com.home-intelligence.healthkit-ios", category: "sync"
    )

    /// Runs one full sync cycle. Returns a human-readable summary suitable
    /// for both the UI and the Shortcuts/Spoken-Result string.
    @MainActor
    static func runOnce() async -> String {
        let settings = Settings.shared
        let started = Date()
        do {
            guard HKHealthStore.isHealthDataAvailable() else {
                return await record(
                    settings: settings, success: false, started: started,
                    summary: "HealthKit isn't available on this device."
                )
            }
            let store = HKHealthStore()
            try await Authorization.request(store: store)
            let collector = HealthCollector(
                store: store,
                windowMinutes: settings.windowMinutes
            )
            let snapshot = try await collector.collect()
            let payload = PayloadBuilder.build(snapshot)
            if payload.isEmpty {
                return await record(
                    settings: settings, success: true, started: started,
                    summary: "Nothing to send (no Health samples in the last \(settings.windowMinutes) min)."
                )
            }
            let body = try await Forwarder.post(payload: payload, settings: settings)
            let summary =
                "Uploaded \(snapshot.metricCount) metrics + " +
                "\(snapshot.workouts.count) workouts (\(payload.byteCount) bytes)."
            logger.info("\(summary, privacy: .public) | server: \(body, privacy: .public)")
            return await record(
                settings: settings, success: true, started: started, summary: summary
            )
        } catch let error as ForwardError {
            return await record(
                settings: settings, success: false, started: started,
                summary: error.errorDescription ?? "Unknown forward error."
            )
        } catch {
            return await record(
                settings: settings, success: false, started: started,
                summary: "Sync failed: \(error.localizedDescription)"
            )
        }
    }

    @MainActor
    private static func record(
        settings: Settings, success: Bool, started: Date, summary: String
    ) -> String {
        settings.lastRunAt = Date()
        settings.lastRunSummary = summary
        settings.lastRunWasError = !success
        if success {
            logger.info("\(summary, privacy: .public)")
        } else {
            logger.error("\(summary, privacy: .public)")
        }
        return summary
    }
}
