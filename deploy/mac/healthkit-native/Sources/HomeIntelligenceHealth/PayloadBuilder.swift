import Foundation

// Builds the Health Auto Export-shaped JSON the orchestrator's
// HealthAutoExportNormalizer accepts:
//
//   {
//     "data": {
//       "metrics":  [{"type": "HK...", "units": "...", "data": [{date, qty}]}],
//       "workouts": [{"type": "HKWorkoutTypeIdentifier", "name": "...",
//                     "start": "...", "end": "...", "duration": ...}]
//     }
//   }
//
// Keep the type identifiers in sync with orchestrator/health.py:
//   _HEALTHKIT_METRICS — quantity types
//   _SLEEP_STAGE_METRICS — sleep stage values
struct Payload {
    let body: Data
    let isEmpty: Bool
    var byteCount: Int { body.count }
}

enum PayloadBuilder {
    static func build(_ snapshot: Snapshot) -> Payload {
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime]
        iso.timeZone = TimeZone(identifier: "UTC")

        var metrics: [[String: Any]] = []
        let captured = iso.string(from: snapshot.capturedAt)

        // Quantity metrics — only emit ones we have data for.
        func addQuantity(
            _ type: String, _ unit: String, _ value: Double?
        ) {
            guard let value = value else { return }
            metrics.append([
                "type": type,
                "units": unit,
                "data": [["date": captured, "qty": value]],
            ])
        }
        addQuantity("HKQuantityTypeIdentifierStepCount",
                    "steps", snapshot.steps)
        addQuantity("HKQuantityTypeIdentifierActiveEnergyBurned",
                    "kcal", snapshot.activeEnergy)
        addQuantity("HKQuantityTypeIdentifierHeartRate",
                    "bpm", snapshot.heartRate)
        addQuantity("HKQuantityTypeIdentifierRestingHeartRate",
                    "bpm", snapshot.restingHeartRate)
        addQuantity("HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
                    "ms", snapshot.hrv)
        addQuantity("HKQuantityTypeIdentifierBodyMass",
                    "kg", snapshot.weight)
        addQuantity("HKQuantityTypeIdentifierOxygenSaturation",
                    "%", snapshot.bloodOxygen)
        addQuantity("HKQuantityTypeIdentifierVO2Max",
                    "mL/kg/min", snapshot.vo2Max)

        // Sleep — emit a single asleep aggregate if we found any.
        if let asleepMin = snapshot.sleepAsleepMin, asleepMin > 0 {
            let window = snapshot.sleepWindow
            let start = iso.string(from: window?.start ?? snapshot.capturedAt)
            let end   = iso.string(from: window?.end   ?? snapshot.capturedAt)
            metrics.append([
                "type": "HKCategoryTypeIdentifierSleepAnalysis",
                "data": [[
                    "startDate": start,
                    "endDate":   end,
                    "stage":     "asleep",
                    "qty":       asleepMin,
                    "value":     "asleep",
                ]],
            ])
        }

        // Workouts — separate top-level array (the normalizer reads both
        // `data.metrics` workout entries AND `data.workouts`).
        let workouts: [[String: Any]] = snapshot.workouts.map { wk in
            var item: [String: Any] = [
                "type":     "HKWorkoutTypeIdentifier",
                "name":     wk.typeName,
                "start":    iso.string(from: wk.start),
                "end":      iso.string(from: wk.end),
                "duration": wk.durationMin,
            ]
            if let energy = wk.activeEnergy   { item["activeEnergy"] = energy }
            if let dist   = wk.distanceM      { item["distance"]     = dist   }
            return item
        }

        var data: [String: Any] = ["metrics": metrics]
        if !workouts.isEmpty { data["workouts"] = workouts }

        let envelope: [String: Any] = ["data": data]
        let isEmpty = metrics.isEmpty && workouts.isEmpty
        let body = (try? JSONSerialization.data(
            withJSONObject: envelope,
            options: [.sortedKeys]
        )) ?? Data()
        return Payload(body: body, isEmpty: isEmpty)
    }
}
