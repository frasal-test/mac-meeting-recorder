#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECORDER="$SCRIPT_DIR/.recorder"
MENUBAR_APP="$SCRIPT_DIR/MeetRec.app"
MENUBAR="$MENUBAR_APP/Contents/MacOS/MeetRec"
MENUBAR_INFO="$MENUBAR_APP/Contents/Info.plist"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/com.frasal.meetrec.plist"
LABEL="com.frasal.meetrec"
LEGACY_LAUNCH_AGENT="$HOME/Library/LaunchAgents/com.frasal.taprecord.plist"
LEGACY_LABEL="com.frasal.taprecord"
# A self-signed code-signing certificate keeps the designated requirement tied
# to an identity instead of the binary's cdhash. Without one macOS pins the
# Microphone and Screen Recording grants to the exact build and re-prompts
# after every recompile. Create it once — see "Stable signing" in the README.
SIGN_IDENTITY="${MEETREC_SIGN_IDENTITY:-MeetRec Dev}"

needs_build() {
    local output="$1"
    shift
    if [[ ! -x "$output" ]]; then
        return 0
    fi
    local input
    for input in "$@"; do
        if [[ "$input" -nt "$output" ]]; then
            return 0
        fi
    done
    return 1
}

resolve_sign_identity() {
    if security find-identity -v -p codesigning 2>/dev/null \
        | grep -qF "\"$SIGN_IDENTITY\""; then
        printf '%s' "$SIGN_IDENTITY"
    else
        printf '%s' "-"
    fi
}

# Signing is unconditional: codesign is deterministic, so re-signing unchanged
# code reproduces the same signature and costs nothing. Skipping it after a
# rebuild would instead leave the bundle seal broken.
ensure_signature() {
    local binary="$1"
    local identifier="$2"
    local identity
    identity="$(resolve_sign_identity)"
    codesign --force --sign "$identity" --identifier "$identifier" "$binary"
    if [[ "$identity" == "-" ]]; then
        echo "Signed ad-hoc: $binary ($identifier)"
        echo "  Warning: macOS will re-ask for permissions after every" \
            "rebuild. Create the '$SIGN_IDENTITY' certificate to stop this."
    else
        echo "Signed: $binary ($identifier) with '$identity'"
    fi
}

build_recorder() {
    local sources=(
        "$SCRIPT_DIR/RecordingCore.swift"
        "$SCRIPT_DIR/recorder.swift"
        "$SCRIPT_DIR/Recorder-Info.plist"
    )
    if needs_build "$RECORDER" "${sources[@]}"; then
        swiftc -framework ScreenCaptureKit -framework AVFoundation \
            -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist \
            -Xlinker "$SCRIPT_DIR/Recorder-Info.plist" \
            -O "$SCRIPT_DIR/RecordingCore.swift" \
            "$SCRIPT_DIR/recorder.swift" -o "$RECORDER"
        echo "Built: $RECORDER"
    else
        echo "Up to date: $RECORDER"
    fi
    ensure_signature "$RECORDER" "com.frasal.meetrec.recorder"
}

build_menubar() {
    local sources=(
        "$SCRIPT_DIR/RecordingCore.swift"
        "$SCRIPT_DIR/menubar.swift"
        "$SCRIPT_DIR/Menubar-Info.plist"
    )
    if needs_build "$MENUBAR" "${sources[@]}"; then
        mkdir -p "$MENUBAR_APP/Contents/MacOS"
        cp "$SCRIPT_DIR/Menubar-Info.plist" "$MENUBAR_INFO"
        swiftc -framework AppKit -framework ScreenCaptureKit \
            -framework AVFoundation \
            -O "$SCRIPT_DIR/RecordingCore.swift" \
            "$SCRIPT_DIR/menubar.swift" -o "$MENUBAR"
        echo "Built: $MENUBAR_APP"
    else
        echo "Up to date: $MENUBAR_APP"
    fi
    ensure_signature "$MENUBAR_APP" "com.frasal.meetrec.menubar"
}

build() {
    build_recorder
    build_menubar
}

install_agent() {
    build
    mkdir -p "$(dirname "$LAUNCH_AGENT")"
    "$SCRIPT_DIR/.venv/bin/python" -m meeting_recorder.launch_agent \
        install \
        --plist "$LAUNCH_AGENT" \
        --label "$LABEL" \
        --program "$MENUBAR" \
        --project "$SCRIPT_DIR"
    launchctl bootout "gui/$(id -u)/$LEGACY_LABEL" 2>/dev/null || true
    if [[ -f "$LEGACY_LAUNCH_AGENT" ]]; then
        legacy_disabled="$LEGACY_LAUNCH_AGENT.disabled"
        if [[ -e "$legacy_disabled" ]]; then
            legacy_disabled="$LEGACY_LAUNCH_AGENT.disabled.$(date +%s)"
        fi
        mv "$LEGACY_LAUNCH_AGENT" "$legacy_disabled"
        echo "Legacy LaunchAgent disabled: $legacy_disabled"
    fi
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT"
    echo "MeetRec is installed and running in the menu bar."
}

uninstall_agent() {
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    if [[ -f "$LAUNCH_AGENT" ]]; then
        mv "$LAUNCH_AGENT" "$LAUNCH_AGENT.disabled"
        echo "LaunchAgent disabled: $LAUNCH_AGENT.disabled"
    fi
}

case "${1:-}" in
    build)
        build
        ;;
    build-recorder)
        build_recorder
        ;;
    run)
        build
        exec "$MENUBAR" "$SCRIPT_DIR"
        ;;
    install)
        install_agent
        ;;
    uninstall)
        uninstall_agent
        ;;
    doctor)
        if [[ ! -x "$RECORDER" || ! -x "$MENUBAR" ]]; then
            build
        fi
        exec "$SCRIPT_DIR/.venv/bin/python" -m meeting_recorder.control doctor
        ;;
    *)
        echo "Usage: $0 {build|build-recorder|run|install|uninstall|doctor}" >&2
        exit 1
        ;;
esac
