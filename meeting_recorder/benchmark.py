from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Any


def normalized_words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = re.sub(r"[^\w']+", " ", normalized, flags=re.UNICODE)
    return normalized.split()


def word_error_counts(
    reference: str,
    hypothesis: str,
) -> tuple[int, int]:
    expected = normalized_words(reference)
    actual = normalized_words(hypothesis)
    previous = list(range(len(actual) + 1))
    for row, expected_word in enumerate(expected, start=1):
        current = [row]
        for column, actual_word in enumerate(actual, start=1):
            substitution = previous[column - 1] + (
                expected_word != actual_word
            )
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    substitution,
                )
            )
        previous = current
    return previous[-1], len(expected)


def audio_duration(path: Path) -> float:
    import av

    with av.open(str(path)) as container:
        if container.duration is not None:
            return float(container.duration / av.time_base)
        streams = [
            stream for stream in container.streams if stream.type == "audio"
        ]
        if not streams:
            raise ValueError(f"No audio stream in {path}")
        stream = streams[0]
        if stream.duration is None or stream.time_base is None:
            raise ValueError(f"Unknown audio duration: {path}")
        return float(stream.duration * stream.time_base)


def make_result(
    *,
    engine: str,
    text: str,
    reference: str | None,
    elapsed_seconds: float | None,
    duration_seconds: float | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "engine": engine,
        "text": text.strip(),
        "elapsed_seconds": elapsed_seconds,
        "audio_duration_seconds": duration_seconds,
        "realtime_factor": (
            elapsed_seconds / duration_seconds
            if elapsed_seconds is not None
            and duration_seconds
            and duration_seconds > 0
            else None
        ),
    }
    if reference is not None:
        errors, words = word_error_counts(reference, text)
        result.update(
            {
                "word_errors": errors,
                "reference_words": words,
                "wer": errors / words if words else 0.0,
            }
        )
    return result


def run_whisper(args: argparse.Namespace) -> dict[str, Any]:
    from faster_whisper import WhisperModel

    reference = (
        args.reference.read_text(encoding="utf-8")
        if args.reference
        else None
    )
    duration = audio_duration(args.audio)
    model = WhisperModel(
        args.model,
        device="auto",
        compute_type=args.compute_type,
    )
    started = time.perf_counter()
    segments, _ = model.transcribe(
        str(args.audio),
        language=None if args.language == "auto" else args.language,
        beam_size=5,
        vad_filter=False,
    )
    text = " ".join(segment.text.strip() for segment in segments)
    elapsed = time.perf_counter() - started
    return make_result(
        engine=f"faster-whisper/{args.model}",
        text=text,
        reference=reference,
        elapsed_seconds=elapsed,
        duration_seconds=duration,
    )


def extract_fluidaudio_text(output: str) -> str:
    matches = re.findall(
        r"(?:Transcription|Transcript):\s*(.+)",
        output,
        flags=re.IGNORECASE,
    )
    if not matches:
        raise ValueError(
            "FluidAudio CLI output did not contain a Transcription line"
        )
    return matches[-1].strip()


def run_fluidaudio(args: argparse.Namespace) -> dict[str, Any]:
    reference = (
        args.reference.read_text(encoding="utf-8")
        if args.reference
        else None
    )
    duration = audio_duration(args.audio)
    command = [
        "swift",
        "run",
        "--package-path",
        str(args.fluidaudio_dir),
        "fluidaudiocli",
        "transcribe",
        str(args.audio),
        "--model-version",
        args.model_version,
    ]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - started
    raw_output = completed.stdout + "\n" + completed.stderr
    if completed.returncode != 0:
        raise RuntimeError(
            f"FluidAudio CLI exited with {completed.returncode}:\n{raw_output}"
        )
    text = extract_fluidaudio_text(raw_output)
    return make_result(
        engine=f"FluidAudio/Parakeet-{args.model_version}",
        text=text,
        reference=reference,
        elapsed_seconds=elapsed,
        duration_seconds=duration,
    )


def evaluate_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    reference = args.reference.read_text(encoding="utf-8")
    duration = args.audio_duration
    timings: dict[str, float] = {}
    for value in args.elapsed:
        name, raw_seconds = value.split("=", 1)
        timings[name] = float(raw_seconds)

    results = []
    for value in args.candidate:
        name, raw_path = value.split("=", 1)
        text = Path(raw_path).read_text(encoding="utf-8")
        results.append(
            make_result(
                engine=name,
                text=text,
                reference=reference,
                elapsed_seconds=timings.get(name),
                duration_seconds=duration,
            )
        )
    return results


def write_report(path: Path, results: list[dict[str, Any]]) -> None:
    lines = [
        "# ASR benchmark",
        "",
        "| Engine | WER | Elapsed | Audio | RTF |",
        "|---|---:|---:|---:|---:|",
    ]
    for result in results:
        wer = result.get("wer")
        elapsed = result.get("elapsed_seconds")
        duration = result.get("audio_duration_seconds")
        rtf = result.get("realtime_factor")
        lines.append(
            "| {engine} | {wer} | {elapsed} | {duration} | {rtf} |".format(
                engine=result["engine"],
                wer=f"{wer:.2%}" if wer is not None else "n/a",
                elapsed=f"{elapsed:.2f}s" if elapsed is not None else "n/a",
                duration=f"{duration:.2f}s" if duration is not None else "n/a",
                rtf=f"{rtf:.4f}" if rtf is not None else "n/a",
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="taprecord-benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)

    whisper = subparsers.add_parser("whisper")
    whisper.add_argument("audio", type=Path)
    whisper.add_argument("--reference", type=Path)
    whisper.add_argument("--model", default="medium")
    whisper.add_argument("--language", default="it")
    whisper.add_argument("--compute-type", default="int8")
    whisper.add_argument("--output", type=Path, required=True)

    fluid = subparsers.add_parser("fluidaudio")
    fluid.add_argument("audio", type=Path)
    fluid.add_argument("--reference", type=Path)
    fluid.add_argument("--fluidaudio-dir", type=Path, required=True)
    fluid.add_argument(
        "--model-version",
        choices=("v2", "v3"),
        default="v3",
    )
    fluid.add_argument("--output", type=Path, required=True)

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--reference", type=Path, required=True)
    evaluate.add_argument(
        "--candidate",
        action="append",
        required=True,
        help="NAME=transcript.txt",
    )
    evaluate.add_argument(
        "--elapsed",
        action="append",
        default=[],
        help="NAME=seconds",
    )
    evaluate.add_argument("--audio-duration", type=float)
    evaluate.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "whisper":
        results = [run_whisper(args)]
    elif args.command == "fluidaudio":
        results = [run_fluidaudio(args)]
    else:
        results = evaluate_candidates(args)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.output.with_suffix(".json")
    json_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_report(args.output, results)
    print(f"Wrote {args.output} and {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
