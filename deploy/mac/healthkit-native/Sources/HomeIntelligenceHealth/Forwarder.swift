import Foundation

enum ForwardError: Error {
    case retriable(status: Int, body: String)   // launchd retries next interval
    case permanent(status: Int, body: String)
    case network(Error)
}

enum Forwarder {
    static func post(payload: Payload, config: Config) async throws {
        var url = config.url.appendingPathComponent("admin/healthkit/sync")
        if let memberId = config.memberId {
            var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
            components.queryItems = [URLQueryItem(name: "member_id", value: String(memberId))]
            url = components.url ?? url
        }

        var request = URLRequest(url: url, timeoutInterval: config.timeout)
        request.httpMethod = "POST"
        request.setValue("application/json",                     forHTTPHeaderField: "Content-Type")
        request.setValue(config.token,                           forHTTPHeaderField: "X-Health-Token")
        request.setValue("home-intelligence-healthkit-native/1.0",
                         forHTTPHeaderField: "User-Agent")
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
        if (200..<300).contains(http.statusCode) {
            Log.info("orchestrator accepted: \(body.prefix(200))")
            return
        }
        if http.statusCode >= 500 || [408, 425, 429].contains(http.statusCode) {
            throw ForwardError.retriable(status: http.statusCode, body: body)
        }
        throw ForwardError.permanent(status: http.statusCode, body: body)
    }
}
