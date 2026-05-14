import Foundation
import HealthKit
import os

// One-place coordinator that the SwiftUI "Sync now" button AND the
// AppIntent both call. Records the result back into Settings so the UI
// can show "Last sync: 12 minutes ago — server saved 7 metrics".
struct SyncCoordinator {
    static let logger = Logger(
        subsystem: "com.home-intelligence.healthkit-ios", category: "sync"
    )

    /// Runs one full sync cycle. Returns a human-readable summary suitable
    /// for both the UI and the Shortcuts/Spoken-Result string.
    @MainActor
    static func runOnce() async -> String {
        let settings = Settings.shared
        do {
            guard HKHealthStore.isHealthDataAvailable() else {
                return await record(
                    settings: settings, success: false,
                    summary: "HealthKit isn't available on this device.",
                    inserted: 0, metrics: []
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
            let metricsList = collectedMetrics(snapshot)

            if payload.isEmpty {
                return await record(
                    settings: settings, success: true,
                    summary: "Nothing to send (no Health samples in the last \(formatWindow(settings.windowMinutes))).",
                    inserted: 0, metrics: []
                )
            }
            let response = try await Forwarder.post(payload: payload, settings: settings)

            // Trust the server's count over the local snapshot count —
            // if the orchestrator's normalizer dropped some rows (e.g. due
            // to a JSON-shape mismatch), the user should see that, not a
            // misleading "uploaded 8" when 0 actually landed.
            let serverInserted = response.inserted ?? 0
            let serverSkipped  = response.skipped ?? 0
            let bytes = payload.byteCount

            let summary: String
            if serverInserted == 0 && !metricsList.isEmpty {
                summary =
                    "Sent \(metricsList.count) metric(s) (\(bytes) bytes) but the server saved 0 rows. " +
                    "Check the orchestrator log — the payload format may not match what it expects."
            } else if serverSkipped > 0 {
                summary =
                    "Server saved \(serverInserted) new row(s), skipped \(serverSkipped) duplicate(s). " +
                    "Sent: \(metricsList.joined(separator: ", "))."
            } else {
                summary =
                    "Server saved \(serverInserted) row(s). " +
                    "Sent: \(metricsList.joined(separator: ", "))."
            }
            return await record(
                settings: settings,
                success: serverInserted > 0 || metricsList.isEmpty,
                summary: summary,
                inserted: serverInserted,
                metrics: metricsList
            )
        } catch let error as ForwardError {
            return await record(
                settings: settings, success: false,
                summary: error.errorDescription ?? "Unknown forward error.",
                inserted: 0, metrics: []
            )
        } catch {
            return await record(
                settings: settings, success: false,
                summary: "Sync failed: \(error.localizedDescription)",
                inserted: 0, metrics: []
            )
        }
    }

    /// Translates the snapshot's optional fields into the user-facing list of
    /// metrics that actually had data this run. Used both for the summary
    /// string and for the "last metrics" badge in the UI.
    private static func collectedMetrics(_ s: Snapshot) -> [String] {
        var out: [String] = []
        if s.steps != nil            { out.append("Steps") }
        if s.activeEnergy != nil     { out.append("Active Energy") }
        if s.heartRate != nil        { out.append("Heart Rate") }
        if s.restingHeartRate != nil { out.append("Resting HR") }
        if s.hrv != nil              { out.append("HRV") }
        if s.weight != nil           { out.append("Weight") }
        if s.bloodOxygen != nil      { out.append("Blood Oxygen") }
        if s.vo2Max != nil           { out.append("VO₂ Max") }
        if let sleep = s.sleep, sleep.totalAsleepMin > 0 {
            out.append("Sleep")
            if sleep.coreMin != nil { out.append("• Core") }
            if sleep.deepMin != nil { out.append("• Deep") }
            if sleep.remMin != nil  { out.append("• REM") }
        }
        if !s.workouts.isEmpty       { out.append("Workouts (\(s.workouts.count))") }
        return out
    }

    private static func formatWindow(_ minutes: Int) -> String {
        if minutes < 60        { return "\(minutes) min" }
        if minutes < 60 * 24   { return "\(minutes / 60) hour(s)" }
        return "\(minutes / (60 * 24)) day(s)"
    }

    @MainActor
    private static func record(
        settings: Settings, success: Bool, summary: String,
        inserted: Int, metrics: [String]
    ) -> String {
        settings.lastRunAt = Date()
        settings.lastRunSummary = summary
        settings.lastRunWasError = !success
        settings.lastInsertedCount = inserted
        settings.lastMetricsList = metrics
        if success {
            logger.info("\(summary, privacy: .public)")
        } else {
            logger.error("\(summary, privacy: .public)")
        }
        return summary
    }
}
