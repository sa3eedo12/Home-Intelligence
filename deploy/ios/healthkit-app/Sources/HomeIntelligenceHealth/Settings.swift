import Foundation

// User-editable configuration backed by UserDefaults so the app and the
// Shortcuts AppIntent share the same source of truth. Changes from the
// SettingsView are picked up by the next SyncIntent run automatically.
@Observable
final class Settings {
    static let shared = Settings()

    private let defaults = UserDefaults.standard

    var orchestratorURL: String? {
        get { defaults.string(forKey: Keys.url) }
        set { defaults.set(newValue, forKey: Keys.url) }
    }

    var healthToken: String? {
        get { defaults.string(forKey: Keys.token) }
        set { defaults.set(newValue, forKey: Keys.token) }
    }

    var memberId: Int? {
        get {
            let v = defaults.integer(forKey: Keys.memberId)
            return v > 0 ? v : nil
        }
        set {
            if let v = newValue, v > 0 { defaults.set(v, forKey: Keys.memberId) }
            else                       { defaults.removeObject(forKey: Keys.memberId) }
        }
    }

    var windowMinutes: Int {
        get {
            let v = defaults.integer(forKey: Keys.windowMin)
            return v > 0 ? v : 60
        }
        set { defaults.set(max(5, newValue), forKey: Keys.windowMin) }
    }

    // Last-run status — surfaced in the UI so the user can confirm sync
    // health without digging through Console.app.
    var lastRunAt: Date? {
        get { defaults.object(forKey: Keys.lastRunAt) as? Date }
        set { defaults.set(newValue, forKey: Keys.lastRunAt) }
    }

    var lastRunSummary: String? {
        get { defaults.string(forKey: Keys.lastRunSummary) }
        set { defaults.set(newValue, forKey: Keys.lastRunSummary) }
    }

    var lastRunWasError: Bool {
        get { defaults.bool(forKey: Keys.lastRunErr) }
        set { defaults.set(newValue, forKey: Keys.lastRunErr) }
    }

    private enum Keys {
        static let url            = "orchestratorURL"
        static let token          = "healthToken"
        static let memberId       = "memberId"
        static let windowMin      = "windowMinutes"
        static let lastRunAt      = "lastRunAt"
        static let lastRunSummary = "lastRunSummary"
        static let lastRunErr     = "lastRunWasError"
    }
}
