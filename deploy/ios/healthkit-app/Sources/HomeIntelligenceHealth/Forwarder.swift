import Foundation

enum ForwardError: LocalizedError {
    case retriable(status: Int, body: String)   // Shortcuts/user can retry
    case permanent(status: Int, body: String)
    case network(Error)
    case missingConfig(String)

    var errorDescription: String? {
        switch self {
        case .retriable(let status, let body):
            return "Server returned HTTP \(status) (will retry next run): \(body.prefix(160))"
        case .permanent(let status, let body):
            return "Server rejected the upload (HTTP \(status)): \(body.prefix(160))"
        case .network(let underlying):
            return "Couldn't reach the orchestrator: \(underlying.localizedDescription)"
        case .missingConfig(let key):
            return "Settings missing: \(key) — open Home Intelligence Health and fill it in."
        }
    }
}

enum Forwarder {
    static func post(payload: Payload, settings: Settings) async throws -> String {
        guard let baseURLString = settings.orchestratorURL,
              let base = URL(string: baseURLString),
              base.scheme != nil
        else {
            throw ForwardError.missingConfig("Orchestrator URL")
        }
        guard let token = settings.healthToken, !token.isEmpty else {
            throw ForwardError.missingConfig("HealthKit token")
        }

        var url = base.appendingPathComponent("admin/healthkit/sync")
        if let memberId = settings.memberId {
            var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
            components.queryItems = [URLQueryItem(name: "member_id", value: String(memberId))]
            url = components.url ?? url
        }

        var request = URLRequest(url: url, timeoutInterval: 30)
        request.httpMethod = "POST"
        request.setValue("application/json",                       forHTTPHeaderField: "Content-Type")
        request.setValue(token,                                    forHTTPHeaderField: "X-Health-Token")
        request.setValue("home-intelligence-healthkit-ios/1.0",    forHTTPHeaderField: "User-Agent")
        request.httpBody = payload.body

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw ForwardError.network(error)
        }

        guard let http = response as? HTTPURLResponse else {
            throw ForwardError.network(NSError(
                domain: "HomeIntelligenceHealth", code: 0,
                userInfo: [NSLocalizedDescriptionKey: "non-HTTP response"]))
        }
        let body = String(data: data, encoding: .utf8) ?? ""
        if (200..<300).contains(http.statusCode) { return body }
        if http.statusCode >= 500 || [408, 425, 429].contains(http.statusCode) {
            throw ForwardError.retriable(status: http.statusCode, body: body)
        }
        throw ForwardError.permanent(status: http.statusCode, body: body)
    }
}
