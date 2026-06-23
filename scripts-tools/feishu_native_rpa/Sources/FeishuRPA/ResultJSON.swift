import Foundation

// MARK: - RPA Result

struct RPAResult: Codable {
    let ok: Bool
    let method: String
    let target: String
    let text: String
    let screenshot: String?
    let verifiedInAX: Bool
    let durationMs: Int
    let error: String?

    // Snake-case JSON keys for compatibility with existing Playwright script output
    enum CodingKeys: String, CodingKey {
        case ok, method, target, text, screenshot, error
        case verifiedInAX = "verified_in_ax"
        case durationMs = "duration_ms"
    }

    static func success(
        target: String,
        text: String,
        screenshot: String?,
        verified: Bool,
        durationMs: Int
    ) -> RPAResult {
        RPAResult(
            ok: true,
            method: "rpa-native-accessibility",
            target: target,
            text: text,
            screenshot: screenshot,
            verifiedInAX: verified,
            durationMs: durationMs,
            error: nil
        )
    }

    static func failure(
        target: String,
        text: String,
        error: String,
        screenshot: String? = nil,
        durationMs: Int = 0
    ) -> RPAResult {
        RPAResult(
            ok: false,
            method: "rpa-native-accessibility",
            target: target,
            text: text,
            screenshot: screenshot,
            verifiedInAX: false,
            durationMs: durationMs,
            error: error
        )
    }
}

// MARK: - Exit Codes

enum ExitCode: Int32 {
    case success = 0
    case generalFailure = 1
    case accessibilityNotGranted = 2
    case larkNotRunning = 3
    case chatNotFound = 4
    case sendVerificationFailed = 5
    case screenshotFailed = 6
}

func outputAndExit(_ result: RPAResult, code: ExitCode) -> Never {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    if let data = try? encoder.encode(result),
       let json = String(data: data, encoding: .utf8) {
        print(json)
    }
    exit(code.rawValue)
}
