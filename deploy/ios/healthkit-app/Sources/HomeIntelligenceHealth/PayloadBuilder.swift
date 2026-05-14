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

        // Sleep — emit one entry per stage that has data. The orchestrator's
        // normalizer maps each stage to its own metric (sleep_core,
        // sleep_deep, sleep_rem, sleep_asleep, sleep_awake, sleep_inBed) so
        // the dashboard can show a breakdown. The TOTAL `sleep_asleep` uses
        // the union of asleep-class intervals so it doesn't double-count
        // legacy `.asleep` + per-stage samples.
        if let sleep = snapshot.sleep, sleep.totalAsleepMin > 0 {
            let startStr = iso.string(from: sleep.windowStart)
            let endStr   = iso.string(from: sleep.windowEnd)

            func addSleep(stage: String, minutes: Double?) {
                guard let minutes = minutes, minutes > 0 else { return }
                metrics.append([
                    "type": "HKCategoryTypeIdentifierSleepAnalysis",
                    "data": [[
                        "startDate": startStr,
                        "endDate":   endStr,
                        "stage":     stage,
                        "qty":       minutes,
                        "value":     stage,
                    ]],
                ])
            }
            // Total asleep first — this is what the dashboard's "you slept
            // X hours" surface uses.
            addSleep(stage: "asleep", minutes: sleep.totalAsleepMin)
            addSleep(stage: "core",   minutes: sleep.coreMin)
            addSleep(stage: "deep",   minutes: sleep.deepMin)
            addSleep(stage: "rem",    minutes: sleep.remMin)
            addSleep(stage: "awake",  minutes: sleep.awakeMin)
            addSleep(stage: "inBed",  minutes: sleep.inBedMin)
            // .asleepUnspecified maps to "sleep_asleep" upstream, but we
            // already account for it in the total above; emitting it again
            // would double-count.
        }

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
