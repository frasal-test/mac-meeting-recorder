#!/usr/bin/env bash
# Single entry point for recording + transcribing a meeting.
#
# Usage:
#   ./meet.sh                # Italian, with diarization (default)
#   ./meet.sh en             # English, with diarization
#   ./meet.sh es nodiar      # Spanish, transcript only (no HF token needed)
#   ./meet.sh auto nodiar    # Auto-detect language, no diarization
#
# Press Enter to stop recording.
# The transcript appears automatically in recordings/transcripts/.
set -euo pipefail

MEETING_LANG="${1:-it}"
DIARIZE="${2:-diar}"
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

if [[ "$MEETING_LANG" != "it" && "$MEETING_LANG" != "en" && "$MEETING_LANG" != "es" && "$MEETING_LANG" != "auto" ]]; then
    echo "Usage: $0 [it|en|es|auto] [diar|nodiar]" >&2
    exit 1
fi

if [[ "$DIARIZE" != "diar" && "$DIARIZE" != "nodiar" ]]; then
    echo "Usage: $0 [it|en|es|auto] [diar|nodiar]" >&2
    exit 1
fi

DIARIZE_FLAG=""
if [[ "$DIARIZE" == "diar" ]]; then
    DIARIZE_FLAG="--diarize"
fi

# ── compile recorder if needed ────────────────────────────────────────────────
if [[ ! -f "$BIN" || "$SRC" -nt "$BIN" ]]; then
    echo "Compiling recorder.swift..."
    swiftc -framework ScreenCaptureKit -framework AVFoundation \
           -O "$SRC" -o "$BIN"
fi

# ── pick model (English gets the .en variant, others need multilingual) ───────
if [[ "$MEETING_LANG" == "en" ]]; then
    MODEL="${WHISPER_MODEL:-medium.en}"
else
    MODEL="${WHISPER_MODEL:-medium}"
fi

MEETING_LANGUAGE_FLAG=""
if [[ "$MEETING_LANG" != "auto" ]]; then
    MEETING_LANGUAGE_FLAG="--language $MEETING_LANG"
fi

mkdir -p "$RECORDINGS_DIR" "$TRANSCRIPTS_DIR"

# ── start transcription watcher in background ─────────────────────────────────
# stable-seconds=3: file is already complete when saved, no need to wait longer
"$VENV_PYTHON" -m meeting_recorder.cli \
    "$RECORDINGS_DIR" \
    --watch \
    --model "$MODEL" \
    $MEETING_LANGUAGE_FLAG \
    $DIARIZE_FLAG \
    --stable-seconds 3 \
    >/dev/null 2>&1 &
WATCHER_PID=$!

cleanup() { kill "$WATCHER_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# ── start recording ───────────────────────────────────────────────────────────
TS="$(date '+%Y-%m-%dT%H-%M-%S')"
OUTPUT="$RECORDINGS_DIR/$TS.m4a"
TRANSCRIPT="$TRANSCRIPTS_DIR/${TS}.txt"

echo ""
echo "  Lingua: $MEETING_LANG | Modello: $MODEL | Diarizzazione: $DIARIZE"
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
    MINS=$((ELAPSED / 60))
    SECS=$((ELAPSED % 60))
    printf "\r  ⏳ Trascrizione in corso... %dm %02ds" $MINS $SECS
done
echo ""

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
