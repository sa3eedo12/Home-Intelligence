import SwiftUI

// One-screen settings + sync UI. Uses @Bindable so SwiftUI re-renders
// whenever the @Observable Settings instance mutates — this is the fix
// for "the stepper doesn't update the label".
struct ContentView: View {
    @Bindable private var settings = Settings.shared
    @State private var isSyncing = false
    @State private var liveSummary: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("TrueNAS orchestrator") {
                    LabeledContent("URL") {
                        TextField("http://192.168.1.190:8080", text: $settings.orchestratorURL)
                            .keyboardType(.URL)
                            .autocapitalization(.none)
                            .disableAutocorrection(true)
                            .multilineTextAlignment(.trailing)
                    }
                    LabeledContent("Token") {
                        SecureField("X-Health-Token", text: $settings.healthToken)
                            .multilineTextAlignment(.trailing)
                    }
                    LabeledContent("Member ID") {
                        TextField("optional", text: Binding(
                            get: { settings.memberId.map(String.init) ?? "" },
                            set: { settings.memberId = Int($0) }
                        ))
                        .keyboardType(.numberPad)
                        .multilineTextAlignment(.trailing)
                    }
                }

                Section("Sync window") {
                    Picker("Look back", selection: Binding(
                        get: { WindowPreset.closest(to: settings.windowMinutes) },
                        set: { settings.windowMinutes = $0.minutes }
                    )) {
                        ForEach(WindowPreset.allCases) { preset in
                            Text(preset.label).tag(preset)
                        }
                    }
                    .pickerStyle(.menu)

                    Text("Each sync reads Health samples from the last \(WindowPreset.closest(to: settings.windowMinutes).label.lowercased()). Set this to be at least as long as the gap between automation runs so you don't miss data.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Section {
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
                        VStack(alignment: .leading, spacing: 6) {
                            Text(summary)
                                .font(.callout)
                                .foregroundStyle(settings.lastRunWasError ? .red : .primary)
                            if let when = settings.lastRunAt {
                                Text("Last run \(when, style: .relative) ago")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                            if !settings.lastMetricsList.isEmpty {
                                FlowLabels(items: settings.lastMetricsList)
                            }
                        }
                    }
                } header: {
                    Text("Sync")
                }

                Section {
                    ForEach(MetricCatalog.all) { cat in
                        HStack(alignment: .top) {
                            Image(systemName: cat.symbol)
                                .frame(width: 24)
                                .foregroundStyle(.tint)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(cat.label).font(.body)
                                Text(cat.detail)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                } header: {
                    Text("What gets uploaded each run")
                } footer: {
                    Text("Categories with no recent samples are silently skipped — they're not zeroed on the server. Permission is requested on first sync.")
                }

                Section {
                    Text("Open the **Shortcuts** app → Automation tab → + → Personal Automation → Time of Day → choose a recurrence (e.g. every hour) → add **'Sync Health to Home Intelligence'** as the action → toggle 'Run Immediately' on so it fires without asking.")
                        .font(.callout)
                } header: {
                    Text("Schedule it")
                }
            }
            .navigationTitle("HI Health")
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

/// Wraps a list of short labels (e.g. "Steps", "Heart Rate") in pill-style
/// chips that wrap to multiple lines if needed. SwiftUI doesn't have a
/// built-in flow layout pre-iOS 16 — using a simple LazyVGrid is fine for
/// our short fixed lists.
private struct FlowLabels: View {
    let items: [String]

    var body: some View {
        let columns = [GridItem(.adaptive(minimum: 90), spacing: 6)]
        LazyVGrid(columns: columns, alignment: .leading, spacing: 6) {
            ForEach(items, id: \.self) { label in
                Text(label)
                    .font(.caption)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(.tint.opacity(0.15), in: Capsule())
                    .foregroundStyle(.tint)
            }
        }
    }
}

#Preview {
    ContentView()
}
