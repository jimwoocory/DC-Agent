import AppKit
import ApplicationServices

// MARK: - Lark Process Discovery

enum AccessibilityEngine {

    // Bundle ID for the Chinese-market Feishu (Lark) Electron app.
    static let larkBundleID = "com.electron.lark"

    // MARK: - Process

    /// Find the running Lark/Feishu process.
    static func findLarkProcess() -> NSRunningApplication? {
        let apps = NSRunningApplication.runningApplications(
            withBundleIdentifier: larkBundleID
        )
        return apps.first { $0.isActive || $0.activationPolicy != .prohibited }
            ?? apps.first
    }

    /// Get the PID of the Lark process.
    static func larkPID() -> pid_t? {
        findLarkProcess()?.processIdentifier
    }

    // MARK: - AX Element Creation

    /// Create the root AXUIElement for a process.
    static func createAppElement(pid: pid_t) -> AXUIElement {
        AXUIElementCreateApplication(pid)
    }

    // MARK: - Attribute Helpers

    /// Get a string attribute from an AX element.
    static func getStringAttribute(_ element: AXUIElement, _ attr: String) -> String? {
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(element, attr as CFString, &value)
        guard result == .success else { return nil }
        return value as? String
    }

    /// Get the AX role of an element (e.g. "AXButton", "AXTextArea").
    static func getRole(_ element: AXUIElement) -> String? {
        getStringAttribute(element, kAXRoleAttribute)
    }

    /// Get the AX subrole of an element.
    static func getSubrole(_ element: AXUIElement) -> String? {
        getStringAttribute(element, kAXSubroleAttribute)
    }

    /// Get the AX title/description of an element.
    static func getTitle(_ element: AXUIElement) -> String? {
        getStringAttribute(element, kAXTitleAttribute)
            ?? getStringAttribute(element, kAXDescriptionAttribute)
    }

