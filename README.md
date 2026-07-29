# TapRecord Whisper

Local-first macOS meeting recorder and transcription pipeline.

TapRecord records microphone and system audio into **two persistent tracks**,
transcribes them locally with `faster-whisper`, and merges both timelines using
their real start offsets. The microphone track is deterministically labelled
`ME`; optional pyannote diarization is applied only to the system track to
separate remote participants.

Audio and transcripts stay on the Mac.

## What changed

- Persistent `mic.caf` and `system.caf` tracks: no destructive pre-transcription
  mix.
- Atomic `meta.json` with timestamps, track offsets, duration and session state.
- Persistent `job.json` queue with retry counters and interrupted-job recovery.
- Serial worker: a new meeting can start while the previous one transcribes.
- Combined TXT, SRT, JSON, Markdown, speaker transcript and optional RTTM.
- Configurable `on_stop` hook.
- `doctor` diagnostics.
- Optional menu-bar daemon and LaunchAgent.
- Separate faster-whisper versus FluidAudio/Parakeet benchmark harness.

## Requirements

- macOS 13 or later
- Xcode Command Line Tools
- Python 3.9+
- Headphones recommended

Create the Python environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

For remote-speaker diarization:

```bash
.venv/bin/pip install -r requirements-diarization.txt
```

Add `HF_TOKEN=hf_...` to `.env` and accept the model conditions for:

- <https://huggingface.co/pyannote/segmentation-3.0>
- <https://huggingface.co/pyannote/speaker-diarization-3.1>

On first recording, grant **TapRecord Recorder** access to:

- Screen & System Audio Recording
- Microphone

For terminal-only use, macOS may also attribute the request to Terminal.

## Record from the terminal

```bash
./meet.sh [language] [diar|nodiar] [remote_speakers]
```

Examples:

```bash
./meet.sh
./meet.sh en
./meet.sh es nodiar
./meet.sh auto nodiar
./meet.sh it diar 2
```

Press Enter to stop. Recording returns immediately after the session is queued;
transcription continues in the background. To retain the previous synchronous
behaviour:

```bash
WAIT_FOR_TRANSCRIPT=1 ./meet.sh it diar 2
```

## Session format

```text
recordings/
└── 2026-07-29T10-30-00/
    ├── meta.json
    ├── job.json
    ├── transcribe.log
    ├── audio/
    │   ├── mic.caf
    │   └── system.caf
    └── transcripts/
        ├── transcript.txt
        ├── transcript.srt
        ├── transcript.json
        ├── transcript.md
        ├── transcript.speakers.txt
        └── transcript.rttm
```

`transcript.json` is the canonical output. Every segment contains its timeline,
speaker and source track. Word timestamps are shifted by the same track offset.

## Queue and recovery

```bash
# Queue or requeue one session
.venv/bin/python -m meeting_recorder.control enqueue <session>
.venv/bin/python -m meeting_recorder.control enqueue <session> --force

# Process one session synchronously
.venv/bin/python -m meeting_recorder.control process <session>

# Recover sessions left without a transcript
.venv/bin/python -m meeting_recorder.control recover

# Process the pending queue
.venv/bin/python -m meeting_recorder.control worker --once
```

Only one worker processes sessions at a time. A job found in `processing` state
after an interrupted worker is returned to `pending`. Failed jobs remain
retryable until `max_attempts` is reached.

## Configuration and hook

The optional config file is `~/.config/taprecord/config.json`. Start from
[`config.example.json`](config.example.json).

`on_stop` is executed after all transcript files have been written. The session
directory is available both as the first positional argument and as
`TAPRECORD_SESSION_DIR`. A failed hook is logged but does not invalidate a valid
transcript.

```json
{
  "on_stop": "/path/to/archive-session",
  "transcription": {
    "model": "medium",
    "language": "it",
    "diarize_system": true,
    "max_attempts": 3
  }
}
```

## Doctor

```bash
./taprecord.sh doctor
```

It checks macOS, the Swift build, Python models, Hugging Face configuration,
recordings directory and macOS recording permissions.

## Menu bar and launch at login

```bash
# Build and run in the foreground
./taprecord.sh run

# Build, install a LaunchAgent and start it
./taprecord.sh install

# Disable the LaunchAgent; its plist is preserved as .disabled
./taprecord.sh uninstall
```

The menu bar offers Italian, English, Spanish and automatic-language recording
presets, plus Stop, elapsed time, recordings folder and doctor. It queues
transcription with the selected language/model and posts a macOS notification
when the job completes.

## Transcribe existing media

The original file/folder workflow remains available:

```bash
.venv/bin/python -m meeting_recorder.cli recording.m4a \
  --model medium \
  --language it \
  --diarize
```

Supported outputs now also include Markdown.

## FluidAudio / Parakeet benchmark

The benchmark is deliberately separate from production transcription. Current
FluidAudio provides Parakeet v2 for English and v3 for multilingual evaluation,
but adopting either engine should be based on representative meeting audio,
technical vocabulary and measured WER.

Run faster-whisper:

```bash
.venv/bin/python -m meeting_recorder.benchmark whisper sample.caf \
  --reference reference.txt \
  --model medium \
  --language it \
  --output benchmark/whisper.md
```

Run FluidAudio from a local FluidAudio checkout:

```bash
.venv/bin/python -m meeting_recorder.benchmark fluidaudio sample.caf \
  --reference reference.txt \
  --fluidaudio-dir /path/to/FluidAudio \
  --model-version v3 \
  --output benchmark/parakeet-v3.md
```

Evaluate pre-generated transcripts:

```bash
.venv/bin/python -m meeting_recorder.benchmark evaluate \
  --reference reference.txt \
  --candidate whisper=whisper.txt \
  --candidate parakeet=parakeet.txt \
  --output benchmark/comparison.md
```

The report records WER, elapsed time and real-time factor. At least three
representative samples are recommended: Italian presales vocabulary, English,
and a mixed-language meeting.

## Privacy and legal note

Processing is local. Models are downloaded once and cached locally. The optional
`on_stop` hook is user-controlled and may change that privacy boundary.

Recording meetings may require participant consent and compliance with company
policy and applicable law.

## Acknowledgements

The two-track recording architecture, recoverable session model, background
transcription queue, menu-bar workflow and post-processing hook were inspired
by [digimata/quill](https://github.com/digimata/quill). TapRecord retains its
own implementation and combines those architectural ideas with its existing
multilingual `faster-whisper` pipeline, optional pyannote diarization and
multi-format transcript outputs.

## License

MIT
