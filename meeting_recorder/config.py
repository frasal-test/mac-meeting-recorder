from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CONFIG_PATH = Path("~/.config/taprecord/config.json").expanduser()


@dataclass
class TranscriptionConfig:
    enabled: bool = True
    model: str = "medium"
    language: str | None = "it"
    compute_type: str = "int8"
    diarize_system: bool = True
    word_timestamps: bool = True
    max_attempts: int = 3
    # faster-whisper's VAD discards segments it judges to be non-speech. On a
    # quiet or intermittent microphone track it silently drops real speech, so
    # it stays off unless a recording is noisy enough to need it.
    vad_filter: bool = False
    # Seconds of silence after which faster-whisper treats a segment as a
    # hallucination and removes it. None disables the heuristic, which is the
    # safe default for pause-heavy meeting audio.
    hallucination_silence_threshold: float | None = None


@dataclass
class AppConfig:
    recordings_dir: Path
    on_stop: str | None = None
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)


def default_recordings_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "recordings"


def _merge_transcription(raw: dict[str, Any]) -> TranscriptionConfig:
    defaults = TranscriptionConfig()
    return TranscriptionConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        model=str(raw.get("model", defaults.model)),
        language=raw.get("language", defaults.language),
        compute_type=str(raw.get("compute_type", defaults.compute_type)),
        diarize_system=bool(
            raw.get("diarize_system", defaults.diarize_system)
        ),
        word_timestamps=bool(
            raw.get("word_timestamps", defaults.word_timestamps)
        ),
        max_attempts=max(
            1,
            int(raw.get("max_attempts", defaults.max_attempts)),
        ),
        vad_filter=bool(raw.get("vad_filter", defaults.vad_filter)),
        hallucination_silence_threshold=_optional_float(
            raw.get(
                "hallucination_silence_threshold",
                defaults.hallucination_silence_threshold,
            )
        ),
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def load_config(path: Path | None = None) -> AppConfig:
    config_path = (path or CONFIG_PATH).expanduser()
    raw: dict[str, Any] = {}
    if config_path.exists():
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Config root must be an object: {config_path}")
        raw = loaded

    recordings = Path(
        str(raw.get("recordings_dir", default_recordings_dir()))
    ).expanduser()
    transcription_raw = raw.get("transcription", {})
    if not isinstance(transcription_raw, dict):
        raise ValueError("transcription config must be an object")
    on_stop = raw.get("on_stop")
    return AppConfig(
        recordings_dir=recordings,
        on_stop=str(on_stop) if on_stop else None,
        transcription=_merge_transcription(transcription_raw),
    )
