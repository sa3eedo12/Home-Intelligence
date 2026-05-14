import SwiftUI

// SwiftUI App entry point. Single-window app with the settings/UI screen.
// Background sync is handled by the AppIntent + an iOS Personal Automation
// (see SyncIntent.swift); we don't register a BGTaskScheduler task because
// iOS aggressively defers/skips those, while Shortcuts-driven automations
// fire reliably.
@main
struct HomeIntelligenceHealthApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
