#!/usr/bin/env bash
# Record a meeting into two persistent tracks and enqueue transcription.
#
# Usage:
#   ./meet.sh                    # Italian, system-track diarization
#   ./meet.sh en                 # English
#   ./meet.sh es nodiar          # Spanish, deterministic ME/REMOTE labels
#   ./meet.sh auto nodiar        # Automatic language detection
#   ./meet.sh it diar 2          # Expect two remote speakers
set -euo pipefail

MEETING_LANG="${1:-it}"
DIARIZE="${2:-diar}"
NUM_SPEAKERS="${3:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECORDER_BINARY="$SCRIPT_DIR/.recorder"
RECORDINGS_DIR="${RECORDINGS_DIR:-$SCRIPT_DIR/recordings}"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
WAIT_FOR_TRANSCRIPT="${WAIT_FOR_TRANSCRIPT:-0}"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Missing Python environment: $SCRIPT_DIR/.venv" >&2
    echo "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements-diarization.txt" >&2
    exit 1
fi
if [[ "$MEETING_LANG" != "it" && "$MEETING_LANG" != "en" && "$MEETING_LANG" != "es" && "$MEETING_LANG" != "auto" ]]; then
    echo "Usage: $0 [it|en|es|auto] [diar|nodiar] [num_speakers]" >&2
    exit 1
fi
if [[ "$DIARIZE" != "diar" && "$DIARIZE" != "nodiar" ]]; then
    echo "Usage: $0 [it|en|es|auto] [diar|nodiar] [num_speakers]" >&2
    exit 1
fi
if [[ -n "$NUM_SPEAKERS" && ! "$NUM_SPEAKERS" =~ ^[1-9][0-9]*$ ]]; then
    echo "num_speakers must be a positive integer." >&2
    exit 1
fi

# taprecord.sh owns the build rules, including the staleness check and the
# ad-hoc signature. Re-signing on every run would reset the TCC grants.
"$SCRIPT_DIR/taprecord.sh" build-recorder

# Always the multilingual model. Whisper's .en variants are marginally better
# on pure English and cannot transcribe anything else at all - nor even run
# language detection - so picking one turns "this meeting is in English" into
# "no other language will be recoverable, whatever happens in the room". A
# call that switches to Italian halfway is worth more than a fraction of a
# WER point.
MODEL="${WHISPER_MODEL:-medium}"

TIMESTAMP="$(date '+%Y-%m-%dT%H-%M-%S')"
SESSION_DIR="$RECORDINGS_DIR/$TIMESTAMP"
mkdir -p "$SESSION_DIR"

echo ""
echo "  Lingua: $MEETING_LANG | Modello: $MODEL | Diarizzazione system: $DIARIZE"
echo "  Sessione: $SESSION_DIR"
echo "  ● Registrazione in corso — premi Invio per fermare"
echo ""

"$RECORDER_BINARY" "$SESSION_DIR"

if [[ ! -s "$SESSION_DIR/audio/mic.caf" && ! -s "$SESSION_DIR/audio/system.caf" ]]; then
    echo "Recording failed or produced no audio tracks: $SESSION_DIR" >&2
    exit 1
fi

CONTROL_ARGS=(
    --config "${TAPRECORD_CONFIG:-$HOME/.config/taprecord/config.json}"
    enqueue "$SESSION_DIR"
    --model "$MODEL"
    --language "$MEETING_LANG"
)
if [[ "$DIARIZE" == "diar" ]]; then
    CONTROL_ARGS+=(--diarize-system)
else
    CONTROL_ARGS+=(--no-diarize-system)
fi
if [[ -n "$NUM_SPEAKERS" ]]; then
    CONTROL_ARGS+=(--num-speakers "$NUM_SPEAKERS")
fi

"$VENV_PYTHON" -m meeting_recorder.control "${CONTROL_ARGS[@]}"

echo ""
if [[ "$WAIT_FOR_TRANSCRIPT" == "1" ]]; then
    "$VENV_PYTHON" -m meeting_recorder.control \
        --config "${TAPRECORD_CONFIG:-$HOME/.config/taprecord/config.json}" \
        worker --once
    echo "  ✓ Trascrizione completata: $SESSION_DIR/transcripts/transcript.md"
else
    nohup "$VENV_PYTHON" -m meeting_recorder.control \
        --config "${TAPRECORD_CONFIG:-$HOME/.config/taprecord/config.json}" \
        worker --once \
        >>"$SESSION_DIR/transcribe.log" 2>&1 &
    WORKER_PID=$!
    echo "  ✓ Registrazione salvata; trascrizione in coda (PID $WORKER_PID)."
    echo "  Stato: $SESSION_DIR/job.json"
    echo "  Log:   $SESSION_DIR/transcribe.log"
fi
echo ""
