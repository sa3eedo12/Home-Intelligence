import Foundation

// User-editable configuration backed by UserDefaults so the app and the
// Shortcuts AppIntent share the same source of truth. Stored properties
// (with didSet to persist) so the @Observable macro can actually track
// changes — using computed UserDefaults accessors here would silently
// break SwiftUI re-rendering when the user moves a stepper.
@Observable
final class Settings {
    static let shared = Settings()

    private let defaults = UserDefaults.standard

    var orchestratorURL: String { didSet { defaults.set(orchestratorURL, forKey: Keys.url) } }
    var healthToken: String     { didSet { defaults.set(healthToken,     forKey: Keys.token) } }
    var memberId: Int?          { didSet { setOptionalInt(memberId, forKey: Keys.memberId) } }
    var windowMinutes: Int      { didSet { defaults.set(max(5, windowMinutes), forKey: Keys.windowMin) } }

    // Last-run status — surfaced in the UI so the user can confirm sync
    // health without digging through Console.app.
    var lastRunAt: Date?        { didSet { defaults.set(lastRunAt,        forKey: Keys.lastRunAt) } }
    var lastRunSummary: String? { didSet { defaults.set(lastRunSummary,   forKey: Keys.lastRunSummary) } }
    var lastRunWasError: Bool   { didSet { defaults.set(lastRunWasError,  forKey: Keys.lastRunErr) } }
    var lastInsertedCount: Int  { didSet { defaults.set(lastInsertedCount, forKey: Keys.lastInserted) } }
    var lastMetricsList: [String] {
        didSet { defaults.set(lastMetricsList, forKey: Keys.lastMetricsList) }
    }

    private init() {
        self.orchestratorURL = defaults.string(forKey: Keys.url) ?? ""
        self.healthToken     = defaults.string(forKey: Keys.token) ?? ""
        let mid = defaults.integer(forKey: Keys.memberId)
        self.memberId        = mid > 0 ? mid : nil
        let win = defaults.integer(forKey: Keys.windowMin)
        self.windowMinutes   = win > 0 ? win : 60
        self.lastRunAt       = defaults.object(forKey: Keys.lastRunAt) as? Date
        self.lastRunSummary  = defaults.string(forKey: Keys.lastRunSummary)
        self.lastRunWasError = defaults.bool(forKey: Keys.lastRunErr)
        self.lastInsertedCount = defaults.integer(forKey: Keys.lastInserted)
        self.lastMetricsList = defaults.stringArray(forKey: Keys.lastMetricsList) ?? []
    }

    private func setOptionalInt(_ value: Int?, forKey key: String) {
        if let value = value, value > 0 { defaults.set(value, forKey: key) }
        else                            { defaults.removeObject(forKey: key) }
    }

    private enum Keys {
        static let url             = "orchestratorURL"
        static let token           = "healthToken"
        static let memberId        = "memberId"
        static let windowMin       = "windowMinutes"
        static let lastRunAt       = "lastRunAt"
        static let lastRunSummary  = "lastRunSummary"
        static let lastRunErr      = "lastRunWasError"
        static let lastInserted    = "lastInsertedCount"
        static let lastMetricsList = "lastMetricsList"
    }
}

// MARK: - Window presets

/// User-facing presets for the lookback window. Picker shows these labels;
/// each maps to a fixed minute count. Avoids the "is 60 minutes 1 hour or 60s?"
/// confusion of a raw stepper.
enum WindowPreset: Int, CaseIterable, Identifiable {
    case min15  = 15
    case min30  = 30
    case hr1    = 60
    case hr3    = 180
    case hr6    = 360
    case hr12   = 720
    case hr24   = 1440
    case day3   = 4320
    case day7   = 10080

    var id: Int { rawValue }
    var minutes: Int { rawValue }

    var label: String {
        switch self {
        case .min15: return "15 minutes"
        case .min30: return "30 minutes"
        case .hr1:   return "1 hour"
        case .hr3:   return "3 hours"
        case .hr6:   return "6 hours"
        case .hr12:  return "12 hours"
        case .hr24:  return "1 day"
        case .day3:  return "3 days"
        case .day7:  return "1 week"
        }
    }

    /// Returns the preset whose minute count matches `minutes` exactly, or
    /// the closest one if there's no exact match.
    static func closest(to minutes: Int) -> WindowPreset {
        if let exact = WindowPreset(rawValue: minutes) { return exact }
        return WindowPreset.allCases.min(by: { abs($0.minutes - minutes) < abs($1.minutes - minutes) }) ?? .hr1
    }
}

// MARK: - What we read from HealthKit

/// Catalog of metric categories the app reads. Powers the "What gets
/// uploaded" section in the UI so the user can see exactly what's
/// included before they grant permission.
struct MetricCategory: Identifiable {
    let id: String
    let label: String
    let detail: String
    let symbol: String   // SF Symbol name
}

enum MetricCatalog {
    static let all: [MetricCategory] = [
        MetricCategory(id: "steps",           label: "Steps",           detail: "Sum over the lookback window",     symbol: "figure.walk"),
        MetricCategory(id: "active_energy",   label: "Active Energy",   detail: "Calories burned, sum",             symbol: "flame.fill"),
        MetricCategory(id: "heart_rate",      label: "Heart Rate",      detail: "Average over the window",          symbol: "heart.fill"),
        MetricCategory(id: "resting_hr",      label: "Resting Heart Rate", detail: "Most recent reading (24h)",     symbol: "waveform.path.ecg"),
        MetricCategory(id: "hrv",             label: "Heart Rate Variability", detail: "Most recent SDNN (24h)",    symbol: "waveform"),
        MetricCategory(id: "weight",          label: "Body Mass",       detail: "Most recent reading (last 7 days)", symbol: "scalemass.fill"),
        MetricCategory(id: "blood_oxygen",    label: "Blood Oxygen",    detail: "Most recent SpO₂ (24h)",            symbol: "lungs.fill"),
        MetricCategory(id: "vo2_max",         label: "VO₂ Max",         detail: "Most recent reading (last 30 days)", symbol: "figure.run"),
        MetricCategory(id: "sleep",           label: "Sleep",           detail: "Sum of asleep stages (last 24h)",   symbol: "bed.double.fill"),
        MetricCategory(id: "workouts",        label: "Workouts",        detail: "Walking, Running, Cycling, etc that ended in the window", symbol: "dumbbell.fill"),
    ]
}
