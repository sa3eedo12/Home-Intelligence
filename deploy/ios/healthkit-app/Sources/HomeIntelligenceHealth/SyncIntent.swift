import AppIntents

// Exposes "Sync Health to Home Intelligence" as a callable action in the
// iOS Shortcuts app. Combined with a Personal Automation (Time of Day →
// Run Shortcut) this gives you reliable scheduled sync without any
// background-task code or asking the OS to wake the app on its own
// (BGTaskScheduler is famously unreliable).
//
// Setup the user does once on iPhone:
//   1. Open Shortcuts.app
//   2. + → New Automation → Time of Day → e.g. every hour at :00
//   3. Add Action → search "Sync Health to Home Intelligence"
//   4. Toggle "Run Immediately" ON, "Notify When Run" OFF
//
// When the automation fires, iOS launches our app in the background just
// long enough to run this intent.
struct SyncHealthIntent: AppIntent {
    static var title: LocalizedStringResource = "Sync Health to Home Intelligence"
    static var description = IntentDescription(
        "Reads recent Apple Health samples and uploads them to your Home Intelligence orchestrator."
    )

    // Lets the user run this from the Shortcuts app without first opening
    // ours. iOS still launches us in the background to execute.
    static var openAppWhenRun: Bool = false

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let summary = await SyncCoordinator.runOnce()
        return .result(value: summary, dialog: IntentDialog(stringLiteral: summary))
    }
}

/// Surfaces our intent in the system shortcuts gallery so the user doesn't
/// have to manually search for it.
struct AppShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: SyncHealthIntent(),
            phrases: [
                "Sync Health with \(.applicationName)",
                "Run \(.applicationName) sync",
            ],
            shortTitle: "Sync Health",
            systemImageName: "heart.text.square"
        )
    }
}
