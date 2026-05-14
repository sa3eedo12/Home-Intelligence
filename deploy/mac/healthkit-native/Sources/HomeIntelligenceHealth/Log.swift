import Foundation
import os

// Logs go to ~/Library/Logs/HomeIntelligenceHealth.log AND to stderr so
// `Console.app`, `tail`, and the LaunchAgent's StandardErrorPath all see
// them. Single-line, no rotation — launchd is expected to recycle the file
// (or you can ship logrotate yourself).
enum Log {
    private static let url: URL = {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true
        )
        return dir.appendingPathComponent("HomeIntelligenceHealth.log")
    }()

    private static let formatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    static func info(_ message: String)  { write("INFO",  message) }
    static func warn(_ message: String)  { write("WARN",  message) }
    static func error(_ message: String) { write("ERROR", message) }

    private static func write(_ level: String, _ message: String) {
        let line = "\(formatter.string(from: Date())) \(level) \(message)\n"
        FileHandle.standardError.write(line.data(using: .utf8) ?? Data())
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(line.data(using: .utf8) ?? Data())
            try? handle.close()
        } else {
            try? line.data(using: .utf8)?.write(to: url, options: .atomic)
        }
    }
}
