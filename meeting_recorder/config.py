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
    # A meeting microphone is silent most of the time — the person wearing it
    # spends the call listening. Fed that silence, Whisper decodes its own
    # priors instead of nothing and emits filler: one session produced 88
    # identical "Okay." segments over stretches measuring -80 dBFS. The VAD
    # keeps the decoder away from those stretches, so it stays on.
    vad_filter: bool = True
    # Seconds of silence after which faster-whisper treats a segment as a
    # hallucination and removes it. Backstop for whatever the VAD lets pass.
    hallucination_silence_threshold: float | None = 2.0
    # Each window is decoded conditioned on the previous one, which keeps the
    # text coherent across a conversation but also lets a single hallucination
    # seed the next window and lock the decoder into a repetition loop. Turn
    # off to break a recording that repeats one phrase.
    condition_on_previous_text: bool = True
    # Bring the speech in every track to a common loudness before decoding. A
    # Bluetooth headset mic records far quieter than the system track — 17 dB
    # apart in one session — and the gap costs accuracy on the quiet side.
    normalize_audio: bool = True
    # Target level for speech, measured over speech alone. See
    # gated_speech_gain_db for why the silence has to be excluded.
    target_speech_dbfs: float = -20.0


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
        condition_on_previous_text=bool(
            raw.get(
                "condition_on_previous_text",
                defaults.condition_on_previous_text,
            )
        ),
        normalize_audio=bool(
            raw.get("normalize_audio", defaults.normalize_audio)
        ),
        target_speech_dbfs=float(
            raw.get("target_speech_dbfs", defaults.target_speech_dbfs)
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
