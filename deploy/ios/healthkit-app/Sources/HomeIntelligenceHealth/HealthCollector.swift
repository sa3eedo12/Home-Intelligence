import Foundation
import HealthKit

// Collects HKHealthStore samples for the configured time window and returns
// a flat Snapshot. Each metric's query is independent and skipped silently
// if HealthKit returns no data — the orchestrator handles partial payloads.
struct HealthCollector {
    let store: HKHealthStore
    let windowMinutes: Int

    /// The set of quantity types we read. Authorization.swift uses this list.
    static let quantityTypeIds: [HKQuantityTypeIdentifier] = [
        .stepCount,
        .activeEnergyBurned,
        .heartRate,
        .restingHeartRate,
        .heartRateVariabilitySDNN,
        .bodyMass,
        .oxygenSaturation,
        .vo2Max,
    ]

    func collect() async throws -> Snapshot {
        let now = Date()
        let windowStart = now.addingTimeInterval(-Double(windowMinutes) * 60)

        // Run quantity queries concurrently — they're independent.
        async let steps        = sumQuantity(.stepCount,            unit: .count(),     start: windowStart, end: now)
        async let activeEnergy = sumQuantity(.activeEnergyBurned,   unit: .kilocalorie(),start: windowStart, end: now)
        async let heartRate    = avgQuantity(.heartRate,            unit: bpmUnit(),    start: windowStart, end: now)
        async let restingHR    = mostRecentQuantity(.restingHeartRate,
                                                   unit: bpmUnit(),
                                                   start: now.addingTimeInterval(-86400),
                                                   end: now)
        async let hrv          = mostRecentQuantity(.heartRateVariabilitySDNN,
                                                   unit: .secondUnit(with: .milli),
                                                   start: now.addingTimeInterval(-86400),
                                                   end: now)
        async let weight       = mostRecentQuantity(.bodyMass,
                                                   unit: .gramUnit(with: .kilo),
                                                   start: now.addingTimeInterval(-7 * 86400),
                                                   end: now)
        async let bloodOxygen  = mostRecentQuantity(.oxygenSaturation,
                                                   unit: .percent(),
                                                   start: now.addingTimeInterval(-86400),
                                                   end: now)
        async let vo2Max       = mostRecentQuantity(.vo2Max,
                                                   unit: vo2MaxUnit(),
                                                   start: now.addingTimeInterval(-30 * 86400),
                                                   end: now)
        async let sleepData    = sleepBreakdown(start: now.addingTimeInterval(-86400), end: now)
        async let workouts     = recentWorkouts(start: windowStart, end: now)

        // Convert percent (HealthKit returns 0.0-1.0) into the 0-100 % the
        // orchestrator's normalizer prefers.
        let bo = try await bloodOxygen
        let oxygenPct = bo.map { $0 * 100 }

        return try await Snapshot(
            capturedAt: now,
            windowMinutes: windowMinutes,
            steps:            steps,
            activeEnergy:     activeEnergy,
            heartRate:        heartRate,
            restingHeartRate: restingHR,
            hrv:              hrv,
            weight:           weight,
            bloodOxygen:      oxygenPct,
            vo2Max:           vo2Max,
            sleep:            sleepData,
            workouts:         workouts
        )
    }

    // MARK: - Query helpers

