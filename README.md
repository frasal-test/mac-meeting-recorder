# MeetRec

Local-first macOS meeting recorder and transcription pipeline. It records your
microphone and the other participants into two separate tracks and transcribes
them on your Mac. Audio and transcripts never leave the machine.

## Quickstart

macOS 13 or later. About 20 minutes, and roughly 3 GB of downloads.

```bash
git clone https://github.com/frasal-test/meetrec.git
cd meetrec
xcode-select --install    # skip if `swiftc --version` already prints a version
python3 -m venv .venv
.venv/bin/pip install -r requirements-diarization.txt
```

`diarize_system` is **on by default**, so `requirements-diarization.txt` is the
file that matches the shipped configuration. Installing `requirements.txt`
alone still transcribes — the session is written without speaker separation and
carries a warning saying so — but to make that the deliberate choice, set
`"diarize_system": false` in the [config file](#configuration-and-hook).

Separating remote speakers needs a Hugging Face **read** token. Sign in, accept
the model conditions on both
[segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0) and
[speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1),
then write the token to `.env` in the project root:

```bash
echo 'HF_TOKEN=hf_your_token_here' > .env
```

In **Keychain Access**, create a self-signed certificate named `MeetRec Dev`
(*Certificate Assistant → Create a Certificate*, Identity Type **Self Signed
Root**, Certificate Type **Code Signing**). Without it macOS re-asks for
microphone and screen-recording permission after every rebuild — see
[Stable signing](#stable-signing). Then build and install:

```bash
./meetrec.sh install
```

A ✒︎ icon appears in the menu bar. Start a recording from it and grant
**Microphone** and **Screen & System Audio Recording** when macOS asks, restart
MeetRec so the screen grant takes effect, and check the result:

```bash
./meetrec.sh doctor
```

Wear headphones — otherwise your microphone picks up the other participants
through the speakers and they are transcribed twice. The first transcription
downloads the `medium` model (1.4 GB) before producing anything; transcripts
land in `recordings/<timestamp>/transcripts/`.

**New to the terminal, or something above did not work?**
[INSTALL.md](INSTALL.md) walks through the same steps one at a time, with what
each one should print and how to fix it when it does not.

## How it works

MeetRec records microphone and system audio into **two persistent tracks**,
transcribes them locally with `faster-whisper`, and merges both timelines using
their real start offsets. The microphone track is deterministically labelled
`ME`; optional pyannote diarization is applied only to the system track to
separate remote participants.

`faster-whisper` is the production transcription engine for every recording
preset (Italian, English, Spanish and automatic language detection).

Audio and transcripts stay on the Mac.

The menu-bar application captures both audio tracks directly in its own
process. This gives macOS one stable app identity for Microphone and Screen &
System Audio permissions. The terminal command uses the same shared Swift
recording core through a separate CLI executable.

## What changed

- Persistent `mic.caf` and `system.caf` tracks: no destructive pre-transcription
  mix.
- Atomic `meta.json` with timestamps, track offsets, duration and session state.
- Persistent `job.json` queue with retry counters and interrupted-job recovery.
- Serial worker: a new meeting can start while the previous one transcribes.
- Combined TXT, SRT, JSON, Markdown, speaker transcript and optional RTTM.
- Configurable `on_stop` hook.
- `doctor` diagnostics.
- Menu-bar daemon with recording timer, persistent transcription progress and
  LaunchAgent.
- Speech leveling and filtering (VAD, silence-hallucination removal) on by
  default, so a quiet microphone is neither invented over nor discarded.

## Requirements

- macOS 13 or later
- Xcode Command Line Tools
- Python 3.9+
- A Hugging Face read token, for remote-speaker diarization only
- Headphones recommended

`requirements.txt` installs the transcription engine alone;
`requirements-diarization.txt` adds pyannote and PyTorch for remote-speaker
separation. Since `diarize_system` defaults to `true`, the second file is the
one that matches the default configuration.

The [Quickstart](#quickstart) has the commands; [INSTALL.md](INSTALL.md) has the
same sequence with verification and failure modes for each step.

On first menu-bar recording, grant **MeetRec** access to Screen & System Audio
Recording and to the Microphone. For terminal-only use, grant the separately
listed **MeetRec Recorder** process; macOS may also attribute the request to
Terminal.

### Stable signing

Do this once, or macOS re-asks for permissions after every rebuild.

Without a signing certificate the build falls back to an ad-hoc signature,
whose designated requirement is the binary's own `cdhash`. Every recompile
produces a new hash, so the grant macOS stored no longer matches and it prompts
again — while the old entry stays in the Privacy pane, switched on but inert.

Create a self-signed code-signing certificate:

1. Open **Keychain Access**.
2. Menu **Keychain Access → Certificate Assistant → Create a Certificate…**
3. Name: `MeetRec Dev`
4. Identity Type: **Self Signed Root**
5. Certificate Type: **Code Signing**
6. **Create**, then **Done**.

Confirm it is usable — it must appear in:

```bash
security find-identity -v -p codesigning
```

If it does not, open it in Keychain Access, expand **Trust**, and set **Code
Signing** to *Always Trust*.

Builds pick it up automatically. Override the name with `MEETREC_SIGN_IDENTITY`
if you prefer a different one.

If MeetRec had already run ad-hoc signed, clear the grants it left behind so the
next launch registers cleanly. On a fresh install there is nothing to reset and
these two commands can be skipped:

```bash
tccutil reset ScreenCapture com.frasal.meetrec.menubar
```

```bash
tccutil reset Microphone com.frasal.meetrec.menubar
```

Rebuild, restart the agent, and grant the prompts one final time.

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

When speaker separation cannot run at all — pyannote is not installed, or the
Hugging Face token is refused — the session is still transcribed, without
speaker separation and with every remote participant labelled `REMOTE`. Neither
cause changes on a retry, and a valid recording is worth more than the labels
put on it. The reason is recorded in `transcript.json` under `warnings` and
printed at the top of the readable transcripts, so it is visible to whoever
opens the meeting rather than only to whoever reads the log. Any other failure
still fails the job and retries, because there a retry is the point.

Only one worker processes sessions at a time. Every worker pass scans the entire
pending queue, including sessions left by an earlier failure. The menu-bar app
also runs a recovery pass when it starts. A job found in `processing` state
after an interrupted worker is returned to `pending`; failed jobs remain
retryable on subsequent passes until `max_attempts` is reached.

## Configuration and hook

The optional config file is `~/.config/meetrec/config.json`. Start from
[`config.example.json`](config.example.json).

`on_stop` is executed after all transcript files have been written. The session
directory is available both as the first positional argument and as
`MEETREC_SESSION_DIR`. A failed hook is logged but does not invalidate a valid
transcript.

```json
{
  "on_stop": "/path/to/archive-session",
  "transcription": {
    "model": "medium",
    "language": "it",
    "compute_type": "int8",
    "diarize_system": true,
    "max_attempts": 3,
    "vad_filter": true,
    "hallucination_silence_threshold": 2.0,
    "condition_on_previous_text": true,
    "normalize_audio": true,
    "target_speech_dbfs": -20.0
  }
}
```

### Speech leveling and filtering

These four settings are **on by default and work as a set**. Whoever wears the
microphone spends most of a meeting listening, and Whisper handed that silence
decodes its own priors into filler — one hour-long session produced 88
identical `Okay.` segments over stretches measuring -80 dBFS. Filtering alone
is not the answer either: the VAD judges absolute energy, so on a Bluetooth
headset mic recorded 17 dB below the system track it discarded 82% of the words
its owner really spoke. Levelling first is what makes filtering safe.

- `normalize_audio` — brings the speech in each track to `target_speech_dbfs`
  before decoding. The measurement is gated: near-silent blocks are excluded,
  so a track that is 79% silence is leveled by what its speech needs rather
  than by an average the silence dominates.
- `target_speech_dbfs` — where leveled speech should land. `-20.0` suits
  speech; peaks are allowed to clip, since a gated measurement deliberately
  ignores the isolated transients that reach full scale.
- `vad_filter` — faster-whisper's voice activity detection, which keeps the
  decoder away from silence. Turn it off only for a recording you would rather
  over-transcribe than lose.
- `hallucination_silence_threshold` — seconds of silence after which a segment
  is discarded as a hallucination, as a backstop to the VAD. `null` disables it.
- `condition_on_previous_text` — each window is decoded conditioned on the
  previous one. That keeps a conversation coherent, but also lets one
  hallucination seed the next window: set it to `false` to break a recording
  stuck repeating a phrase or emitting segments minutes long.

`compute_type` accepts any CTranslate2 type. `int8` is the fastest; on Apple
Silicon `int8_float32` or `float32` transcribe a weak microphone more
accurately at some cost in speed.

## Doctor

```bash
./meetrec.sh doctor
```

It checks macOS, the Swift build, Python models, Hugging Face configuration,
recordings directory and macOS recording permissions.

## Menu bar and launch at login

```bash
# Build and run in the foreground
./meetrec.sh run

# Build, install a LaunchAgent and start it
./meetrec.sh install

# Disable the LaunchAgent; its plist is preserved as .disabled
./meetrec.sh uninstall
```

The menu bar offers Italian, English, Spanish and automatic-language recording
presets, plus Stop, elapsed time, recordings folder and doctor. It queues
transcription with the selected language/model and posts a macOS notification
when the job completes.

While a session is processed, the status item shows the current percentage and
the menu contains a progress bar with the active phase: faster-whisper model
loading, microphone transcription, system-audio transcription, pyannote
loading, remote-speaker diarization, timeline merge and output generation.
The diarization phase uses an animated indeterminate bar because pyannote does
not expose a reliable numeric completion percentage.

`./meetrec.sh build` and `./meetrec.sh install` rebuild an executable only
when its Swift sources or embedded Info.plist changed. This avoids replacing an
unchanged ad-hoc-signed binary and unnecessarily invalidating macOS privacy
permission records. The build also applies explicit, stable bundle identifiers
to the menu-bar application and terminal executable. The generated
`MeetRec.app` is a standard macOS application bundle and can also be launched
directly from Finder.

## Privacy and legal note

Processing is local. Models are downloaded once and cached locally. The optional
`on_stop` hook is user-controlled and may change that privacy boundary.

Recording meetings may require participant consent and compliance with company
policy and applicable law.

## Acknowledgements

The two-track recording architecture, recoverable session model, background
transcription queue, menu-bar workflow and post-processing hook were inspired
by [digimata/quill](https://github.com/digimata/quill). MeetRec retains its
own implementation and combines those architectural ideas with its existing
multilingual `faster-whisper` pipeline, optional pyannote diarization and
multi-format transcript outputs.

## License

MIT
