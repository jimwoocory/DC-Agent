import CoreGraphics
import AppKit

// MARK: - Virtual Key Codes

enum KeyCode {
    static let returnKey: CGKeyCode = 0x24
    static let escape: CGKeyCode = 0x35
    static let tab: CGKeyCode = 0x30
    static let space: CGKeyCode = 0x31
    static let deleteForward: CGKeyCode = 0x75
    static let deleteBack: CGKeyCode = 0x33
    static let ansA: CGKeyCode = 0x00
    static let ansK: CGKeyCode = 0x28
    static let ansV: CGKeyCode = 0x09
}

// MARK: - Input Simulator

enum InputSimulator {

    /// Post a key combo (key down + key up) to a specific process or globally.
    static func postKeyCombo(
        keyCode: CGKeyCode,
        modifiers: CGEventFlags = [],
        toPID: pid_t? = nil
    ) {
        guard let source = CGEventSource(stateID: .hidSystemState) else {
            logError("Failed to create CGEventSource")
            return
        }

        let flags: CGEventFlags = modifiers.intersection(.maskCommand, .maskShift, .maskAlternate, .maskControl)

        guard let keyDown = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: true),
              let keyUp = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: false) else {
            logError("Failed to create CGEvent for key \(keyCode)")
            return
        }

        keyDown.flags = flags
        keyUp.flags = flags

        if let pid = toPID {
            keyDown.postToPid(pid)
            keyUp.postToPid(pid)
        } else {
            keyDown.post(tap: .cgSessionEventTap)
            keyUp.post(tap: .cgSessionEventTap)
        }
    }

    /// Press Return/Enter key.
    static func pressReturn(toPID: pid_t? = nil) {
        postKeyCombo(keyCode: KeyCode.returnKey, toPID: toPID)
    }

    /// Press Escape key.
    static func pressEscape(toPID: pid_t? = nil) {
        postKeyCombo(keyCode: KeyCode.escape, toPID: toPID)
    }

    /// Press Cmd+A (select all).
    static func selectAll(toPID: pid_t? = nil) {
        postKeyCombo(keyCode: KeyCode.ansA, modifiers: .maskCommand, toPID: toPID)
    }

    /// Press Cmd+K (Lark global search).
    static func openSearch(toPID: pid_t? = nil) {
        postKeyCombo(keyCode: KeyCode.ansK, modifiers: .maskCommand, toPID: toPID)
    }

    /// Press Cmd+V (paste).
    static func paste(toPID: pid_t? = nil) {
        postKeyCombo(keyCode: KeyCode.ansV, modifiers: .maskCommand, toPID: toPID)
    }

    /// Press Delete/Backspace.
    static func pressDelete(toPID: pid_t? = nil) {
        postKeyCombo(keyCode: KeyCode.deleteBack, toPID: toPID)
    }

    /// Type ASCII text character by character via CGEvent.
    /// WARNING: Does NOT support Chinese/CJK characters.
    /// For Unicode text, use ClipboardHelper.pasteText() instead.
    static func typeASCII(_ text: String, toPID: pid_t? = nil) {
        guard let source = CGEventSource(stateID: .hidSystemState) else { return }
        for char in text.unicodeScalars {
            guard let keyCode = asciiToKeyCode[char.value] else { continue }
            let shift = char.value >= 0x41 && char.value <= 0x5A // uppercase A-Z
            let modifiers: CGEventFlags = shift ? .maskShift : []

            if let down = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: true),
               let up = CGEvent(keyboardEventSource: source, virtualKey: keyCode, keyDown: false) {
                down.flags = modifiers
                up.flags = modifiers
                if let pid = toPID {
                    down.postToPid(pid)
                    up.postToPid(pid)
                } else {
                    down.post(tap: .cgSessionEventTap)
                    up.post(tap: .cgSessionEventTap)
                }
                Thread.sleep(forTimeInterval: 0.03)
            }
        }
    }

    /// Post a mouse click at screen coordinates.
    static func clickAt(x: CGFloat, y: CGFloat, toPID: pid_t? = nil) {
        let point = CGPoint(x: x, y: y)
        guard let source = CGEventSource(stateID: .hidSystemState) else { return }

        if let mouseDown = CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left),
           let mouseUp = CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left) {
            if let pid = toPID {
                mouseDown.postToPid(pid)
                mouseUp.postToPid(pid)
            } else {
                mouseDown.post(tap: .cgSessionEventTap)
                mouseUp.post(tap: .cgSessionEventTap)
            }
        }
    }

    // MARK: - ASCII Key Code Map

    private static let asciiToKeyCode: [UInt32: CGKeyCode] = [
        0x61: 0x00, // a
        0x62: 0x0B, // b
        0x63: 0x08, // c
        0x64: 0x02, // d
        0x65: 0x0E, // e
        0x66: 0x03, // f
        0x67: 0x05, // g
        0x68: 0x04, // h
        0x69: 0x22, // i
        0x6A: 0x26, // j
        0x6B: 0x28, // k
        0x6C: 0x25, // l
        0x6D: 0x2E, // m
        0x6E: 0x2D, // n
        0x6F: 0x1F, // o
        0x70: 0x23, // p
        0x71: 0x0C, // q
        0x72: 0x0F, // r
        0x73: 0x01, // s
        0x74: 0x11, // t
        0x75: 0x20, // u
        0x76: 0x09, // v
        0x77: 0x0D, // w
        0x78: 0x07, // x
        0x79: 0x10, // y
        0x7A: 0x06, // z
        0x30: 0x1D, // 0
        0x31: 0x12, // 1
        0x32: 0x13, // 2
        0x33: 0x14, // 3
        0x34: 0x15, // 4
        0x35: 0x17, // 5
        0x36: 0x16, // 6
        0x37: 0x1A, // 7
        0x38: 0x1C, // 8
        0x39: 0x19, // 9
        0x20: 0x31, // space
    ]
}

// MARK: - CGEventFlags helpers

extension CGEventFlags {
    func intersection(_ others: CGEventFlags...) -> CGEventFlags {
        var result = CGEventFlags(rawValue: 0)
        for flag in others {
            if self.contains(flag) {
                result.insert(flag)
            }
        }
        return result
    }
}

// MARK: - CGEvent postToPid helper

extension CGEvent {
    func postToPid(_ pid: pid_t) {
        let pidRef = NSNumber(value: pid)
        guard let pidArray = NSArray(object: pidRef) as? [NSNumber] else { return }
        // Use CGEventPostToPid via the private SPI bridge
        // Fallback: post to session and hope the right app gets it
        // On macOS 15, we can use the tap parameter on CGEventPost
        self.setIntegerValueField(.eventTargetUnixProcessID, value: Int64(pid))
        self.post(tap: .cgSessionEventTap)
        _ = pidArray // suppress unused warning
    }
}

// MARK: - Logging

func logInfo(_ msg: String) {
    FileHandle.standardError.write(Data("[FeishuRPA] \(msg)\n".utf8))
}

func logError(_ msg: String) {
    FileHandle.standardError.write(Data("[FeishuRPA ERROR] \(msg)\n".utf8))
}

func logVerbose(_ msg: String, enabled: Bool) {
    if enabled {
        FileHandle.standardError.write(Data("[FeishuRPA VERBOSE] \(msg)\n".utf8))
    }
}
