#!/bin/zsh
# Build a self-contained arm64 converter and a one-click NAS installer app.

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h:h}"
OUTPUT_ROOT="${1:-/private/tmp/ai-cdr-release}"
PUBLIC_KEY="${2:-$HOME/.ssh/id_ed25519.pub}"
CODESIGN_IDENTITY="${AI_CDR_CODESIGN_IDENTITY:-Dianchi AI CDR Code Signing}"
BUILD_ROOT="$OUTPUT_ROOT/build"
DIST_ROOT="$OUTPUT_ROOT/dist"
RAW_DIST_ROOT="$OUTPUT_ROOT/raw-dist"
APP_ROOT="$OUTPUT_ROOT/AI-CDR-安装.app"
CONVERTER_APP="$DIST_ROOT/AI-CDR-Converter.app"

/bin/rm -rf "$OUTPUT_ROOT"
/bin/mkdir -p "$BUILD_ROOT" "$DIST_ROOT" "$RAW_DIST_ROOT"

cd "$REPO_ROOT"
uv run pyinstaller \
    --noconfirm \
    --clean \
    --onefile \
    --name AI-CDR-Converter \
    --workpath "$BUILD_ROOT" \
    --specpath "$BUILD_ROOT" \
    --distpath "$RAW_DIST_ROOT" \
    "$SCRIPT_DIR/ai_cdr_converter.py"

/bin/mkdir -p "$CONVERTER_APP/Contents/MacOS"
/bin/cp "$RAW_DIST_ROOT/AI-CDR-Converter" \
    "$CONVERTER_APP/Contents/MacOS/AI-CDR-Converter"
/bin/chmod 755 "$CONVERTER_APP/Contents/MacOS/AI-CDR-Converter"
/usr/bin/plutil -create xml1 "$CONVERTER_APP/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleExecutable -string "AI-CDR-Converter" \
    "$CONVERTER_APP/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleIdentifier -string "com.dianchi.ai-cdr-converter" \
    "$CONVERTER_APP/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleName -string "AI-CDR-Converter" \
    "$CONVERTER_APP/Contents/Info.plist"
/usr/bin/plutil -insert CFBundlePackageType -string "APPL" \
    "$CONVERTER_APP/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleShortVersionString -string "1.2.1" \
    "$CONVERTER_APP/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleVersion -string "1.2.1" \
    "$CONVERTER_APP/Contents/Info.plist"
/usr/bin/plutil -insert LSMinimumSystemVersion -string "11.0" \
    "$CONVERTER_APP/Contents/Info.plist"
/usr/bin/plutil -insert LSUIElement -bool true \
    "$CONVERTER_APP/Contents/Info.plist"
/usr/bin/plutil -insert NSAppleEventsUsageDescription -string \
    "用于调用 Illustrator 和 CorelDRAW 完成本地文件转换。" \
    "$CONVERTER_APP/Contents/Info.plist"
/usr/bin/codesign --force --deep --sign "$CODESIGN_IDENTITY" "$CONVERTER_APP"

/bin/mkdir -p "$APP_ROOT/Contents/MacOS" "$APP_ROOT/Contents/Resources"
/usr/bin/xcrun clang \
    -fobjc-arc \
    -fmodules-cache-path="$OUTPUT_ROOT/module-cache" \
    -mmacosx-version-min=11.0 \
    -O2 \
    -framework Foundation \
    -framework AppKit \
    -o "$APP_ROOT/Contents/MacOS/AI-CDR-安装" \
    "$SCRIPT_DIR/InstallerMain.m"
/bin/cp -R "$CONVERTER_APP" "$APP_ROOT/Contents/Resources/AI-CDR-Converter.app"
/bin/cp "$PUBLIC_KEY" "$APP_ROOT/Contents/Resources/admin_key.pub"
/bin/cp "$SCRIPT_DIR/installer_payload.sh" "$APP_ROOT/Contents/Resources/installer_payload.sh"
/bin/chmod 755 "$APP_ROOT/Contents/MacOS/AI-CDR-安装"
/bin/chmod 755 "$APP_ROOT/Contents/Resources/installer_payload.sh"

/usr/bin/plutil -create xml1 "$APP_ROOT/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleExecutable -string "AI-CDR-安装" "$APP_ROOT/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleIdentifier -string "com.dianchi.ai-cdr-installer" "$APP_ROOT/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleName -string "AI-CDR-安装" "$APP_ROOT/Contents/Info.plist"
/usr/bin/plutil -insert CFBundlePackageType -string "APPL" "$APP_ROOT/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleShortVersionString -string "1.2.1" "$APP_ROOT/Contents/Info.plist"
/usr/bin/plutil -insert LSMinimumSystemVersion -string "11.0" "$APP_ROOT/Contents/Info.plist"
/usr/bin/plutil -insert NSHighResolutionCapable -bool true "$APP_ROOT/Contents/Info.plist"

/usr/bin/codesign --force --deep --sign "$CODESIGN_IDENTITY" "$APP_ROOT"
/usr/bin/ditto -c -k --keepParent "$APP_ROOT" "$OUTPUT_ROOT/AI-CDR-安装.zip"
/usr/bin/shasum -a 256 "$OUTPUT_ROOT/AI-CDR-安装.zip"
