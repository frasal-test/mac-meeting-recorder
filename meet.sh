#!/usr/bin/env bash
# Single entry point for recording + transcribing a meeting.
#
# Usage:
#   ./meet.sh        # Italian (default)
#   ./meet.sh en     # English
#
# Press Enter to stop recording.
# The transcript appears automatically in recordings/transcripts/.
set -euo pipefail

LANG="${1:-it}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/recorder.swift"
BIN="$SCRIPT_DIR/.recorder"
RECORDINGS_DIR="${RECORDINGS_DIR:-$SCRIPT_DIR/recordings}"
TRANSCRIPTS_DIR="$RECORDINGS_DIR/transcripts"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

# ── sanity checks ─────────────────────────────────────────────────────────────
if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Missing Python environment: $SCRIPT_DIR/.venv" >&2
    echo "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements-diarization.txt" >&2
    exit 1
fi

if [[ "$LANG" != "it" && "$LANG" != "en" ]]; then
    echo "Usage: $0 [it|en]" >&2
    exit 1
fi

# ── compile recorder if needed ────────────────────────────────────────────────
if [[ ! -f "$BIN" || "$SRC" -nt "$BIN" ]]; then
    echo "Compiling recorder.swift..."
    swiftc -framework ScreenCaptureKit -framework AVFoundation \
           -O "$SRC" -o "$BIN"
fi

# ── pick model ────────────────────────────────────────────────────────────────
if [[ "$LANG" == "en" ]]; then
    MODEL="${WHISPER_MODEL:-medium.en}"
else
    MODEL="${WHISPER_MODEL:-medium}"
fi

mkdir -p "$RECORDINGS_DIR" "$TRANSCRIPTS_DIR"

# ── start transcription watcher in background ─────────────────────────────────
# stable-seconds=3: file is already complete when saved, no need to wait longer
"$VENV_PYTHON" -m meeting_recorder.cli \
    "$RECORDINGS_DIR" \
    --watch \
    --model "$MODEL" \
    --language "$LANG" \
    --diarize \
    --stable-seconds 3 \
    2>/dev/null &
WATCHER_PID=$!

cleanup() { kill "$WATCHER_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# ── start recording ───────────────────────────────────────────────────────────
TS="$(date '+%Y-%m-%dT%H-%M-%S')"
OUTPUT="$RECORDINGS_DIR/$TS.m4a"
TRANSCRIPT="$TRANSCRIPTS_DIR/${TS}.txt"

echo ""
echo "  Lingua: $LANG | Modello: $MODEL"
echo "  ● Registrazione in corso — premi Invio per fermare"
echo ""

# Recorder runs in foreground: Enter stops it, SIGTERM also works
"$BIN" "$OUTPUT"

# ── wait for transcript ───────────────────────────────────────────────────────
echo ""
echo "  ⏳ Trascrizione in corso..."

ELAPSED=0
TIMEOUT=600  # max 10 minutes
while [[ ! -f "$TRANSCRIPT" && $ELAPSED -lt $TIMEOUT ]]; do
    sleep 3
    ELAPSED=$((ELAPSED + 3))
done

echo ""
if [[ -f "$TRANSCRIPT" ]]; then
    echo "  ✓ Fatto!"
    echo ""
    echo "  Testo:     $TRANSCRIPT"
    echo "  Parlanti:  ${TRANSCRIPTS_DIR}/${TS}.speakers.txt"
    echo "  Sottotit.: ${TRANSCRIPTS_DIR}/${TS}.srt"
    echo ""
else
    echo "  ⚠ Timeout — trascrizione ancora in corso o fallita."
    echo "  Controlla: $TRANSCRIPTS_DIR"
    echo ""
fi
