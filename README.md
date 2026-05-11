# TapRecord Whisper

Small local pipeline for using TapRecord as the recorder and `faster-whisper` as the transcription engine.

TapRecord handles the macOS/Zoom capture permissions. This tool watches the recordings folder, waits until a file stops changing, then writes:

- `.txt` plain transcript
- `.srt` captions
- `.json` structured segments and optional word timestamps
- optional `.speakers.txt` and `.rttm` speaker diarization output

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

For speaker labels:

```bash
pip install -r requirements-diarization.txt
```

Add your Hugging Face read token to `.env`:

```bash
HF_TOKEN=hf_...
```

## Transcribe One Recording

```bash
python -m taprecord_whisper.cli "/path/to/TapRecord Recording.m4a"
```

## Watch TapRecord Folder

```bash
python -m taprecord_whisper.cli "$HOME/Music/TapRecord" --watch
```

By default, folder mode writes outputs to a `transcripts` folder inside the input folder.

## Meeting Launchers

For the local TapRecord audio folder at `/Users/francescosalerno/Movies/TapRecord/Audio`,
use the helper scripts:

```bash
./meeting-it.sh
./meeting-en.sh
```

Both scripts watch the TapRecord `Audio` folder, enable diarization, and let pyannote
infer the number of speakers. The Italian launcher uses `--model medium --language it`.
The English launcher uses `--model medium.en --language en`.

You can override the folder or model when needed:

```bash
TAPRECORD_AUDIO_DIR="/other/audio/folder" ./meeting-it.sh
WHISPER_MODEL=large-v3 ./meeting-en.sh
```

## Useful Options

```bash
python -m taprecord_whisper.cli "$HOME/Music/TapRecord" \
  --watch \
  --model small.en \
  --language en \
  --word-timestamps
```

For multilingual meetings, use a multilingual model such as `small`, `medium`, or `large-v3` instead of `.en`.

On Apple Silicon, CPU with `--compute-type int8` is usually the easiest first run. If you have a CUDA GPU elsewhere, use `--device cuda --compute-type float16`.

## Speaker Labels

Speaker diarization separates the audio into labels such as `SPEAKER_00`, `SPEAKER_01`, and attaches the best-matching label to each transcript segment. It does not know real names unless you rename them afterward.

The default diarization model is `pyannote/speaker-diarization-3.1`. It requires:

- accepting the Hugging Face user conditions for `pyannote/segmentation-3.0`
- accepting the Hugging Face user conditions for `pyannote/speaker-diarization-3.1`
- accepting the Hugging Face user conditions for `pyannote/speaker-diarization-community-1` when using newer `pyannote.audio` versions
- a Hugging Face read token in `.env` for the same account that accepted the model conditions

Run with diarization:

```bash
HF_TOKEN="hf_..." python -m taprecord_whisper.cli "/path/to/TapRecord Recording.m4a" \
  --diarize \
  --num-speakers 2
```

Or watch a folder:

```bash
HF_TOKEN="hf_..." python -m taprecord_whisper.cli "$HOME/Music/TapRecord" \
  --watch \
  --diarize \
  --min-speakers 2 \
  --max-speakers 6
```

Speaker output files:

- `.speakers.txt`: readable transcript grouped by speaker
- `.rttm`: raw diarization turns
- `.json`: transcript segments include a `speaker` field and a `diarization` list

For diarization, the app converts each source recording to a temporary mono 16 kHz
WAV before calling pyannote. This avoids sample-count issues that can happen when
pyannote reads compressed files such as `.m4a` directly.

## Notes

- The watcher only processes files with common audio/video extensions.
- `--stable-seconds` prevents transcription from starting while TapRecord is still writing.
- `--force` re-runs transcription even when outputs already exist.
- Recording meetings may require consent depending on your jurisdiction and company policy.
