# mac-meeting-recorder

Record and transcribe Zoom/Teams meetings on macOS — locally, no cloud, no subscriptions.

Captures **system audio** (remote participants) and **microphone** (you) simultaneously, mixes them, and transcribes everything with [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Optional speaker diarization via [pyannote.audio](https://github.com/pyannote/pyannote-audio).

## How it works

- **Recording** — a Swift CLI uses [ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit) for system audio and `AVAudioEngine` for the microphone. The two streams are mixed into a single `.m4a` via `AVFoundation`. No virtual audio drivers, no device switching.
- **Transcription** — a Python watcher picks up new files and runs `faster-whisper` on them, producing `.txt`, `.srt`, and `.json` outputs.
- **Diarization** — optional speaker labels (`SPEAKER_00`, `SPEAKER_01`, …) via pyannote, written to `.speakers.txt` and `.rttm`.

## Requirements

- macOS 13 or later
- Xcode Command Line Tools (`xcode-select --install`)
- Python 3.9+
- [ffmpeg](https://ffmpeg.org) (`brew install ffmpeg`) — used by PyAV for audio decoding

## Setup

```bash
git clone https://github.com/frasal-test/mac-meeting-recorder.git
cd mac-meeting-recorder

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

For speaker diarization:

```bash
.venv/bin/pip install -r requirements-diarization.txt
```

Add your [Hugging Face](https://huggingface.co) read token to `.env` (required for pyannote gated models):

```bash
echo "HF_TOKEN=hf_..." > .env
```

You also need to accept the model conditions on Hugging Face for:
- [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
- [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)

### Permissions (one-time)

Grant **Terminal** access in **System Settings → Privacy & Security**:
- **Screen & System Audio Recording** — for remote participants' audio
- **Microphone** — for your own voice

## Usage

```bash
./meet.sh        # Italian (default)
./meet.sh en     # English
```

That's it. One terminal, one command.

```
  Lingua: it | Modello: medium
  ● Registrazione in corso — premi Invio per fermare

  [Invio]

  ⏳ Trascrizione in corso...

  ✓ Fatto!

  Testo:     recordings/transcripts/2025-05-11T15-30-00.txt
  Parlanti:  recordings/transcripts/2025-05-11T15-30-00.speakers.txt
  Sottotit.: recordings/transcripts/2025-05-11T15-30-00.srt
```

### Output files

| File | Description |
|------|-------------|
| `.txt` | Plain transcript |
| `.srt` | Subtitles with timestamps |
| `.json` | Structured segments, word timestamps, metadata |
| `.speakers.txt` | Transcript grouped by speaker |
| `.rttm` | Raw diarization turns |

### Override model or recordings folder

```bash
WHISPER_MODEL=large-v3 ./meet.sh
RECORDINGS_DIR=/path/to/folder ./meet.sh en
```

## Standalone recording

If you just want to record without the watcher:

```bash
./record.sh
# Ctrl+C to stop
```

## Advanced options

The transcription engine accepts additional flags via the CLI directly:

```bash
.venv/bin/python -m meeting_recorder.cli recordings/ \
  --watch \
  --model large-v3 \
  --language it \
  --diarize \
  --word-timestamps
```

On Apple Silicon, `--compute-type int8` (default) is the fastest option.
With a CUDA GPU: `--device cuda --compute-type float16`.

## Legal note

Recording meetings may require consent depending on your jurisdiction and company policy. Make sure all participants are aware the call is being recorded.

## License

MIT
