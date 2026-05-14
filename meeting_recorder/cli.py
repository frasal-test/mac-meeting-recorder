from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, TypeVar

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

T = TypeVar("T")


# Speaker label assigned to segments from the local microphone track.
MIC_SPEAKER_LABEL = "YOU"

AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".caf",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}


def env_file_paths() -> list[Path]:
    project_env = Path(__file__).resolve().parent.parent / ".env"
    cwd_env = Path.cwd() / ".env"
    if cwd_env == project_env:
        return [project_env]
    return [project_env, cwd_env]


def load_env_files() -> None:
    for env_path in env_file_paths():
        if not env_path.exists():
            continue

        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)


@dataclass
class Word:
    start: float
    end: float
    word: str
    probability: float | None = None


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[Word] | None = None


@dataclass
class DiarizationTurn:
    start: float
    end: float
    speaker: str


@dataclass
class Transcript:
    source: str
    model: str
    language: str | None
    language_probability: float | None
    duration: float | None
    segments: list[TranscriptSegment]
    diarization: list[DiarizationTurn] | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mac-meeting-recorder",
        description="Transcribe audio/video files with faster-whisper.",
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Audio/video file or folder to watch for new recordings.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="Where transcript files should be written. Defaults to <input>/transcripts for folders or next to the file.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep watching a folder for new recordings.",
    )
    parser.add_argument(
        "--model",
        default="base.en",
        help="faster-whisper model size/name, for example base.en, small.en, medium, large-v3.",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=("auto", "cpu", "cuda"),
        help="Inference device.",
    )
    parser.add_argument(
        "--compute-type",
        default="int8",
        help="CTranslate2 compute type, for example int8, int8_float16, float16, float32.",
    )
    parser.add_argument(
        "--language",
        help="Spoken language code such as en or it. Omit to auto-detect.",
    )
    parser.add_argument(
        "--task",
        default="transcribe",
        choices=("transcribe", "translate"),
        help="Transcribe in the source language or translate to English.",
    )
    parser.add_argument(
        "--beam-size",
        type=int,
        default=5,
        help="Beam size for decoding.",
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=0,
        help="CPU threads for faster-whisper. 0 lets CTranslate2 choose.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=10,
        help="Folder watch polling interval.",
    )
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=20,
        help="A file must be unchanged this long before transcription starts.",
    )
    parser.add_argument(
        "--word-timestamps",
        action="store_true",
        help="Include word-level timestamps in the JSON output.",
    )
    parser.add_argument(
        "--no-vad",
        action="store_true",
        help="Disable faster-whisper VAD filtering.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-transcribe files even when transcript outputs already exist.",
    )
    parser.add_argument(
        "--diarize",
        action="store_true",
        help="Add speaker labels using pyannote.audio.",
    )
    parser.add_argument(
        "--diarization-model",
        default="pyannote/speaker-diarization-3.1",
        help="pyannote diarization pipeline model.",
    )
    parser.add_argument(
        "--hf-token",
        help="Hugging Face token for gated pyannote models. Defaults to HF_TOKEN or HUGGINGFACE_TOKEN.",
    )
    parser.add_argument(
        "--num-speakers",
        type=int,
        help="Known number of speakers in the recording.",
    )
    parser.add_argument(
        "--min-speakers",
        type=int,
        help="Minimum expected speakers.",
    )
    parser.add_argument(
        "--max-speakers",
        type=int,
        help="Maximum expected speakers.",
    )
    parser.add_argument(
        "--diarization-device",
        default="cpu",
        choices=("cpu", "cuda", "mps"),
        help="Device for pyannote diarization.",
    )
    return parser.parse_args()


def companion_mic_path(source: Path) -> Path | None:
    """Return the ._mic.caf companion file left by recorder.swift, if present."""
    candidate = source.parent / (source.stem + "._mic.caf")
    return candidate if candidate.exists() else None


def is_media_file(path: Path) -> bool:
    return path.is_file() and not path.name.startswith(".") and path.suffix.lower() in AUDIO_EXTENSIONS


