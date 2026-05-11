#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIO_DIR="${AUDIO_DIR:-$SCRIPT_DIR/recordings}"
MODEL="${WHISPER_MODEL:-medium}"

if [[ ! -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  echo "Missing local Python environment: $SCRIPT_DIR/.venv" >&2
  echo "Run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements-diarization.txt" >&2
  exit 1
fi

mkdir -p "$AUDIO_DIR"

exec "$SCRIPT_DIR/.venv/bin/python" -m taprecord_whisper.cli \
  "$AUDIO_DIR" \
  --watch \
  --model "$MODEL" \
  --language it \
  --diarize \
  "$@"
