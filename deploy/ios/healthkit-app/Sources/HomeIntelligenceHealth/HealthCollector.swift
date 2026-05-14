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
        async let sleepData    = sleepAsleep(start: now.addingTimeInterval(-86400), end: now)
        async let workouts     = recentWorkouts(start: windowStart, end: now)

        // Convert percent (HealthKit returns 0.0-1.0) into the 0-100 % the
        // orchestrator's normalizer prefers.
        let bo = try await bloodOxygen
        let oxygenPct = bo.map { $0 * 100 }

        let (asleepMin, sleepWindow) = try await sleepData
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
            sleepAsleepMin:   asleepMin,
            sleepWindow:      sleepWindow,
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

    /// Returns (sumOfAsleepMinutes, optional aggregate window).
    private func sleepAsleep(start: Date, end: Date) async throws -> (Double?, SleepWindow?) {
        guard let type = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) else {
            return (nil, nil)
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

        // Sum durations of "asleep"-class samples — but be careful about
        // double-counting: watchOS 9+ emits BOTH a legacy `.asleep` sample
        // covering the whole sleep period AND fine-grained per-stage samples
        // (`.asleepCore`, `.asleepDeep`, `.asleepREM`) for the SAME period.
        // Naively summing them all reports ~2x the real sleep time.
        //
        // Strategy: if any stage-specific sample exists in the window, ignore
        // the legacy `.asleep` value entirely (the stages cover the same
        // ground in finer detail). Otherwise count `.asleep` (older devices /
        // manually-logged sleep without a watch).
        let hasStagedSamples = samples.contains { sample in
            guard let v = HKCategoryValueSleepAnalysis(rawValue: sample.value) else { return false }
            switch v {
            case .asleepCore, .asleepDeep, .asleepREM, .asleepUnspecified:
                return true
            default:
                return false
            }
        }
        var asleepSeconds: TimeInterval = 0
        var earliest: Date?
        var latest: Date?
        for s in samples {
            guard let value = HKCategoryValueSleepAnalysis(rawValue: s.value) else { continue }
            if !isAsleepStage(value) { continue }
            // Skip the legacy bucket if we have staged data — the stages
            // already account for the same minutes more precisely.
            if value == .asleep && hasStagedSamples { continue }
            asleepSeconds += s.endDate.timeIntervalSince(s.startDate)
            if earliest == nil || s.startDate < earliest! { earliest = s.startDate }
            if latest == nil   || s.endDate   > latest!   { latest   = s.endDate }
        }
        let minutes = asleepSeconds / 60.0
        if minutes <= 0 { return (nil, nil) }
        let window = (earliest != nil && latest != nil)
            ? SleepWindow(start: earliest!, end: latest!, asleepMin: minutes)
            : nil
        return (minutes, window)
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

    // MARK: - Stage classification

    private func isAsleepStage(_ value: HKCategoryValueSleepAnalysis) -> Bool {
        // .inBed and .awake are NOT asleep. Everything else counts. Default
        // to "asleep" for new enum values rather than discarding them.
        switch value {
        case .inBed, .awake:
            return false
        case .asleep, .asleepCore, .asleepDeep, .asleepREM, .asleepUnspecified:
            return true
        @unknown default:
            return true
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
