import CoreGraphics
import AppKit

// MARK: - Screenshot Capture

enum ScreenshotCapture {

    /// Capture a specific window by its CGWindowID.
    static func captureWindow(windowID: CGWindowID) -> CGImage? {
        CGWindowListCreateImage(
            .infinite,
            .optionIncludingWindow,
            windowID,
            .bestResolution
        )
    }

    /// Capture the entire screen (fallback when window capture fails).
    static func captureScreen() -> CGImage? {
        CGWindowListCreateImage(
            .infinite,
            .optionOnScreenOnly,
            kCGNullWindowID,
            .bestResolution
        )
    }

    /// Save a CGImage as PNG to the given path.
    static func savePNG(_ image: CGImage, to path: String) -> Bool {
        let bitmapRep = NSBitmapImageRep(cgImage: image)
        guard let pngData = bitmapRep.representation(using: .png, properties: [:]) else {
            logError("Failed to convert CGImage to PNG data")
            return false
        }
        do {
            try pngData.write(to: URL(fileURLWithPath: path))
            return true
        } catch {
            logError("Failed to write PNG to \(path): \(error)")
            return false
        }
    }

    /// Take a screenshot of Lark's window and save to /tmp.
    /// Returns the file path on success, nil on failure.
    static func takeAndSave(windowID: CGWindowID?, prefix: String = "feishu-native-rpa") -> String? {
        let image: CGImage?
        if let wid = windowID {
            image = captureWindow(windowID: wid) ?? captureScreen()
        } else {
            image = captureScreen()
        }

        guard let img = image else {
            logError("Screenshot capture returned nil — Screen Recording permission may be missing")
            return nil
        }

        let timestamp = Int(Date().timeIntervalSince1970)
        let path = "/tmp/\(prefix)-\(timestamp).png"
        if savePNG(img, to: path) {
            logInfo("Screenshot saved: \(path)")
            return path
        }
        return nil
    }
}
