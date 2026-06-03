#!/usr/bin/env bash
# Single entry point for recording + transcribing a meeting.
#
# Usage:
#   ./meet.sh                    # Italian, diarization, speakers auto-detected
#   ./meet.sh en                 # English, diarization
#   ./meet.sh es nodiar          # Spanish, transcript only (no HF token needed)
#   ./meet.sh auto nodiar        # Auto-detect language, no diarization
#   ./meet.sh it diar 2          # Italian, diarization, 2 speakers (more accurate)
#
# Press Enter to stop recording.
# Each run creates recordings/<timestamp>/ with audio/ and transcripts/.
set -euo pipefail

MEETING_LANG="${1:-it}"
DIARIZE="${2:-diar}"
NUM_SPEAKERS="${3:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/recorder.swift"
BIN="$SCRIPT_DIR/.recorder"
RECORDINGS_DIR="${RECORDINGS_DIR:-$SCRIPT_DIR/recordings}"
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

if [[ -n "$NUM_SPEAKERS" && ! "$NUM_SPEAKERS" =~ ^[1-9][0-9]*$ ]]; then
    echo "num_speakers must be a positive integer." >&2
    exit 1
fi

TRANSCRIBE_ARGS=()
if [[ "$DIARIZE" == "diar" ]]; then
    TRANSCRIBE_ARGS+=(--diarize)
fi

if [[ -n "$NUM_SPEAKERS" && "$DIARIZE" == "diar" ]]; then
    TRANSCRIBE_ARGS+=(--num-speakers "$NUM_SPEAKERS")
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

if [[ "$MEETING_LANG" != "auto" ]]; then
    TRANSCRIBE_ARGS+=(--language "$MEETING_LANG")
fi

# ── start recording ───────────────────────────────────────────────────────────
TS="$(date '+%Y-%m-%dT%H-%M-%S')"
SESSION_DIR="$RECORDINGS_DIR/$TS"
AUDIO_DIR="$SESSION_DIR/audio"
TRANSCRIPTS_DIR="$SESSION_DIR/transcripts"
OUTPUT="$AUDIO_DIR/meeting.m4a"
TRANSCRIPT="$TRANSCRIPTS_DIR/meeting.txt"

mkdir -p "$AUDIO_DIR" "$TRANSCRIPTS_DIR"

echo ""
echo "  Lingua: $MEETING_LANG | Modello: $MODEL | Diarizzazione: $DIARIZE"
echo "  Sessione: $SESSION_DIR"
echo "  ● Registrazione in corso — premi Invio per fermare"
echo ""

# Recorder runs in foreground: Enter stops it, SIGTERM also works
"$BIN" "$OUTPUT"

if [[ ! -s "$OUTPUT" ]]; then
    echo "Recording failed or produced an empty file: $OUTPUT" >&2
    exit 1
fi

# ── wait for transcript ───────────────────────────────────────────────────────
echo ""
echo "  ⏳ Trascrizione in corso..."

"$VENV_PYTHON" -m meeting_recorder.cli \
    "$OUTPUT" \
    --output-dir "$TRANSCRIPTS_DIR" \
    --model "$MODEL" \
    "${TRANSCRIBE_ARGS[@]}" \
    --no-vad

echo ""
if [[ -f "$TRANSCRIPT" ]]; then
    echo "  ✓ Fatto!"
    echo ""
    echo "  Sessione:  $SESSION_DIR"
    echo "  Audio:     $OUTPUT"
    echo "  Testo:     $TRANSCRIPT"
    echo "  Parlanti:  ${TRANSCRIPTS_DIR}/meeting.speakers.txt"
    echo "  Sottotit.: ${TRANSCRIPTS_DIR}/meeting.srt"
    echo ""
else
    echo "  ⚠ Trascrizione terminata senza generare il transcript atteso."
    echo "  Controlla: $TRANSCRIPTS_DIR"
    echo ""
    exit 1
fi