def output_dir_for(input_path: Path, explicit: Path | None) -> Path:
    if explicit:
        return explicit
    if input_path.is_dir():
        return input_path / "transcripts"
    return input_path.parent / "transcripts"


def transcript_base(output_dir: Path, source: Path) -> Path:
    return output_dir / source.with_suffix("").name


def transcript_exists(output_dir: Path, source: Path, diarize: bool = False) -> bool:
    base = transcript_base(output_dir, source)
    required = [base.with_suffix(".txt"), base.with_suffix(".json")]
    if diarize:
        required.extend(
            [
                base.with_suffix(".speakers.txt"),
                base.with_suffix(".rttm"),
            ]
        )
    return all(path.exists() for path in required)


def iter_media_files(input_path: Path) -> Iterable[Path]:
    if input_path.is_file():
        if is_media_file(input_path):
            yield input_path
        return

    for path in sorted(input_path.iterdir()):
        if is_media_file(path):
            yield path


def stable_enough(path: Path, stable_seconds: float) -> bool:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    age = time.time() - max(stat.st_mtime, stat.st_ctime)
    return age >= stable_seconds and stat.st_size > 0


def format_timestamp(seconds: float, separator: str = ",") -> str:
    milliseconds = round(seconds * 1000)
    hours = milliseconds // 3_600_000
    milliseconds %= 3_600_000
    minutes = milliseconds // 60_000
    milliseconds %= 60_000
    secs = milliseconds // 1000
    millis = milliseconds % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def write_txt(path: Path, transcript: Transcript) -> None:
    text = "\n".join(segment.text.strip() for segment in transcript.segments).strip()
    path.write_text(text + "\n", encoding="utf-8")


