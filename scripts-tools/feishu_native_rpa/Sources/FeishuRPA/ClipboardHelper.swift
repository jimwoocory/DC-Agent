import AppKit

// MARK: - Clipboard Helper

enum ClipboardHelper {

    /// Copy text to the system pasteboard.
    static func copyToClipboard(_ text: String) {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text, forType: .string)
    }

    /// Paste from the clipboard using Cmd+V.
    static func pasteFromClipboard(targetPID: pid_t? = nil) {
        InputSimulator.postKeyCombo(
            keyCode: 0x09, // kVK_ANSI_V
            modifiers: .maskCommand,
            toPID: targetPID
        )
    }

    /// Copy text to clipboard and paste it via Cmd+V.
    /// This is the primary way to input Chinese/Unicode text,
    /// since CGEvent keyboard events only support virtual key codes.
    static func pasteText(_ text: String, targetPID: pid_t? = nil) {
        copyToClipboard(text)
        // Small delay to ensure pasteboard is ready
        Thread.sleep(forTimeInterval: 0.1)
        pasteFromClipboard(targetPID: targetPID)
    }
}
