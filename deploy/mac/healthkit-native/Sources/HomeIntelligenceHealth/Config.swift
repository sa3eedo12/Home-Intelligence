import Foundation

struct Config {
    let url: URL
    let token: String
    let memberId: Int?
    let windowMinutes: Int
    let timeout: TimeInterval

    enum Error: Swift.Error, LocalizedError {
        case missingEnvVar(String)
        case invalidURL(String)
        case invalidInt(String, String)

        var errorDescription: String? {
            switch self {
            case .missingEnvVar(let name):
                return "missing required env var: \(name)"
            case .invalidURL(let raw):
                return "ORCHESTRATOR_URL is not a valid URL: \(raw)"
            case .invalidInt(let name, let raw):
                return "\(name) is not a valid integer: \(raw)"
            }
        }
    }

    static func load(env: [String: String] = ProcessInfo.processInfo.environment) throws -> Config {
        guard let urlString = env["ORCHESTRATOR_URL"], !urlString.isEmpty else {
            throw Error.missingEnvVar("ORCHESTRATOR_URL")
        }
        guard let token = env["HEALTHKIT_TOKEN"], !token.isEmpty else {
            throw Error.missingEnvVar("HEALTHKIT_TOKEN")
        }
        guard let url = URL(string: urlString) else {
            throw Error.invalidURL(urlString)
        }

        var memberId: Int? = nil
        if let raw = env["MEMBER_ID"], !raw.isEmpty {
            guard let parsed = Int(raw) else {
                throw Error.invalidInt("MEMBER_ID", raw)
            }
            memberId = parsed
        }

        let windowMinutes: Int
        if let raw = env["WINDOW_MINUTES"], !raw.isEmpty {
            guard let parsed = Int(raw), parsed > 0 else {
                throw Error.invalidInt("WINDOW_MINUTES", raw)
            }
            windowMinutes = parsed
        } else {
            windowMinutes = 60
        }

        let timeout: TimeInterval
        if let raw = env["REQUEST_TIMEOUT"], !raw.isEmpty {
            guard let parsed = Double(raw), parsed > 0 else {
                throw Error.invalidInt("REQUEST_TIMEOUT", raw)
            }
            timeout = parsed
        } else {
            timeout = 30
        }

        return Config(
            url: url, token: token, memberId: memberId,
            windowMinutes: windowMinutes, timeout: timeout
        )
    }
}
