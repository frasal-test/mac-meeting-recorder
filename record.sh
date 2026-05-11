#!/usr/bin/env bash
# Record system audio (remote participants) + microphone (you) to recordings/.
# Everything is handled in Swift — no external tools required.
#
# Permissions needed (one-time, in System Settings → Privacy & Security):
#   - Screen & System Audio Recording  → for remote participants' audio
#   - Microphone                       → for your own voice
#
# Stop with Ctrl+C — streams are merged into a single .m4a automatically.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/recorder.swift"
BIN="$SCRIPT_DIR/.recorder"
RECORDINGS_DIR="${RECORDINGS_DIR:-$SCRIPT_DIR/recordings}"

if [[ ! -f "$BIN" || "$SRC" -nt "$BIN" ]]; then
    echo "Compiling recorder.swift..."
    swiftc -framework ScreenCaptureKit -framework AVFoundation \
           -O "$SRC" -o "$BIN"
    echo "Done."
fi

mkdir -p "$RECORDINGS_DIR"
OUTPUT="$RECORDINGS_DIR/$(date '+%Y-%m-%dT%H-%M-%S').m4a"

exec "$BIN" "$OUTPUT"
