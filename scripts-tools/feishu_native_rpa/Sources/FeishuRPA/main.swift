import AppKit
import ApplicationServices

// MARK: - CLI Argument Parsing

struct CLIArgs {
    var text: String = ""
    var target: String = "巅池-Agent小助手"
    var verbose: Bool = false
    var dryRun: Bool = false
    var dumpAXTree: Bool = false
    var screenshotOnly: Bool = false
    var screenshotDir: String = "/tmp"

    static func parse() -> CLIArgs {
        var args = CLIArgs()
        let argv = CommandLine.arguments
        var i = 1
        while i < argv.count {
            let arg = argv[i]
            switch arg {
            case "--text":
                i += 1
                if i < argv.count { args.text = argv[i] }
            case "--target":
                i += 1
                if i < argv.count { args.target = argv[i] }
            case "--screenshot-dir":
                i += 1
                if i < argv.count { args.screenshotDir = argv[i] }
            case "--verbose":
                args.verbose = true
            case "--dry-run":
                args.dryRun = true
            case "--dump-ax-tree":
                args.dumpAXTree = true
            case "--screenshot-only":
                args.screenshotOnly = true
            default:
                if arg.hasPrefix("--text=") {
                    args.text = String(arg.dropFirst("--text=".count))
                } else if arg.hasPrefix("--target=") {
                    args.target = String(arg.dropFirst("--target=".count))
                } else if arg.hasPrefix("--screenshot-dir=") {
                    args.screenshotDir = String(arg.dropFirst("--screenshot-dir=".count))
                }
            }
            i += 1
        }
        return args
    }
}

// MARK: - Main Orchestration

