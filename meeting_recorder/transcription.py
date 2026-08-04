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
    track: str | None = None
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
    # Carried into the human-readable outputs, not just the log: a transcript
    # produced with a degraded pipeline has to say so where it will actually be
    # read, which is the transcript itself.
    warnings: list[str] | None = None


class DiarizationUnavailable(RuntimeError):
    """Speaker separation cannot run, and retrying will not change that."""


def format_timestamp(seconds: float, separator: str = ",") -> str:
    milliseconds = round(seconds * 1000)
    hours = milliseconds // 3_600_000
    milliseconds %= 3_600_000
    minutes = milliseconds // 60_000
    milliseconds %= 60_000
    secs = milliseconds // 1000
    millis = milliseconds % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def warning_lines(transcript: Transcript, prefix: str) -> list[str]:
    return [f"{prefix}{warning}" for warning in transcript.warnings or []]


def write_txt(path: Path, transcript: Transcript) -> None:
    text = "\n".join(segment.text.strip() for segment in transcript.segments).strip()
    header = warning_lines(transcript, "[MeetRec] ")
    if header:
        text = "\n".join(header) + "\n\n" + text
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
        if current_start is None or segment.speaker != current_speaker:
            flush()
            current_speaker = segment.speaker
            current_text = [segment.text.strip()]
            current_start = segment.start
            current_end = segment.end
        else:
            current_text.append(segment.text.strip())
            current_end = segment.end

    flush()
    body = "\n".join(lines).strip()
    header = warning_lines(transcript, "[MeetRec] ")
    if header:
        body = "\n".join(header) + "\n\n" + body
    path.write_text(body + "\n", encoding="utf-8")