    private func sumQuantity(
        _ identifier: HKQuantityTypeIdentifier,
        unit: HKUnit,
        start: Date, end: Date
    ) async throws -> Double? {
        guard let type = HKQuantityType.quantityType(forIdentifier: identifier) else { return nil }
        let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictStartDate)
        return try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Double?, Error>) in
            let q = HKStatisticsQuery(quantityType: type, quantitySamplePredicate: predicate, options: .cumulativeSum) { _, result, error in
                if let error = error { cont.resume(throwing: error); return }
                let value = result?.sumQuantity()?.doubleValue(for: unit)
                cont.resume(returning: value)
            }
            store.execute(q)
        }
    }

    private func avgQuantity(
        _ identifier: HKQuantityTypeIdentifier,
        unit: HKUnit,
        start: Date, end: Date
    ) async throws -> Double? {
        guard let type = HKQuantityType.quantityType(forIdentifier: identifier) else { return nil }
        let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictStartDate)
        return try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Double?, Error>) in
            let q = HKStatisticsQuery(quantityType: type, quantitySamplePredicate: predicate, options: .discreteAverage) { _, result, error in
                if let error = error { cont.resume(throwing: error); return }
                let value = result?.averageQuantity()?.doubleValue(for: unit)
                cont.resume(returning: value)
            }
            store.execute(q)
        }
    }

    private func mostRecentQuantity(
        _ identifier: HKQuantityTypeIdentifier,
        unit: HKUnit,
        start: Date, end: Date
    ) async throws -> Double? {
        guard let type = HKQuantityType.quantityType(forIdentifier: identifier) else { return nil }
        let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictStartDate)
        let sort = [NSSortDescriptor(key: HKSampleSortIdentifierEndDate, ascending: false)]
        return try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Double?, Error>) in
            let q = HKSampleQuery(sampleType: type, predicate: predicate, limit: 1, sortDescriptors: sort) { _, samples, error in
                if let error = error { cont.resume(throwing: error); return }
                let sample = (samples?.first as? HKQuantitySample)
                cont.resume(returning: sample?.quantity.doubleValue(for: unit))
            }
            store.execute(q)
        }
    }

    /// Reads sleep samples for the window and produces a per-stage breakdown.
    /// Handles the watchOS quirk of emitting both legacy `.asleep` AND fine
    /// stage samples covering the same period: the total `asleep` value is
    /// the UNION of all asleep-class intervals (so overlapping legacy +
    /// stages count once), while the per-stage minutes are summed
    /// independently.
    private func sleepBreakdown(start: Date, end: Date) async throws -> SleepBreakdown? {
        guard let type = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) else {
            return nil
        }
        let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictStartDate)
        let sort = [NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)]
        let samples: [HKCategorySample] = try await withCheckedThrowingContinuation { cont in
            let q = HKSampleQuery(sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit, sortDescriptors: sort) { _, raw, error in
                if let error = error { cont.resume(throwing: error); return }
                cont.resume(returning: (raw as? [HKCategorySample]) ?? [])
            }
            store.execute(q)
        }
        if samples.isEmpty { return nil }

        // Collect intervals per stage so we can both report per-stage totals
        // AND compute the union of all asleep-class intervals.
        var coreIntervals: [(Date, Date)]    = []
        var deepIntervals: [(Date, Date)]    = []
        var remIntervals: [(Date, Date)]     = []
        var unspecIntervals: [(Date, Date)]  = []
        var legacyAsleep: [(Date, Date)]     = []
        var awakeIntervals: [(Date, Date)]   = []
        var inBedIntervals: [(Date, Date)]   = []
        for s in samples {
            guard let value = HKCategoryValueSleepAnalysis(rawValue: s.value) else { continue }
            let pair = (s.startDate, s.endDate)
            switch value {
            case .asleepCore:        coreIntervals.append(pair)
            case .asleepDeep:        deepIntervals.append(pair)
            case .asleepREM:         remIntervals.append(pair)
            case .asleepUnspecified: unspecIntervals.append(pair)
            case .asleep:            legacyAsleep.append(pair)   // legacy aggregate
            case .awake:             awakeIntervals.append(pair)
            case .inBed:             inBedIntervals.append(pair)
            @unknown default:        unspecIntervals.append(pair) // be conservative
            }
        }

        let asleepIntervals = coreIntervals + deepIntervals + remIntervals
                              + unspecIntervals + legacyAsleep
        let totalAsleepSec  = mergedDurationSec(asleepIntervals)
        if totalAsleepSec <= 0 { return nil }

        return SleepBreakdown(
            totalAsleepMin: totalAsleepSec / 60.0,
            coreMin:        coreIntervals.isEmpty   ? nil : sumDurationSec(coreIntervals)   / 60.0,
            deepMin:        deepIntervals.isEmpty   ? nil : sumDurationSec(deepIntervals)   / 60.0,
            remMin:         remIntervals.isEmpty    ? nil : sumDurationSec(remIntervals)    / 60.0,
            unspecifiedMin: unspecIntervals.isEmpty ? nil : sumDurationSec(unspecIntervals) / 60.0,
            awakeMin:       awakeIntervals.isEmpty  ? nil : sumDurationSec(awakeIntervals)  / 60.0,
            inBedMin:       inBedIntervals.isEmpty  ? nil : mergedDurationSec(inBedIntervals) / 60.0,
            windowStart:    asleepIntervals.map { $0.0 }.min() ?? start,
            windowEnd:      asleepIntervals.map { $0.1 }.max() ?? end
        )
    }

    /// Merges overlapping (start, end) intervals and returns the total
    /// covered duration in seconds. Used so a legacy `.asleep` sample
    /// covering the whole night doesn't double-count with the fine-grained
    /// per-stage samples that subdivide it.
    private func mergedDurationSec(_ intervals: [(Date, Date)]) -> TimeInterval {
        if intervals.isEmpty { return 0 }
        let sorted = intervals.sorted(by: { $0.0 < $1.0 })
        var merged: [(Date, Date)] = [sorted[0]]
        for (s, e) in sorted.dropFirst() {
            let last = merged[merged.count - 1]
            if s <= last.1 {
                merged[merged.count - 1] = (last.0, max(last.1, e))
            } else {
                merged.append((s, e))
            }
        }
        return merged.reduce(0) { $0 + $1.1.timeIntervalSince($1.0) }
    }

    /// Plain sum of interval durations — does NOT merge overlaps. Used for
    /// per-stage minutes where overlap shouldn't happen and the user wants
    /// the actual stage time (e.g. "you spent 95 min in deep sleep").
    private func sumDurationSec(_ intervals: [(Date, Date)]) -> TimeInterval {
        intervals.reduce(0) { $0 + $1.1.timeIntervalSince($1.0) }
    }

    private func recentWorkouts(start: Date, end: Date) async throws -> [Workout] {
        let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictEndDate)
        let sort = [NSSortDescriptor(key: HKSampleSortIdentifierEndDate, ascending: true)]
        let samples: [HKWorkout] = try await withCheckedThrowingContinuation { cont in
            let q = HKSampleQuery(sampleType: HKObjectType.workoutType(), predicate: predicate, limit: 25, sortDescriptors: sort) { _, raw, error in
                if let error = error { cont.resume(throwing: error); return }
                cont.resume(returning: (raw as? [HKWorkout]) ?? [])
            }
            store.execute(q)
        }
        return samples.map { wk in
            let energy = wk.statistics(for: HKQuantityType(.activeEnergyBurned))?
                .sumQuantity()?.doubleValue(for: .kilocalorie())
            let distance = wk.statistics(for: HKQuantityType(.distanceWalkingRunning))?
                .sumQuantity()?.doubleValue(for: .meter())
            return Workout(
                typeName: workoutName(wk.workoutActivityType),
                start: wk.startDate,
                end: wk.endDate,
                durationMin: wk.duration / 60.0,
                activeEnergy: energy,
                distanceM: distance
            )
        }
    }

    // MARK: - Unit shims (HKUnit factories that read better at call sites)

    private func bpmUnit() -> HKUnit {
        // HealthKit's heart rate unit is count/min.
        return HKUnit.count().unitDivided(by: HKUnit.minute())
    }

    private func vo2MaxUnit() -> HKUnit {
        // mL/(kg·min)
        return HKUnit(from: "mL/(kg.min)")
    }

    private func workoutName(_ type: HKWorkoutActivityType) -> String {
        switch type {
        case .walking:       return "Walking"
        case .running:       return "Running"
        case .cycling:       return "Cycling"
        case .swimming:      return "Swimming"
        case .traditionalStrengthTraining,
             .functionalStrengthTraining: return "Strength"
        case .yoga:          return "Yoga"
        case .highIntensityIntervalTraining: return "HIIT"
        case .rowing:        return "Rowing"
        case .elliptical:    return "Elliptical"
        case .stairClimbing: return "Stairs"
        case .hiking:        return "Hiking"
        default:             return "Workout"
        }
    }
}
