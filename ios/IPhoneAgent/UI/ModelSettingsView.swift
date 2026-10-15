import SwiftUI

/// Settings › Model: endpoint, key and the model picked from the endpoint's list; overrides under Advanced.
struct ModelSettingsView: View {
    @ObservedObject var app: AppModel
    @ObservedObject var settings: Settings
    @State private var testing = false
    @State private var advanced = false

    var body: some View {
        Form {
            Section {
                TextField("Base URL", text: $settings.baseURL, prompt: Text(verbatim: "https://example.com/v1"))
                    .keyboardType(.URL)
                SecureField("API key", text: $settings.apiKey, prompt: Text("API key (optional)"))
                ModelPicker(settings: settings, title: "Model", selection: $settings.model, automatic: ModelPicker.automatic(settings))
                Button { Task { await settings.loadModels() } } label: {
                    HStack {
                        Text("Refresh Model List")
                        Spacer()
                        if settings.loadingModels { ProgressView() }
                    }
                }
                .disabled(settings.loadingModels || !settings.isConfigured)
            } header: {
                Text("OpenAI-compatible API")
            } footer: {
                if let error = settings.modelsError {
                    Text("Could not load the model list: \(error) Type a model id instead.")
                } else {
                    Text("Your key stays in this iPhone's Keychain and is only sent to this endpoint.")
                }
            }
            Section {
                Button {
                    testing = true
                    Task { await app.pingLLM(); testing = false }
                } label: {
                    HStack {
                        Text("Test Connection")
                        Spacer()
                        if testing { ProgressView() } else if let ok = app.modelOK {
                            Image(systemName: ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                                .foregroundStyle(ok ? Palette.ok : Palette.stop)
                        }
                    }
                }
                .disabled(testing || !app.modelReady)
            }
            Section {
                DisclosureGroup("Advanced", isExpanded: $advanced) {
                    let same = String(localized: "Same as Model")
                    ModelPicker(settings: settings, title: "Planner model", selection: $settings.plannerModel, automatic: same)
                    ModelPicker(settings: settings, title: "Actor model", selection: $settings.actorModel, automatic: same)
                    Picker("Token parameter", selection: $settings.tokenParam) {
                        ForEach(["auto"] + LLMClient.tokenParams, id: \.self) { Text(verbatim: $0) }
                    }
                    TextField("Extra body JSON", text: $settings.extraBody, prompt: Text(verbatim: "{}"))
                        .font(.body.monospaced())
                }
            }
        }
        .textInputAutocapitalization(.never)
        .autocorrectionDisabled()
        .navigationTitle("Model")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: settings.baseURL + "\n" + settings.apiKey) {
            guard (try? await Task.sleep(nanoseconds: 600_000_000)) != nil else { return }  // typing: wait for a pause
            await settings.loadModels()
        }
    }
}

/// A model id: a menu of the endpoint's models when the list loaded, else a text field to type one.
/// "" is the automatic choice (`automatic` labels it).
struct ModelPicker: View {
    @ObservedObject var settings: Settings
    let title: LocalizedStringKey
    @Binding var selection: String
    let automatic: String

    static func automatic(_ settings: Settings) -> String {
        settings.models.first.map { String(localized: "Automatic (first: \($0))") } ?? String(localized: "Automatic")
    }

    var body: some View {
        if settings.models.isEmpty {
            LabeledContent {
                if settings.loadingModels && selection.isEmpty {
                    ProgressView()
                } else {
                    TextField(title, text: $selection, prompt: Text(verbatim: automatic))
                        .multilineTextAlignment(.trailing)
                }
            } label: { Text(title) }
        } else {
            Picker(title, selection: $selection) {
                Text(verbatim: automatic).tag("")
                if !selection.isEmpty && !settings.models.contains(selection) { Text(verbatim: selection).tag(selection) }
                ForEach(settings.models, id: \.self) { Text(verbatim: $0).tag($0) }
            }
            .pickerStyle(.menu)
        }
    }
}

/// Onboarding, after Test Connection: the model in use and a menu to change it.
struct PickedModelRow: View {
    @ObservedObject var settings: Settings

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("Model").font(.subheadline).foregroundStyle(.secondary)
                Text(verbatim: settings.currentModel ?? ModelPicker.automatic(settings)).font(.body.monospaced()).lineLimit(1)
            }
            Spacer()
            if settings.loadingModels {
                ProgressView()
            } else if !settings.models.isEmpty {
                Menu("Change") {
                    Picker("Model", selection: $settings.model) {
                        Text(verbatim: ModelPicker.automatic(settings)).tag("")
                        ForEach(settings.models, id: \.self) { Text(verbatim: $0).tag($0) }
                    }
                }
            }
        }
        .task { if settings.models.isEmpty { await settings.loadModels() } }
    }
}