    /// Get the AX value of an element (for text fields, etc.).
    static func getValue(_ element: AXUIElement) -> String? {
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &value)
        guard result == .success else { return nil }
        return value as? String
    }

    /// Get child elements of an AX element.
    static func getChildren(_ element: AXUIElement) -> [AXUIElement] {
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &value)
        guard result == .success else { return [] }
        return (value as? [AXUIElement]) ?? []
    }

    /// Get the windows of an app.
    static func getWindows(_ appElement: AXUIElement) -> [AXUIElement] {
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(appElement, kAXWindowsAttribute as CFString, &value)
        guard result == .success else { return [] }
        return (value as? [AXUIElement]) ?? []
    }

    /// Get the position (origin) of an AX element.
    static func getPosition(_ element: AXUIElement) -> CGPoint? {
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &value)
        guard result == .success, let axValue = value else { return nil }
        var point = CGPoint.zero
        AXValueGetValue(axValue as! AXValue, .cgPoint, &point)
        return point
    }

    /// Get the size of an AX element.
    static func getSize(_ element: AXUIElement) -> CGSize? {
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &value)
        guard result == .success, let axValue = value else { return nil }
        var size = CGSize.zero
        AXValueGetValue(axValue as! AXValue, .cgSize, &size)
        return size
    }

    /// Get the CGWindowID from an AX window element.
    static func getWindowID(_ windowElement: AXUIElement) -> CGWindowID? {
        var value: CFTypeRef?
        // kAXWindowAttribute returns the CGWindowID on some macOS versions
        let result = AXUIElementCopyAttributeValue(
            windowElement,
            "AXWindow" as CFString,
            &value
        )
        if result == .success, let num = value as? NSNumber {
            return CGWindowID(num.uint32Value)
        }

        // Fallback: use CGWindowListCopyWindowInfo to match by title + owner PID
        guard let title = getTitle(windowElement),
              let position = getPosition(windowElement),
              let size = getSize(windowElement) else {
            return nil
        }

        let windowList = CGWindowListCopyWindowInfo(
            [.optionOnScreenOnly, .excludeDesktopElements],
            kCGNullWindowID
        ) as? [[String: Any]] ?? []

        for info in windowList {
            guard let wTitle = info[kCGWindowName as String] as? String,
                  wTitle == title,
                  let wBounds = info[kCGWindowBounds as String] as? [String: CGFloat],
                  abs((wBounds["X"] ?? 0) - position.x) < 5,
                  abs((wBounds["Y"] ?? 0) - position.y) < 5,
                  abs((wBounds["Width"] ?? 0) - size.width) < 5
            else { continue }

            if let wNumber = info[kCGWindowNumber as String] as? Int {
                return CGWindowID(wNumber)
            }
        }

        return nil
    }

    // MARK: - Tree Search

    /// Recursively find an element by role and optional title/label match.
    static func findElement(
        byRole targetRole: String,
        label: String? = nil,
        in element: AXUIElement,
        maxDepth: Int = 30
    ) -> AXUIElement? {
        guard maxDepth > 0 else { return nil }

        let role = getRole(element)
        if role == targetRole {
            if let label = label {
                let title = getTitle(element) ?? ""
                let value = getValue(element) ?? ""
                if title.contains(label) || value.contains(label) {
                    return element
                }
            } else {
                return element
            }
        }

        for child in getChildren(element) {
            if let found = findElement(byRole: targetRole, label: label, in: child, maxDepth: maxDepth - 1) {
                return found
            }
        }
        return nil
    }

    /// Find all elements matching a predicate (role + optional position heuristic).
    static func findAllElements(
        byRole targetRole: String,
        in element: AXUIElement,
        maxDepth: Int = 30
    ) -> [AXUIElement] {
        guard maxDepth > 0 else { return [] }

        var results: [AXUIElement] = []
        let role = getRole(element)
        if role == targetRole {
            results.append(element)
        }

        for child in getChildren(element) {
            results.append(contentsOf: findAllElements(byRole: targetRole, in: child, maxDepth: maxDepth - 1))
        }
        return results
    }

    /// Find the chat composer (text input area) in Lark's AX tree.
    /// Strategy: look for AXTextArea in the lower-right quadrant of the window.
    static func findComposer(in windowElement: AXUIElement, windowOrigin: CGPoint, windowSize: CGSize) -> AXUIElement? {
        let textAreas = findAllElements(byRole: "AXTextArea", in: windowElement)
        let textFieldGroups = findAllElements(byRole: "AXTextField", in: windowElement)
        let candidates = textAreas + textFieldGroups

        // The composer is typically in the lower-right quadrant
        let rightThreshold = windowOrigin.x + windowSize.width * 0.35
        let bottomThreshold = windowOrigin.y + windowSize.height * 0.55

        var best: (element: AXUIElement, score: Double)?
        for elem in candidates {
            guard let pos = getPosition(elem), let size = getSize(elem) else { continue }
            let inRightPanel = pos.x > rightThreshold
            let nearBottom = pos.y > bottomThreshold
            let wideEnough = size.width > 120

            if !inRightPanel || !nearBottom || !wideEnough { continue }

            // Score: prefer elements closer to bottom-right
            let score = Double(pos.y) + Double(pos.x) * 0.1
            if best == nil || score > best!.score {
                best = (elem, score)
            }
        }

        // If position heuristic fails, try any AXTextArea that's reasonably large
        if best == nil {
            for elem in candidates {
                guard let size = getSize(elem) else { continue }
                if size.width > 200 && size.height > 20 {
                    return elem
                }
            }
        }

        return best?.element
    }

    /// Search for message text in the AX tree (for verification).
    static func findTextInTree(_ text: String, in element: AXUIElement, maxDepth: Int = 20) -> Bool {
        guard maxDepth > 0 else { return false }

        let shortText = String(text.prefix(50))
        let role = getRole(element) ?? ""
        if role == "AXStaticText" || role == "AXTextArea" {
            if let value = getValue(element), value.contains(shortText) {
                return true
            }
            if let title = getTitle(element), title.contains(shortText) {
                return true
            }
        }

        for child in getChildren(element) {
            if findTextInTree(text, in: child, maxDepth: maxDepth - 1) {
                return true
            }
        }
        return false
    }

    // MARK: - Focus & Action

    /// Focus an AX element.
    static func focusElement(_ element: AXUIElement) -> Bool {
        let result = AXUIElementSetAttributeValue(
            element,
            kAXFocusedAttribute as CFString,
            true as CFTypeRef
        )
        return result == .success
    }

    /// Perform an AX action on an element (e.g. "AXPress").
    static func performAction(_ action: String, on element: AXUIElement) -> Bool {
        AXUIElementPerformAction(element, action as CFString) == .success
    }

    // MARK: - Debug: Dump AX Tree

    /// Dump the AX tree to a string for debugging.
    static func dumpAXTree(for element: AXUIElement, indent: Int = 0, maxDepth: Int = 15) -> String {
        guard maxDepth > 0 else { return "" }

        let prefix = String(repeating: "  ", count: indent)
        let role = getRole(element) ?? "?"
        let title = getTitle(element) ?? ""
        let value = getValue(element) ?? ""

        var line = "\(prefix)\(role)"
        if !title.isEmpty { line += " '\(String(title.prefix(40)))'" }
        if !value.isEmpty { line += " = '\(String(value.prefix(40)))'" }
        if let pos = getPosition(element), let size = getSize(element) {
            line += " @ (\(Int(pos.x)), \(Int(pos.y)), \(Int(size.width)), \(Int(size.height)))"
        }
        line += "\n"

        for child in getChildren(element) {
            line += dumpAXTree(for: child, indent: indent + 1, maxDepth: maxDepth - 1)
        }
        return line
    }
}