func main() {
    let startTime = Date()
    let args = CLIArgs.parse()
    let verbose = args.verbose

    logInfo("Feishu Native RPA starting...")
    logInfo("Target: \(args.target)")
    if !args.text.isEmpty { logInfo("Text: \(String(args.text.prefix(60)))...") }

    // --- Phase A: Pre-flight checks ---
    logInfo("Phase A: Pre-flight checks")

    // Check Accessibility permission
    let trustedOptionKey = "AXTrustedCheckOptionPrompt" as CFString
    let trusted = AXIsProcessTrustedWithOptions(
        [trustedOptionKey: true] as CFDictionary
    )
    if !trusted {
        let result = RPAResult.failure(
            target: args.target,
            text: args.text,
            error: "Accessibility permission not granted. Go to System Settings > Privacy & Security > Accessibility and add Terminal."
        )
        outputAndExit(result, code: .accessibilityNotGranted)
    }
    logVerbose("Accessibility permission: OK", enabled: verbose)

    // Find Lark process; launch if not running
    if AccessibilityEngine.findLarkProcess() == nil {
        logInfo("Lark.app not running, attempting to launch...")
        if let larkURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: AccessibilityEngine.larkBundleID) {
            NSWorkspace.shared.open(larkURL)
            // Wait for Lark to initialize
            Thread.sleep(forTimeInterval: 5.0)
        } else {
            let result = RPAResult.failure(
                target: args.target,
                text: args.text,
                error: "Lark.app not found on this system. Please install Lark first."
            )
            outputAndExit(result, code: .larkNotRunning)
        }
    }

    // Re-check after potential launch
    guard let larkApp = AccessibilityEngine.findLarkProcess() else {
        let result = RPAResult.failure(
            target: args.target,
            text: args.text,
            error: "Lark.app not found after launch attempt."
        )
        outputAndExit(result, code: .larkNotRunning)
    }

    let pid = larkApp.processIdentifier
    logVerbose("Lark PID: \(pid)", enabled: verbose)

    let appElement = AccessibilityEngine.createAppElement(pid: pid)
    let windows = AccessibilityEngine.getWindows(appElement)

    if windows.isEmpty {
        let result = RPAResult.failure(
            target: args.target,
            text: args.text,
            error: "Lark.app has no visible windows. Please open the Lark main window."
        )
        outputAndExit(result, code: .larkNotRunning)
    }

    let mainWindow = windows[0]
    logVerbose("Found \(windows.count) window(s), using first", enabled: verbose)

    // Get window ID for screenshots
    let windowID = AccessibilityEngine.getWindowID(mainWindow)
    if let wid = windowID {
        logVerbose("Window CGWindowID: \(wid)", enabled: verbose)
    } else {
        logVerbose("Could not get CGWindowID, will use screen capture fallback", enabled: verbose)
    }

    // Get window geometry
    let windowOrigin = AccessibilityEngine.getPosition(mainWindow) ?? CGPoint(x: 0, y: 0)
    let windowSize = AccessibilityEngine.getSize(mainWindow) ?? CGSize(width: 1280, height: 800)
    logVerbose("Window: origin=(\(Int(windowOrigin.x)),\(Int(windowOrigin.y))) size=(\(Int(windowSize.width)),\(Int(windowSize.height)))", enabled: verbose)

    // --- Screenshot-only mode ---
    if args.screenshotOnly {
        logInfo("Screenshot-only mode, capturing...")
        let screenshot = ScreenshotCapture.takeAndSave(windowID: windowID)
        let elapsed = Int(Date().timeIntervalSince(startTime) * 1000)
        let result = RPAResult.success(
            target: args.target,
            text: "",
            screenshot: screenshot,
            verified: false,
            durationMs: elapsed
        )
        outputAndExit(result, code: .success)
    }

    // --- Dump AX tree mode ---
    if args.dumpAXTree {
        logInfo("Dumping AX tree...")
        let tree = AccessibilityEngine.dumpAXTree(for: mainWindow)
        print(tree)
        let screenshot = ScreenshotCapture.takeAndSave(windowID: windowID)
        let elapsed = Int(Date().timeIntervalSince(startTime) * 1000)
        let result = RPAResult.success(
            target: args.target,
            text: "",
            screenshot: screenshot,
            verified: false,
            durationMs: elapsed
        )
        outputAndExit(result, code: .success)
    }

    // --- Dry-run mode ---
    if args.dryRun {
        logInfo("Dry-run mode: found Lark process and \(windows.count) window(s), no action taken.")
        let elapsed = Int(Date().timeIntervalSince(startTime) * 1000)
        let result = RPAResult.success(
            target: args.target,
            text: args.text.isEmpty ? "(dry-run)" : args.text,
            screenshot: ScreenshotCapture.takeAndSave(windowID: windowID),
            verified: false,
            durationMs: elapsed
        )
        outputAndExit(result, code: .success)
    }

    // Require text for actual send
    guard !args.text.isEmpty else {
        let result = RPAResult.failure(
            target: args.target,
            text: "",
            error: "No --text provided. Use --text \"your message\" or --dry-run to test."
        )
        outputAndExit(result, code: .generalFailure)
    }

    // --- Phase B: Navigate to target chat ---
    logInfo("Phase B: Navigating to chat '\(args.target)'")

    // Activate Lark briefly (needed for reliable key delivery)
    larkApp.activate()
    Thread.sleep(forTimeInterval: 0.5)

    // Dismiss any existing overlays
    for _ in 0..<3 {
        InputSimulator.pressEscape(toPID: pid)
        Thread.sleep(forTimeInterval: 0.3)
    }
    logVerbose("Dismissed overlays", enabled: verbose)

    // Open global search (Cmd+K)
    logVerbose("Opening global search (Cmd+K)...", enabled: verbose)
    InputSimulator.openSearch(toPID: pid)
    Thread.sleep(forTimeInterval: 0.8)

    // Select all + clear search field
    InputSimulator.selectAll(toPID: pid)
    Thread.sleep(forTimeInterval: 0.1)
    InputSimulator.pressDelete(toPID: pid)
    Thread.sleep(forTimeInterval: 0.2)

    // Type target name via clipboard paste (supports Chinese)
    logVerbose("Pasting target name into search...", enabled: verbose)
    ClipboardHelper.pasteText(args.target, targetPID: pid)
    Thread.sleep(forTimeInterval: 1.5)

    // Press Enter to select first search result
    logVerbose("Pressing Enter to select search result...", enabled: verbose)
    InputSimulator.pressReturn(toPID: pid)
    Thread.sleep(forTimeInterval: 2.5)

    // Dismiss any residual search overlay
    InputSimulator.pressEscape(toPID: pid)
    Thread.sleep(forTimeInterval: 0.5)

    logInfo("Phase B complete: should be in chat '\(args.target)' now")

    // --- Phase C: Focus composer ---
    logInfo("Phase C: Focusing chat composer")

    // Electron's WebView does NOT expose the composer via AX tree.
    // Primary strategy: coordinate-based click at the estimated composer position.
    // The chat panel occupies the right ~2/3 of the window, and the composer is
    // a thin strip at the very bottom (~90-93% of window height).
    let composerX = windowOrigin.x + windowSize.width * 0.65
    let composerY = windowOrigin.y + windowSize.height * 0.92
    logVerbose("Clicking composer at (\(Int(composerX)), \(Int(composerY)))", enabled: verbose)
    InputSimulator.clickAt(x: composerX, y: composerY, toPID: pid)
    Thread.sleep(forTimeInterval: 0.5)

    // --- Phase D: Input message ---
    logInfo("Phase D: Inputting message via clipboard paste")
    ClipboardHelper.pasteText(args.text, targetPID: pid)
    Thread.sleep(forTimeInterval: 0.5)
    logVerbose("Message pasted", enabled: verbose)

    // --- Phase E: Send ---
    logInfo("Phase E: Sending message (Return)")
    InputSimulator.pressReturn(toPID: pid)
    Thread.sleep(forTimeInterval: 2.0)

    // --- Phase F: Verification ---
    logInfo("Phase F: Verification")

    // Take screenshot
    let screenshot = ScreenshotCapture.takeAndSave(windowID: windowID)

    // Check AX tree for message text
    let verified = AccessibilityEngine.findTextInTree(args.text, in: mainWindow)
    logVerbose("AX verification: \(verified ? "PASS" : "not confirmed")", enabled: verbose)

    let elapsed = Int(Date().timeIntervalSince(startTime) * 1000)

    // --- Output ---
    if verified || screenshot != nil {
        let result = RPAResult.success(
            target: args.target,
            text: args.text,
            screenshot: screenshot,
            verified: verified,
            durationMs: elapsed
        )
        outputAndExit(result, code: .success)
    } else {
        let result = RPAResult.failure(
            target: args.target,
            text: args.text,
            error: "Send verification failed: message not found in AX tree and screenshot capture failed.",
            screenshot: nil,
            durationMs: elapsed
        )
        outputAndExit(result, code: .sendVerificationFailed)
    }
}

// MARK: - Entry Point

main()