def write_json(path: Path, transcript: Transcript) -> None:
    path.write_text(
        json.dumps(asdict(transcript), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_markdown(path: Path, transcript: Transcript) -> None:
    lines = ["# Transcript", ""]
    lines.append(f"- Source: `{transcript.source}`")
    lines.append(f"- Model: `{transcript.model}`")
    if transcript.language:
        lines.append(f"- Language: `{transcript.language}`")
    if transcript.duration is not None:
        lines.append(f"- Duration: `{format_timestamp(transcript.duration, '.')}`")
    for warning in warning_lines(transcript, "> **Warning:** "):
        lines.extend(["", warning])
    lines.extend(["", "## Conversation", ""])

    current_speaker: str | None = None
    current_start: float | None = None
    current_end: float | None = None
    current_text: list[str] = []

    def flush() -> None:
        if not current_text or current_start is None or current_end is None:
            return
        speaker = current_speaker or "UNKNOWN"
        timestamp = (
            f"{format_timestamp(current_start, '.')}–"
            f"{format_timestamp(current_end, '.')}"
        )
        lines.append(f"**{speaker}** · {timestamp}")
        lines.append("")
        lines.append(" ".join(current_text).strip())
        lines.append("")

    for segment in transcript.segments:
        if current_start is None or segment.speaker != current_speaker:
            flush()
            current_speaker = segment.speaker
            current_start = segment.start
            current_text = [segment.text.strip()]
        else:
            current_text.append(segment.text.strip())
        current_end = segment.end
    flush()
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


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


TRANSCRIPTION_SAMPLE_RATE = 16000
# Gate settings for gated_speech_gain_db, following EBU R128's two stages.
SPEECH_BLOCK_SECONDS = 0.4
SPEECH_ABSOLUTE_GATE_DBFS = -60.0
SPEECH_RELATIVE_GATE_DB = 10.0
SPEECH_GAIN_LIMITS_DB = (-20.0, 30.0)


def decode_mono_speech(source: Path) -> Iterable:
    """Yield the track as mono 16 kHz float32 blocks.

    Resamples exactly as prepare_diarization_audio does, but hands the samples
    back instead of writing them, so a caller can measure a track and then
    rewrite it without holding an hour of audio in memory at once.
    """
    import av

    resampler = av.AudioResampler(
        format="flt",
        layout="mono",
        rate=TRANSCRIPTION_SAMPLE_RATE,
    )
    with av.open(str(source)) as container:
        streams = [s for s in container.streams if s.type == "audio"]
        if not streams:
            raise ValueError(f"No audio stream found in {source}")
        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                yield resampled.to_ndarray().reshape(-1)
        for resampled in resampler.resample(None):
            yield resampled.to_ndarray().reshape(-1)


def gated_speech_gain_db(
    block_mean_squares: Iterable[float],
    target_dbfs: float,
) -> float:
    """Gain in dB that lifts speech - not silence - to target_dbfs.

    An average taken over a whole meeting microphone measures mostly silence:
    79% of one session's mic track sat below -50 dBFS because its owner spent
    the call listening. Normalising against that average would drive the noise
    floor up by tens of dB and leave the speech no louder than before. EBU
    R128 answers this with two gates, reproduced here: discard the near-silent
    blocks, then discard whatever sits far below the level of what survived,
    and measure only the remainder.
    """
    import math

    absolute = 10.0 ** (SPEECH_ABSOLUTE_GATE_DBFS / 10.0)
    audible = [value for value in block_mean_squares if value > absolute]
    if not audible:
        return 0.0
    relative = (sum(audible) / len(audible)) * 10.0 ** (
        -SPEECH_RELATIVE_GATE_DB / 10.0
    )
    speech = [value for value in audible if value > relative]
    if not speech:
        return 0.0
    rms = math.sqrt(sum(speech) / len(speech))
    if rms <= 0.0:
        return 0.0
    minimum, maximum = SPEECH_GAIN_LIMITS_DB
    return max(minimum, min(maximum, target_dbfs - 20.0 * math.log10(rms)))


def measure_speech_blocks(source: Path) -> list[float]:
    import numpy as np

    block = int(TRANSCRIPTION_SAMPLE_RATE * SPEECH_BLOCK_SECONDS)
    squares: list[float] = []
    carry = np.zeros(0, dtype=np.float32)
    for chunk in decode_mono_speech(source):
        carry = np.concatenate((carry, chunk))
        usable = (carry.size // block) * block
        if not usable:
            continue
        blocks = carry[:usable].astype(np.float64).reshape(-1, block)
        squares.extend(np.square(blocks).mean(axis=1).tolist())
        carry = carry[usable:]
    if carry.size:
        squares.append(float(np.square(carry.astype(np.float64)).mean()))
    return squares


def prepare_transcription_audio(
    source: Path,
    target_dbfs: float,
) -> tuple[Path | None, float]:
    """Rewrite the track at a consistent speech level.

    Returns the temporary file and the gain applied, or (None, gain) when the
    track already sits close enough that rewriting it would only cost time.
    """
    import numpy as np

    gain_db = gated_speech_gain_db(measure_speech_blocks(source), target_dbfs)
    if abs(gain_db) < 0.5:
        return None, gain_db

    factor = 10.0 ** (gain_db / 20.0)
    temp_file = tempfile.NamedTemporaryFile(
        prefix=f"{source.stem}-leveled-",
        suffix=".wav",
        delete=False,
    )
    wav_path = Path(temp_file.name)
    temp_file.close()

    try:
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(TRANSCRIPTION_SAMPLE_RATE)
            for chunk in decode_mono_speech(source):
                # Clipping here can only catch the isolated transients the
                # gate deliberately ignored: one mic track peaked at 0 dBFS on
                # twenty samples of click while its speech sat 40 dB below.
                # Holding the gain back for those would defeat the exercise.
                scaled = np.clip(chunk * factor, -1.0, 1.0)
                frames = np.rint(scaled * 32767.0).astype("<i2")
                wav.writeframes(frames.tobytes())
    except Exception:
        wav_path.unlink(missing_ok=True)
        raise

    return wav_path, gain_db


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


# pyannote calls its hook once per pipeline step, in this order. Only
# "embeddings" reports total/completed, and it is also the slowest step, so it
# gets the widest band — that is what makes the bar actually move. The others
# fire once, on completion, so they can only snap to their end value.
DIARIZATION_STEPS: tuple[tuple[str, float, float], ...] = (
    ("segmentation", 0.0, 0.35),
    ("speaker_counting", 0.35, 0.40),
    ("embeddings", 0.40, 0.90),
    ("discrete_diarization", 0.90, 1.0),
)


def diarization_progress_hook(
    callback: Callable[[float], None],
) -> Callable[..., None]:
    bounds = {name: (start, end) for name, start, end in DIARIZATION_STEPS}
    last_reported = -1.0

    def hook(
        step_name: str,
        step_artifact: object,
        file: object = None,
        total: int | None = None,
        completed: int | None = None,
    ) -> None:
        nonlocal last_reported
        span = bounds.get(step_name)
        if span is None:
            return
        start, end = span
        if completed is None or not total:
            fraction = end
        else:
            fraction = start + (end - start) * min(1.0, completed / total)
        # The embeddings step fires per batch; writing job.json every time
        # would be far more I/O than the progress bar can express.
        if fraction < last_reported + 0.01 and fraction < 1.0:
            return
        last_reported = fraction
        callback(fraction)

    return hook


def run_diarization(
    diarizer: object | None,
    source: Path,
    base: Path,
    args: argparse.Namespace,
    progress_callback: Callable[[float], None] | None = None,
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
        if progress_callback is not None:
            kwargs["hook"] = diarization_progress_hook(progress_callback)
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


def transcribe_source(
    model: "WhisperModel",
    diarizer: object | None,
    source: Path,
    args: argparse.Namespace,
    diarization_base: Path | None = None,
    progress_callback: Callable[[float], None] | None = None,
    diarization_callback: Callable[[bool], None] | None = None,
    diarization_progress: Callable[[float], None] | None = None,
) -> Transcript:
    print(f"Transcribing {source.name}...", flush=True)
    if progress_callback is not None:
        progress_callback(0.0)

    leveled: Path | None = None
    if getattr(args, "normalize_audio", False):
        leveled, gain_db = prepare_transcription_audio(
            source,
            getattr(args, "target_speech_dbfs", -20.0),
        )
        if leveled is not None:
            print(f"  Speech level adjusted {gain_db:+.1f} dB", flush=True)

    segments: list[TranscriptSegment] = []
    try:
        # Decoding is lazy, so the leveled file has to outlive the loop below.
        segments_iter, info = model.transcribe(
            str(leveled or source),
            beam_size=args.beam_size,
            language=args.language,
            task=args.task,
            # Detecting per segment is only meaningful when no language was
            # declared. 'auto' should follow a call that switches language
            # halfway; an explicit --language it must stay pinned to Italian
            # even when a few English sentences go by. An .en model has no
            # other language to detect, so asking it to try only misleads.
            multilingual=(
                args.language is None
                and not str(args.model).endswith(".en")
            ),
            condition_on_previous_text=getattr(
                args, "condition_on_previous_text", True
            ),
            vad_filter=not args.no_vad,
            word_timestamps=args.word_timestamps,
            # 0 reads as "disabled" from the CLI; faster-whisper wants None.
            hallucination_silence_threshold=getattr(
                args, "hallucination_silence_threshold", None
            )
            or None,
        )

        duration: float = getattr(info, "duration", None) or 0.0
        for segment in segments_iter:
            pct = f"{segment.end / duration * 100:.0f}%" if duration else "…"
            print(f"  [{pct}] {segment.text.strip()}", flush=True)
            if progress_callback is not None and duration:
                progress_callback(min(1.0, segment.end / duration))
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
                    words=words,
                )
            )
    finally:
        if leveled is not None:
            leveled.unlink(missing_ok=True)

    if progress_callback is not None:
        progress_callback(1.0)

    diarization_turns = None
    if diarizer is not None:
        if diarization_base is None:
            raise ValueError("diarization_base is required with a diarizer")
        if diarization_callback is not None:
            diarization_callback(True)
        try:
            diarization_turns = run_diarization(
                diarizer,
                source,
                diarization_base,
                args,
                progress_callback=diarization_progress,
            )
        finally:
            if diarization_callback is not None:
                diarization_callback(False)
    if diarization_turns:
        for segment in segments:
            segment.speaker = best_speaker_for_segment(segment, diarization_turns)

    return Transcript(
        source=str(source),
        model=args.model,
        language=getattr(info, "language", None),
        language_probability=getattr(info, "language_probability", None),
        duration=getattr(info, "duration", None),
        segments=segments,
        diarization=diarization_turns,
    )


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

    # Both failures below are settled: the package is absent, or the token is
    # not allowed near the model. Retrying decides nothing, so they are raised
    # apart from the rest and let the caller finish without speaker labels.
    # Anything else — a dropped connection, an exhausted disk — still escapes,
    # because there the retry is the whole point.
    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise DiarizationUnavailable(
            "pyannote.audio is not installed; install "
            "requirements-diarization.txt to enable it."
        ) from exc

    token = args.hf_token or os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")

    try:
        pipeline = load_pyannote_pipeline(Pipeline, args.diarization_model, token)
    except Exception as exc:
        if is_huggingface_access_error(exc):
            print_diarization_access_help(args.diarization_model, exc)
            raise DiarizationUnavailable(
                f"the Hugging Face token cannot access {args.diarization_model}; "
                "check HF_TOKEN in .env and accept the model conditions."
            ) from exc
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