def write_srt(path: Path, transcript: Transcript) -> None:
    lines: list[str] = []
    for index, segment in enumerate(transcript.segments, start=1):
        lines.append(str(index))
        lines.append(
            f"{format_timestamp(segment.start)} --> {format_timestamp(segment.end)}"
        )
        lines.append(segment.text.strip())
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_speaker_txt(path: Path, transcript: Transcript) -> None:
    lines: list[str] = []
    current_speaker: str | None = None
    current_text: list[str] = []
    current_start: float | None = None
    current_end: float | None = None

    def flush() -> None:
        if not current_text or current_start is None or current_end is None:
            return
        speaker = current_speaker or "UNKNOWN"
        timestamp = (
            f"{format_timestamp(current_start, separator='.')} - "
            f"{format_timestamp(current_end, separator='.')}"
        )
        lines.append(f"[{timestamp}] {speaker}: {' '.join(current_text).strip()}")

    for segment in transcript.segments:
        if segment.speaker != current_speaker:
            flush()
            current_speaker = segment.speaker
            current_text = [segment.text.strip()]
            current_start = segment.start
            current_end = segment.end
        else:
            current_text.append(segment.text.strip())
            current_end = segment.end

    flush()
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def write_json(path: Path, transcript: Transcript) -> None:
    path.write_text(
        json.dumps(asdict(transcript), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def overlap_seconds(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def best_speaker_for_segment(
    segment: TranscriptSegment, diarization_turns: list[DiarizationTurn]
) -> str | None:
    overlaps: dict[str, float] = {}
    for turn in diarization_turns:
        overlap = overlap_seconds(segment.start, segment.end, turn.start, turn.end)
        if overlap > 0:
            overlaps[turn.speaker] = overlaps.get(turn.speaker, 0.0) + overlap
    if not overlaps:
        return None
    return max(overlaps.items(), key=lambda item: item[1])[0]


def extract_diarization_turns(diarization: object) -> list[DiarizationTurn]:
    turns: list[DiarizationTurn] = []

    speaker_diarization = getattr(diarization, "speaker_diarization", None)
    if speaker_diarization is not None:
        for turn, speaker in speaker_diarization:
            turns.append(
                DiarizationTurn(
                    start=float(turn.start),
                    end=float(turn.end),
                    speaker=str(speaker),
                )
            )
        return turns

    itertracks = getattr(diarization, "itertracks", None)
    if itertracks is None:
        return turns

    for turn, _, speaker in itertracks(yield_label=True):
        turns.append(
            DiarizationTurn(
                start=float(turn.start),
                end=float(turn.end),
                speaker=str(speaker),
            )
        )
    return turns


def write_rttm(path: Path, uri: str, turns: list[DiarizationTurn]) -> None:
    lines = []
    for turn in turns:
        duration = max(0.0, turn.end - turn.start)
        lines.append(
            " ".join(
                [
                    "SPEAKER",
                    uri,
                    "1",
                    f"{turn.start:.3f}",
                    f"{duration:.3f}",
                    "<NA>",
                    "<NA>",
                    turn.speaker,
                    "<NA>",
                    "<NA>",
                ]
            )
        )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")



def diarization_kwargs(args: argparse.Namespace) -> dict[str, int]:
    kwargs: dict[str, int] = {}
    if args.num_speakers is not None:
        kwargs["num_speakers"] = args.num_speakers
    if args.min_speakers is not None:
        kwargs["min_speakers"] = args.min_speakers
    if args.max_speakers is not None:
        kwargs["max_speakers"] = args.max_speakers
    return kwargs


def prepare_diarization_audio(source: Path) -> Path:
    import av

    temp_file = tempfile.NamedTemporaryFile(
        prefix=f"{source.stem}-diarization-",
        suffix=".wav",
        delete=False,
    )
    wav_path = Path(temp_file.name)
    temp_file.close()

    try:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        with av.open(str(source)) as container, wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)

            audio_streams = [stream for stream in container.streams if stream.type == "audio"]
            if not audio_streams:
                raise ValueError(f"No audio stream found in {source}")

            for frame in container.decode(audio=0):
                for resampled in resampler.resample(frame):
                    wav.writeframes(resampled.to_ndarray().tobytes())
            for resampled in resampler.resample(None):
                wav.writeframes(resampled.to_ndarray().tobytes())
    except Exception:
        wav_path.unlink(missing_ok=True)
        raise

    return wav_path


def run_with_elapsed_status(
    message: str,
    interval_seconds: float,
    callback: Callable[[], T],
) -> T:
    done = threading.Event()

    def report_progress() -> None:
        started = time.monotonic()
        while not done.wait(interval_seconds):
            elapsed = int(time.monotonic() - started)
            minutes, seconds = divmod(elapsed, 60)
            print(f"{message} still running ({minutes}m {seconds:02d}s)...", flush=True)

    reporter = threading.Thread(target=report_progress, daemon=True)
    reporter.start()
    try:
        return callback()
    finally:
        done.set()
        reporter.join(timeout=1)


def run_diarization(
    diarizer: object | None,
    source: Path,
    base: Path,
    args: argparse.Namespace,
) -> list[DiarizationTurn] | None:
    if diarizer is None:
        return None

    print(f"Diarizing {source.name}...", flush=True)
    print("Preparing normalized WAV for diarization...", flush=True)
    diarization_audio = prepare_diarization_audio(source)
    try:
        diarization_input = {
            "uri": source.stem,
            "audio": str(diarization_audio),
        }
        kwargs = diarization_kwargs(args)
        diarization = run_with_elapsed_status(
            "Diarization",
            30,
            lambda: diarizer(diarization_input, **kwargs),
        )
        turns = extract_diarization_turns(diarization)
        write_rttm(base.with_suffix(".rttm"), source.stem, turns)
        return turns
    finally:
        diarization_audio.unlink(missing_ok=True)


def collect_segments(
    model: "WhisperModel",
    audio_path: Path,
    args: argparse.Namespace,
    speaker: str | None = None,
) -> tuple[list[TranscriptSegment], object]:
    """Transcribe one audio file, print live progress, return (segments, info)."""
    segments_iter, info = model.transcribe(
        str(audio_path),
        beam_size=args.beam_size,
        language=args.language,
        task=args.task,
        vad_filter=not args.no_vad,
        word_timestamps=args.word_timestamps,
    )
    segments: list[TranscriptSegment] = []
    duration: float = getattr(info, "duration", None) or 0.0
    for segment in segments_iter:
        pct = f"{segment.end / duration * 100:.0f}%" if duration else "…"
        print(f"  [{pct}] {segment.text.strip()}", flush=True)
        words: list[Word] | None = None
        if args.word_timestamps and segment.words:
            words = [
                Word(
                    start=word.start,
                    end=word.end,
                    word=word.word,
                    probability=getattr(word, "probability", None),
                )
                for word in segment.words
            ]
        segments.append(
            TranscriptSegment(
                start=segment.start,
                end=segment.end,
                text=segment.text,
                speaker=speaker,
                words=words,
            )
        )
    return segments, info


def transcribe_file(
    model: "WhisperModel",
    diarizer: object | None,
    source: Path,
    output_dir: Path,
    args: argparse.Namespace,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    base = transcript_base(output_dir, source)
    mic_path = companion_mic_path(source)

    try:
        # System audio — remote speakers (clean digital capture via SCK)
        print(f"Transcribing {source.name}...", flush=True)
        sys_segments, info = collect_segments(model, source, args)

        # Mic track — local speaker, labelled YOU, kept separate to avoid echo
        if mic_path:
            print(f"  Transcribing mic track (local speaker)...", flush=True)
            mic_segments, _ = collect_segments(
                model, mic_path, args, speaker=MIC_SPEAKER_LABEL
            )
            # Interleave both tracks by start time
            all_segments = sorted(
                sys_segments + mic_segments, key=lambda s: s.start
            )
        else:
            all_segments = sys_segments

        # Diarize the system audio to distinguish remote speakers.
        # Mic segments already carry MIC_SPEAKER_LABEL and are not overwritten.
        diarization_turns = run_diarization(diarizer, source, base, args)
        if diarization_turns:
            for segment in all_segments:
                if segment.speaker != MIC_SPEAKER_LABEL:
                    segment.speaker = best_speaker_for_segment(
                        segment, diarization_turns
                    )

        transcript = Transcript(
            source=str(source),
            model=args.model,
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
            duration=getattr(info, "duration", None),
            segments=all_segments,
            diarization=diarization_turns,
        )

        write_txt(base.with_suffix(".txt"), transcript)
        write_srt(base.with_suffix(".srt"), transcript)
        write_json(base.with_suffix(".json"), transcript)
        written = [
            base.with_suffix(".txt").name,
            base.with_suffix(".srt").name,
            base.with_suffix(".json").name,
        ]
        if diarization_turns is not None:
            write_speaker_txt(base.with_suffix(".speakers.txt"), transcript)
            written.append(base.with_suffix(".speakers.txt").name)
            written.append(base.with_suffix(".rttm").name)
        print(f"Wrote {', '.join(written)}", flush=True)

    finally:
        # Always clean up the mic companion file, even on error
        if mic_path:
            mic_path.unlink(missing_ok=True)


def load_model(args: argparse.Namespace) -> "WhisperModel":
    from faster_whisper import WhisperModel

    kwargs = {
        "device": args.device,
        "compute_type": args.compute_type,
    }
    if args.cpu_threads > 0:
        kwargs["cpu_threads"] = args.cpu_threads
    return WhisperModel(args.model, **kwargs)


def exception_details(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(type(current).__name__)
        parts.append(str(current))
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


def is_huggingface_access_error(exc: BaseException) -> bool:
    details = exception_details(exc).lower()
    return any(
        marker in details
        for marker in (
            "gatedrepoerror",
            "gated repo",
            "403 forbidden",
            "401 unauthorized",
            "not in the authorized list",
            "cannot access gated repo",
        )
    )


def huggingface_model_urls_from_error(exc: BaseException) -> list[str]:
    details = exception_details(exc)
    model_ids = set(
        re.findall(r"pyannote/[A-Za-z0-9._-]+", details)
    )
    model_ids.update(
        {
            "pyannote/speaker-diarization-3.1",
            "pyannote/segmentation-3.0",
            "pyannote/speaker-diarization-community-1",
        }
    )
    return [f"https://huggingface.co/{model_id}" for model_id in sorted(model_ids)]


def print_diarization_access_help(model_name: str, exc: BaseException) -> None:
    model_urls = "\n".join(f"  {url}" for url in huggingface_model_urls_from_error(exc))
    print(
        "\nCannot access the pyannote diarization model on Hugging Face.\n"
        "\n"
        "Check these items:\n"
        "- Your .env file contains HF_TOKEN=hf_... with a read token.\n"
        "- The token belongs to the same Hugging Face account you use in the browser.\n"
        "- You accepted the model conditions for these pyannote repositories:\n"
        f"{model_urls}\n"
        f"- The requested diarization model is {model_name!r}.\n",
        file=sys.stderr,
    )


def load_pyannote_pipeline(pipeline_class: object, model_name: str, token: str | None) -> object:
    if not token:
        return pipeline_class.from_pretrained(model_name)  # type: ignore[attr-defined]

    try:
        return pipeline_class.from_pretrained(model_name, token=token)  # type: ignore[attr-defined]
    except TypeError:
        return pipeline_class.from_pretrained(  # type: ignore[attr-defined]
            model_name,
            use_auth_token=token,
        )


def load_diarizer(args: argparse.Namespace) -> object | None:
    if not args.diarize:
        return None

    try:
        from pyannote.audio import Pipeline
    except ImportError:
        print(
            "pyannote.audio is not installed. Install requirements-diarization.txt to use --diarize.",
            file=sys.stderr,
        )
        raise

    token = args.hf_token or os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")

    try:
        pipeline = load_pyannote_pipeline(Pipeline, args.diarization_model, token)
    except Exception as exc:
        if is_huggingface_access_error(exc):
            print_diarization_access_help(args.diarization_model, exc)
            raise SystemExit(2)
        raise

    import torch

    if args.diarization_device != "cpu":
        device = args.diarization_device
    elif torch.backends.mps.is_available():
        device = "mps"
    elif torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    print(f"Diarization device: {device}", flush=True)
    pipeline.to(torch.device(device))
    return pipeline


def process_once(
    args: argparse.Namespace, model: "WhisperModel", diarizer: object | None
) -> int:
    input_path = args.input.expanduser().resolve()
    output_dir = output_dir_for(input_path, args.output_dir)
    files = list(iter_media_files(input_path))
    if not files:
        print(f"No supported media files found in {input_path}", file=sys.stderr)
        return 1

    for source in files:
        if not args.force and transcript_exists(output_dir, source, args.diarize):
            print(f"Skipping {source.name}; transcript already exists.", flush=True)
            continue
        if input_path.is_dir() and not stable_enough(source, args.stable_seconds):
            print(f"Skipping {source.name}; file is still changing.", flush=True)
            continue
        transcribe_file(model, diarizer, source, output_dir, args)
    return 0


def watch(
    args: argparse.Namespace, model: "WhisperModel", diarizer: object | None
) -> int:
    input_path = args.input.expanduser().resolve()
    if not input_path.is_dir():
        print("--watch requires a folder input.", file=sys.stderr)
        return 1

    output_dir = output_dir_for(input_path, args.output_dir)
    print(f"Watching {input_path}", flush=True)
    print(f"Writing transcripts to {output_dir}", flush=True)

    failed: set[Path] = set()
    while True:
        for source in iter_media_files(input_path):
            if source in failed:
                continue
            if not args.force and transcript_exists(output_dir, source, args.diarize):
                continue
            if not stable_enough(source, args.stable_seconds):
                continue
            try:
                transcribe_file(model, diarizer, source, output_dir, args)
            except Exception as exc:
                print(f"Error transcribing {source.name}: {exc}", file=sys.stderr, flush=True)
                print(f"Skipping {source.name} for the rest of this session.", file=sys.stderr, flush=True)
                failed.add(source)
        time.sleep(args.poll_seconds)


def main() -> int:
    load_env_files()
    args = parse_args()
    if not args.input.exists():
        print(f"Input does not exist: {args.input}", file=sys.stderr)
        return 1

    model = load_model(args)
    diarizer = load_diarizer(args)
    if args.watch:
        return watch(args, model, diarizer)
    return process_once(args, model, diarizer)


if __name__ == "__main__":
    raise SystemExit(main())
