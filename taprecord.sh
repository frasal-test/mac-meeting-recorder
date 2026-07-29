#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECORDER="$SCRIPT_DIR/.recorder"
MENUBAR="$SCRIPT_DIR/.taprecord-menu"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/com.frasal.taprecord.plist"
LABEL="com.frasal.taprecord"

build() {
    swiftc -framework ScreenCaptureKit -framework AVFoundation \
        -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist \
        -Xlinker "$SCRIPT_DIR/Recorder-Info.plist" \
        -O "$SCRIPT_DIR/recorder.swift" -o "$RECORDER"
    swiftc -framework AppKit \
        -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist \
        -Xlinker "$SCRIPT_DIR/Menubar-Info.plist" \
        -O "$SCRIPT_DIR/menubar.swift" -o "$MENUBAR"
    echo "Built: $RECORDER"
    echo "Built: $MENUBAR"
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
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT"
    echo "TapRecord is installed and running in the menu bar."
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
        if [[ ! -x "$RECORDER" ]]; then
            build
        fi
        exec "$SCRIPT_DIR/.venv/bin/python" -m meeting_recorder.control doctor
        ;;
    *)
        echo "Usage: $0 {build|run|install|uninstall|doctor}" >&2
        exit 1
        ;;
esac
