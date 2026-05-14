import Foundation
import HealthKit

// Read-only access to the HealthKit sample types we care about. macOS
// HealthKit only requires authorization for the types we actually query;
// keeping the list tight minimizes the prompt the user sees on first run.
enum Authorization {
    static func readTypes() -> Set<HKObjectType> {
        var types: Set<HKObjectType> = []
        types.insert(HKObjectType.workoutType())
        for id in HealthCollector.quantityTypeIds {
            if let t = HKObjectType.quantityType(forIdentifier: id) { types.insert(t) }
        }
        types.insert(HKObjectType.categoryType(forIdentifier: .sleepAnalysis)!)
        return types
    }

    // HKHealthStore.requestAuthorization is callback-based; wrap in async/await.
    // First-run behavior on macOS: a system dialog asks the user which sample
    // types this app may read. Subsequent runs short-circuit silently.
    static func request(store: HKHealthStore) async throws {
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            store.requestAuthorization(toShare: [], read: readTypes()) { success, error in
                if let error = error {
                    cont.resume(throwing: error)
                    return
                }
                if !success {
                    cont.resume(throwing: NSError(
                        domain: "HomeIntelligenceHealth", code: 1,
                        userInfo: [NSLocalizedDescriptionKey:
                            "HealthKit authorization request returned success=false"]))
                    return
                }
                cont.resume(returning: ())
            }
        }
    }
}
