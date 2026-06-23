"""Static analysis tests for the Swift-based Feishu Native RPA sender.

These tests verify that the Swift source code contains the expected patterns
without requiring a running Lark.app or Accessibility permissions.
"""

from __future__ import annotations

from pathlib import Path

SWIFT_DIR = Path("scripts-tools/feishu_native_rpa/Sources/FeishuRPA")
PYTHON_WRAPPER = Path("scripts-tools/feishu_native_rpa_sender.py")
CRON_INSTALLER = Path("scripts-tools/install-feishu-native-rpa-cron.sh")


def _read_swift(name: str) -> str:
    return (SWIFT_DIR / name).read_text(encoding="utf-8")


def _read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- AccessibilityEngine.swift ---


def test_swift_source_uses_correct_bundle_id() -> None:
    source = _read_swift("AccessibilityEngine.swift")
    assert "com.electron.lark" in source


def test_swift_source_creates_ax_element() -> None:
    source = _read_swift("AccessibilityEngine.swift")
    assert "AXUIElementCreateApplication" in source


def test_swift_source_traverses_ax_tree() -> None:
    source = _read_swift("AccessibilityEngine.swift")
    assert "kAXChildrenAttribute" in source
    assert "kAXRoleAttribute" in source


def test_swift_source_has_composer_finder() -> None:
    source = _read_swift("AccessibilityEngine.swift")
    assert "findComposer" in source
    assert "AXTextArea" in source


def test_swift_source_has_ax_tree_dump() -> None:
    source = _read_swift("AccessibilityEngine.swift")
    assert "dumpAXTree" in source


def test_swift_source_finds_text_in_tree() -> None:
    source = _read_swift("AccessibilityEngine.swift")
    assert "findTextInTree" in source


# --- InputSimulator.swift ---


def test_input_simulator_uses_cgevent() -> None:
    source = _read_swift("InputSimulator.swift")
    assert "CGEvent" in source
    assert "CGEventSource" in source


def test_input_simulator_has_key_constants() -> None:
    source = _read_swift("InputSimulator.swift")
    assert "returnKey" in source
    assert "escape" in source
    assert "ansK" in source


def test_input_simulator_has_search_opener() -> None:
    source = _read_swift("InputSimulator.swift")
    assert "openSearch" in source


def test_input_simulator_has_click_support() -> None:
    source = _read_swift("InputSimulator.swift")
    assert "clickAt" in source
    assert "leftMouseDown" in source


# --- ClipboardHelper.swift ---


def test_swift_uses_clipboard_for_text() -> None:
    source = _read_swift("ClipboardHelper.swift")
    assert "NSPasteboard" in source


def test_clipboard_paste_via_cmd_v() -> None:
    source = _read_swift("ClipboardHelper.swift")
    assert "pasteFromClipboard" in source
    assert "pasteText" in source


# --- ScreenshotCapture.swift ---


def test_screenshot_uses_cgwindowlist() -> None:
    source = _read_swift("ScreenshotCapture.swift")
    assert "CGWindowListCreateImage" in source


def test_screenshot_saves_png() -> None:
    source = _read_swift("ScreenshotCapture.swift")
    assert "savePNG" in source
    assert ".png" in source


# --- main.swift ---


def test_main_checks_accessibility_permission() -> None:
    source = _read_swift("main.swift")
    assert "AXIsProcessTrustedWithOptions" in source


def test_main_has_phase_labels() -> None:
    source = _read_swift("main.swift")
    assert "Phase A" in source
    assert "Phase B" in source
    assert "Phase C" in source
    assert "Phase D" in source
    assert "Phase E" in source
    assert "Phase F" in source


def test_main_has_dry_run_mode() -> None:
    source = _read_swift("main.swift")
    assert "dryRun" in source
    assert "--dry-run" in source


def test_main_has_dump_ax_tree_mode() -> None:
    source = _read_swift("main.swift")
    assert "dumpAXTree" in source
    assert "--dump-ax-tree" in source


def test_main_outputs_json() -> None:
    source = _read_swift("main.swift")
    assert "outputAndExit" in source
    assert "RPAResult" in source


def test_main_has_timeout_handling() -> None:
    source = _read_swift("main.swift")
    # Verify sleep calls exist for timing between steps
    assert "Thread.sleep" in source


# --- ResultJSON.swift ---


def test_result_json_has_correct_fields() -> None:
    source = _read_swift("ResultJSON.swift")
    assert "verified_in_ax" in source
    assert "duration_ms" in source
    assert "method" in source


def test_result_json_has_exit_codes() -> None:
    source = _read_swift("ResultJSON.swift")
    assert "accessibilityNotGranted" in source
    assert "larkNotRunning" in source
    assert "chatNotFound" in source
    assert "sendVerificationFailed" in source


# --- Python wrapper ---


def test_python_wrapper_has_retry_logic() -> None:
    source = _read_file(PYTHON_WRAPPER)
    assert "send_with_retry" in source
    assert "max_retries" in source


def test_python_wrapper_finds_binary() -> None:
    source = _read_file(PYTHON_WRAPPER)
    assert "find_binary" in source
    assert "FeishuRPA" in source


def test_python_wrapper_handles_timeout() -> None:
    source = _read_file(PYTHON_WRAPPER)
    assert "TimeoutExpired" in source


def test_python_wrapper_has_logging() -> None:
    source = _read_file(PYTHON_WRAPPER)
    assert "feishu_native_rpa.log" in source


# --- Cron installer ---


def test_cron_installer_has_install_remove_status() -> None:
    source = _read_file(CRON_INSTALLER)
    assert "install)" in source
    assert "remove)" in source
    assert "status)" in source


def test_cron_installer_references_native_sender() -> None:
    source = _read_file(CRON_INSTALLER)
    assert "feishu_native_rpa_sender.py" in source


# --- Package.swift ---


def test_package_targets_macos_14() -> None:
    source = _read_file(Path("scripts-tools/feishu_native_rpa/Package.swift"))
    assert ".v14" in source or "macOS" in source


def test_package_has_no_external_deps() -> None:
    source = _read_file(Path("scripts-tools/feishu_native_rpa/Package.swift"))
    # No dependencies array means no external packages
    assert "dependencies:" not in source or "dependencies: []" in source
