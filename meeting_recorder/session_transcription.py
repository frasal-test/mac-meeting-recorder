from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
from typing import Any

from .cli import (
    DiarizationTurn,
    Transcript,
    TranscriptSegment,
    Word,
    load_diarizer,
    load_model,
    transcribe_source,
    write_json,
    write_markdown,
    write_rttm,
    write_speaker_txt,
    write_srt,
    write_txt,
)
from .config import AppConfig
from .sessions import (
    append_log,
    read_json,
    run_on_stop,
    track_paths,
)


def transcription_args(config: AppConfig) -> argparse.Namespace:
    settings = config.transcription
    return argparse.Namespace(
        model=settings.model,
        device="auto",
        compute_type=settings.compute_type,
        language=settings.language,
        task="transcribe",
        beam_size=5,
        cpu_threads=0,
        word_timestamps=settings.word_timestamps,
        no_vad=True,
        diarize=settings.diarize_system,
        diarization_model="pyannote/speaker-diarization-3.1",
        hf_token=None,
        num_speakers=None,
        min_speakers=None,
        max_speakers=None,
        diarization_device="cpu",
    )


def shifted_word(word: Word, offset: float) -> Word:
    return replace(word, start=word.start + offset, end=word.end + offset)


def shifted_segment(
    segment: TranscriptSegment,
    *,
    offset: float,
    track: str,
    speaker: str,
) -> TranscriptSegment:
    words = None
    if segment.words:
        words = [shifted_word(word, offset) for word in segment.words]
    return replace(
        segment,
        start=segment.start + offset,
        end=segment.end + offset,
        track=track,
        speaker=speaker,
        words=words,
    )


def remote_speaker_map(
    transcript: Transcript,
) -> dict[str | None, str]:
    labels = sorted(
        {
            segment.speaker
            for segment in transcript.segments
            if segment.speaker is not None
        }
    )
    if not labels:
        return {None: "REMOTE"}
    if len(labels) == 1:
        return {labels[0]: "REMOTE"}
    return {
        label: f"REMOTE_{index:02d}"
        for index, label in enumerate(labels, start=1)
    }


def merge_track_transcripts(
    *,
    session_dir: Path,
    meta: dict[str, Any],
    mic: Transcript | None,
    system: Transcript | None,
    model: str,
) -> Transcript:
    offsets = meta.get("trackStartOffsets")
    if not isinstance(offsets, dict):
        offsets = {}
    mic_offset = float(offsets.get("mic") or 0)
    system_offset = float(offsets.get("system") or 0)

    segments: list[TranscriptSegment] = []
    turns: list[DiarizationTurn] = []
    languages: list[str] = []
    probabilities: list[float] = []

    if mic is not None:
        if mic.language:
            languages.append(mic.language)
        if mic.language_probability is not None:
            probabilities.append(mic.language_probability)
        segments.extend(
            shifted_segment(
                segment,
                offset=mic_offset,
                track="mic",
                speaker="ME",
            )
            for segment in mic.segments
        )

    if system is not None:
        if system.language:
            languages.append(system.language)
        if system.language_probability is not None:
            probabilities.append(system.language_probability)
        labels = remote_speaker_map(system)
        for segment in system.segments:
            segments.append(
                shifted_segment(
                    segment,
                    offset=system_offset,
                    track="system",
                    speaker=labels.get(segment.speaker, "REMOTE"),
                )
            )
        for turn in system.diarization or []:
            turns.append(
                DiarizationTurn(
                    start=turn.start + system_offset,
                    end=turn.end + system_offset,
                    speaker=labels.get(turn.speaker, "REMOTE"),
                )
            )

    segments.sort(key=lambda item: (item.start, item.end, item.track or ""))
    unique_languages = list(dict.fromkeys(languages))
    language = ",".join(unique_languages) if unique_languages else None
    probability = (
        sum(probabilities) / len(probabilities) if probabilities else None
    )
    inferred_duration = max((segment.end for segment in segments), default=0.0)
    duration = float(meta.get("durationSeconds") or inferred_duration)
    return Transcript(
        source=str(session_dir),
        model=model,
        language=language,
        language_probability=probability,
        duration=max(duration, inferred_duration),
        segments=segments,
        diarization=turns or None,
    )


class SessionProcessor:
    def __init__(
        self,
        config: AppConfig,
        options: dict[str, Any] | None = None,
    ):
        self.config = config
        self.args = transcription_args(config)
        options = options or {}
        if options.get("model"):
            self.args.model = str(options["model"])
        if "language" in options:
            self.args.language = options["language"] or None
        if "diarize_system" in options:
            self.args.diarize = bool(options["diarize_system"])
        if options.get("num_speakers") is not None:
            self.args.num_speakers = int(options["num_speakers"])
        self._model: object | None = None
        self._diarizer: object | None = None
        self._diarizer_loaded = False

    @property
    def model(self) -> object:
        if self._model is None:
            append_message = (
                f"Loading transcription model {self.args.model}"
            )
            print(append_message, flush=True)
            self._model = load_model(self.args)
        return self._model

    def diarizer(self) -> object | None:
        if not self._diarizer_loaded:
            self._diarizer = load_diarizer(self.args)
            self._diarizer_loaded = True
        return self._diarizer

    def __call__(self, session_dir: Path) -> None:
        if not self.config.transcription.enabled:
            append_log(
                session_dir,
                "Transcription disabled by configuration",
            )
            if self.config.on_stop:
                try:
                    run_on_stop(self.config.on_stop, session_dir)
                    append_log(session_dir, "on_stop hook complete")
                except Exception as exc:
                    append_log(
                        session_dir,
                        f"on_stop hook failed: {exc}",
                    )
            return

        meta = read_json(session_dir / "meta.json")
        paths = track_paths(session_dir, meta)
        output_dir = session_dir / "transcripts"
        output_dir.mkdir(parents=True, exist_ok=True)

        mic = None
        if paths["mic"].is_file() and paths["mic"].stat().st_size:
            append_log(session_dir, "Transcribing microphone track")
            mic = transcribe_source(
                self.model,
                None,
                paths["mic"],
                self.args,
            )

        system = None
        if paths["system"].is_file() and paths["system"].stat().st_size:
            append_log(session_dir, "Transcribing system track")
            system = transcribe_source(
                self.model,
                self.diarizer(),
                paths["system"],
                self.args,
                diarization_base=output_dir / "system",
            )

        if mic is None and system is None:
            raise ValueError("The session has no readable audio tracks")

        transcript = merge_track_transcripts(
            session_dir=session_dir,
            meta=meta,
            mic=mic,
            system=system,
            model=self.args.model,
        )
        base = output_dir / "transcript"
        write_txt(base.with_suffix(".txt"), transcript)
        write_srt(base.with_suffix(".srt"), transcript)
        write_json(base.with_suffix(".json"), transcript)
        write_markdown(base.with_suffix(".md"), transcript)
        write_speaker_txt(
            base.with_suffix(".speakers.txt"),
            transcript,
        )
        if transcript.diarization is not None:
            write_rttm(
                base.with_suffix(".rttm"),
                session_dir.name,
                transcript.diarization,
            )

        if self.config.on_stop:
            try:
                run_on_stop(self.config.on_stop, session_dir)
                append_log(session_dir, "on_stop hook complete")
            except Exception as exc:
                append_log(
                    session_dir,
                    f"on_stop hook failed without invalidating transcript: {exc}",
                )
