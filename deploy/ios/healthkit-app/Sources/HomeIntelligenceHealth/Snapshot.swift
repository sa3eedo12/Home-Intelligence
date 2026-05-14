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
    var sleepAsleepMin: Double?          // sum of asleep-stage minutes in window
    var sleepWindow: SleepWindow?        // optional aggregate window
    var workouts: [Workout] = []

    /// Number of *quantity* metrics that have data. Used in summary log only.
    var metricCount: Int {
        let quantities: [Double?] = [
            steps, activeEnergy, heartRate, restingHeartRate,
            hrv, weight, bloodOxygen, vo2Max,
        ]
        let q = quantities.compactMap { $0 }.count
        let s = (sleepAsleepMin ?? 0) > 0 ? 1 : 0
        return q + s
    }
}

struct SleepWindow {
    var start: Date
    var end: Date
    var asleepMin: Double
}

struct Workout {
    var typeName: String         // e.g. "Walking"
    var start: Date
    var end: Date
    var durationMin: Double
    var activeEnergy: Double?    // kcal
    var distanceM: Double?       // meters
}
