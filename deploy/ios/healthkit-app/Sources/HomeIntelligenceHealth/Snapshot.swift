import Foundation

// Flat snapshot the collector produces — one struct per sync, mirroring the
// JSON shape the orchestrator's HealthAutoExportNormalizer accepts. Optional
// fields are skipped from the payload (not zeroed) so the orchestrator never
// stores a fake "0 steps" if HealthKit had no data.
struct Snapshot {
    var capturedAt: Date
    var windowMinutes: Int
    var steps: Double?
    var activeEnergy: Double?            // kcal
    var heartRate: Double?               // bpm (avg over window)
    var restingHeartRate: Double?        // bpm (latest)
    var hrv: Double?                     // ms (latest HRV SDNN)
    var weight: Double?                  // kg (latest body mass)
    var bloodOxygen: Double?             // % (latest)
    var vo2Max: Double?                  // mL/kg/min (latest)

    // Sleep is reported as a per-stage breakdown when watchOS supports it
    // (Series 4+ on watchOS 9+). The orchestrator stores each stage as a
    // distinct metric (sleep_core / sleep_deep / sleep_rem / sleep_awake /
    // sleep_inBed) plus a total `sleep_asleep` that's the UNION (no
    // double-counting) of all asleep-class intervals.
    var sleep: SleepBreakdown?

    var workouts: [Workout] = []

    /// Number of metrics that have data. Used in the summary log only.
    var metricCount: Int {
        let quantities: [Double?] = [
            steps, activeEnergy, heartRate, restingHeartRate,
            hrv, weight, bloodOxygen, vo2Max,
        ]
        let q = quantities.compactMap { $0 }.count
        let s = (sleep?.totalAsleepMin ?? 0) > 0 ? 1 : 0
        return q + s
    }
}

struct SleepBreakdown {
    /// Total asleep time = union of all asleep-class intervals (Core, Deep,
    /// REM, asleepUnspecified, and legacy .asleep), counted only once even
    /// when watchOS emits both a legacy aggregate and per-stage samples.
    var totalAsleepMin: Double
    /// Per-stage minutes (each summed independently). All optional — older
    /// devices only emit legacy .asleep so the per-stage fields will be nil.
    var coreMin: Double?
    var deepMin: Double?
    var remMin: Double?
    var unspecifiedMin: Double?
    /// Awake time inside the in-bed window. Useful for sleep-quality
    /// downstream analysis.
    var awakeMin: Double?
    /// Total in-bed time (which is awake + asleep + brief stirrings).
    var inBedMin: Double?
    /// Earliest start of any sleep sample in the window, used as the canonical
    /// `started_at` so re-syncs of the same night dedupe correctly.
    var windowStart: Date
    /// Latest end of any sleep sample in the window.
    var windowEnd: Date
}

struct Workout {
    var typeName: String         // e.g. "Walking"
    var start: Date
    var end: Date
    var durationMin: Double
    var activeEnergy: Double?    // kcal
    var distanceM: Double?       // meters
}
