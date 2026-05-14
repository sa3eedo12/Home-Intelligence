import SwiftUI

// Minimal one-screen UI: settings (URL, token, optional member id, window
// length) plus a "Sync now" button and the last-run status. The actual
// sync logic lives in SyncCoordinator so the AppIntent can reuse it.
struct ContentView: View {
    @State private var settings = Settings.shared
    @State private var isSyncing = false
    @State private var liveSummary: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("TrueNAS orchestrator") {
                    LabeledTextField(
                        title: "URL",
                        placeholder: "http://192.168.1.190:8080",
                        text: Binding(
                            get: { settings.orchestratorURL ?? "" },
                            set: { settings.orchestratorURL = $0 }
                        ),
                        keyboard: .URL,
                        autocap: false
                    )
                    LabeledSecureField(
                        title: "X-Health-Token",
                        placeholder: "matches HEALTHKIT_WEBHOOK_TOKEN",
                        text: Binding(
                            get: { settings.healthToken ?? "" },
                            set: { settings.healthToken = $0 }
                        )
                    )
                    LabeledTextField(
                        title: "Member ID (optional)",
                        placeholder: "1",
                        text: Binding(
                            get: { settings.memberId.map(String.init) ?? "" },
                            set: { settings.memberId = Int($0) }
                        ),
                        keyboard: .numberPad,
                        autocap: false
                    )
                }

                Section("Sync window") {
                    Stepper(
                        "Look back \(settings.windowMinutes) min each run",
                        value: Binding(
                            get: { settings.windowMinutes },
                            set: { settings.windowMinutes = $0 }
                        ),
                        in: 5...24*60, step: 5
                    )
                    Text("Set this to be at least as long as the gap between runs so you don't miss data between syncs.")
                        .font(.caption).foregroundStyle(.secondary)
                }

                Section("Sync") {
                    Button {
                        Task { await syncNow() }
                    } label: {
                        if isSyncing {
                            HStack { ProgressView(); Text("Syncing…") }
                        } else {
                            Label("Sync now", systemImage: "arrow.triangle.2.circlepath")
                        }
                    }
                    .disabled(isSyncing)

                    if let summary = liveSummary ?? settings.lastRunSummary {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(summary)
                                .font(.callout)
                                .foregroundStyle(settings.lastRunWasError ? .red : .primary)
                            if let when = settings.lastRunAt {
                                Text("Last run: \(when, style: .relative) ago")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }

                Section("Schedule it") {
                    Text("Open the Shortcuts app → Automation tab → + → Personal Automation → Time of Day → Add 'Sync Health to Home Intelligence' as the action. Toggle 'Run Immediately' on so it fires without asking.")
                        .font(.callout)
                }
            }
            .navigationTitle("Home Intelligence Health")
        }
    }

    @MainActor
    private func syncNow() async {
        isSyncing = true
        liveSummary = nil
        let summary = await SyncCoordinator.runOnce()
        liveSummary = summary
        isSyncing = false
    }
}

private struct LabeledTextField: View {
    let title: String
    let placeholder: String
    @Binding var text: String
    var keyboard: UIKeyboardType = .default
    var autocap: Bool = true

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            TextField(placeholder, text: $text)
                .keyboardType(keyboard)
                .autocapitalization(autocap ? .sentences : .none)
                .disableAutocorrection(true)
        }
    }
}

private struct LabeledSecureField: View {
    let title: String
    let placeholder: String
    @Binding var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            SecureField(placeholder, text: $text)
        }
    }
}

#Preview {
    ContentView()
}
