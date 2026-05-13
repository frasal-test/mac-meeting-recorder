# mac-meeting-recorder

Record and transcribe Zoom/Teams meetings on macOS — locally, no cloud, no subscriptions.

Captures **system audio** (remote participants) and **microphone** (you) simultaneously, applies echo cancellation, mixes them, and transcribes everything with [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Optional speaker diarization via [pyannote.audio](https://github.com/pyannote/pyannote-audio).

**Audio and transcripts never leave your machine.**

## How it works

- **Recording** — a Swift CLI uses [ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit) for system audio (remote participants) and `AVAudioEngine` for the microphone (you). Echo cancellation (AEC) is applied to the microphone so remote audio played through speakers isn't captured twice. The two streams are mixed into a single `.m4a` via `AVFoundation`. No virtual audio drivers, no device switching.
- **Transcription** — a Python watcher picks up new files and runs `faster-whisper` on them, showing live segments and progress. Outputs `.txt`, `.srt`, and `.json`.
- **Diarization** — optional speaker labels (`SPEAKER_00`, `SPEAKER_01`, …) via pyannote. Automatically uses MPS (Apple Silicon GPU), CUDA, or CPU — whichever is available.

## Requirements

- macOS 13 or later (macOS 14+ recommended for echo cancellation)
- Xcode Command Line Tools (`xcode-select --install`)
- Python 3.9+
- [ffmpeg](https://ffmpeg.org) (`brew install ffmpeg`)

## Setup

```bash
git clone https://github.com/frasal-test/mac-meeting-recorder.git
cd mac-meeting-recorder

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

For speaker diarization (optional):

```bash
.venv/bin/pip install -r requirements-diarization.txt
```

Add your [Hugging Face](https://huggingface.co) read token to `.env` (required only for diarization):

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
./meet.sh [language] [diar|nodiar]
```

| Argument | Options | Default |
|----------|---------|---------|
| language | `it` `en` `es` `auto` | `it` |
| diarization | `diar` `nodiar` | `diar` |

### Examples

```bash
./meet.sh              # Italian + diarization
./meet.sh en           # English + diarization
./meet.sh es           # Spanish + diarization
./meet.sh auto         # auto-detect language + diarization
./meet.sh es nodiar    # Spanish, transcript only (no HF token needed)
./meet.sh auto nodiar  # auto-detect, no diarization
```

### What you see

```
  Lingua: es | Modello: medium | Diarizzazione: diar
  ● Registrazione in corso — premi Invio per fermare

  [Invio]

  ⏳ Trascrizione in corso...
  [4%] Hola a todos, empezamos la llamada.
  [9%] Como decía ayer...

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
| `.speakers.txt` | Transcript grouped by speaker (requires diarization) |
| `.rttm` | Raw diarization turns (requires diarization) |

## Language detection

- Pass a specific language (`it`, `en`, `es`) when the call has one predominant language — faster and more accurate.
- Use `auto` for calls that mix languages: Whisper detects from the first 30 seconds and applies it to the whole file.
- If the call starts in one language and switches to another, force the predominant language explicitly for best results.

## Diarization notes

- Diarization requires a free [Hugging Face](https://huggingface.co) account and accepting the pyannote model terms.
- Device is selected automatically: **MPS** (Apple Silicon) → **CUDA** → **CPU**.
- On an M3 Pro with MPS, expect ~5 min for a 1-hour call. On CPU alone, expect 20-30 min.
- Use `nodiar` to skip diarization entirely — no HF account needed, transcription only.

## Transcribe an existing file

```bash
.venv/bin/python -m meeting_recorder.cli recordings/my-meeting.m4a \
  --model medium --language es --diarize
```

Add `--force` to re-transcribe a file that already has a transcript.

## Privacy

All processing is fully local:
- Models are downloaded from Hugging Face once and cached in `~/.cache/huggingface/hub/`
- After the initial download, the tool works completely offline (VPN-safe)
- Audio files stay in `recordings/`, transcripts in `recordings/transcripts/`
- Nothing is sent to any external service

## Advanced options

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
