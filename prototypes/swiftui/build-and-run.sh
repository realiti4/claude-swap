#!/usr/bin/env bash
# Builds a fixture-only SwiftUI menu-bar prototype as a native macOS app bundle.
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
readonly SOURCE="$SCRIPT_DIR/ClaudeSwapMenuBarPrototype.swift"
readonly PLIST="$SCRIPT_DIR/Info.plist"
readonly BUILD_DIRECTORY="$REPOSITORY_ROOT/build/swiftui-menubar-prototype"
readonly APP_BUNDLE="$BUILD_DIRECTORY/ClaudeSwapMenuBarPrototype.app"
readonly EXECUTABLE="$APP_BUNDLE/Contents/MacOS/ClaudeSwapMenuBarPrototype"
readonly LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

build() {
    rm -rf "$APP_BUNDLE"
    mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
    cp "$PLIST" "$APP_BUNDLE/Contents/Info.plist"

    xcrun swiftc \
        -parse-as-library \
        -swift-version 6 \
        -target arm64-apple-macosx26.0 \
        -framework AppKit \
        -framework SwiftUI \
        "$SOURCE" \
        -o "$EXECUTABLE"

    codesign --force --sign - "$APP_BUNDLE" >/dev/null
    plutil -lint "$APP_BUNDLE/Contents/Info.plist" >/dev/null
    "$LSREGISTER" -f "$APP_BUNDLE"
    printf 'Built and registered: %s\n' "$APP_BUNDLE"
}

case "${1:-run}" in
    build)
        build
        ;;
    run)
        build
        open "$APP_BUNDLE"
        printf 'Launched: %s\n' "$APP_BUNDLE"
        ;;
    *)
        printf 'Usage: %s [build|run]\n' "$0" >&2
        exit 64
        ;;
esac
