from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from meeting_recorder.transcription import (
    DiarizationTurn,
    DiarizationUnavailable,
    SPEECH_GAIN_LIMITS_DB,
    Transcript,
    TranscriptSegment,
    Word,
    diarization_progress_hook,
    gated_speech_gain_db,
    transcribe_source,
    write_markdown,
    write_speaker_txt,
    write_srt,
    write_txt,
)
from meeting_recorder.config import (
    AppConfig,
    TranscriptionConfig,
    load_config,
)
from meeting_recorder.session_transcription import (
    SessionProcessor,
    merge_track_transcripts,
    transcription_args,
)
from meeting_recorder.sessions import (
    enqueue_session,
    pending_sessions,
    read_json,
    recover_sessions,
    run_job,
    update_job_progress,
)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class SessionQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session = self.root / "2026-07-29T10-00-00"
        (self.session / "audio").mkdir(parents=True)
        (self.session / "audio" / "system.caf").write_bytes(b"audio")
        write_json(
            self.session / "meta.json",
            {
                "state": "recorded",
                "tracks": {
                    "mic": "audio/mic.caf",
                    "system": "audio/system.caf",
                },
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_enqueue_and_complete_job(self) -> None:
        job = enqueue_session(
            self.session,
            options={"language": "it"},
        )
        self.assertEqual(job["state"], "pending")
        self.assertEqual(job["options"]["language"], "it")
        self.assertEqual(job["progress"]["phase"], "queued")

        def complete(session: Path) -> None:
            update_job_progress(
                session,
                phase="writing_outputs",
                fraction=0.94,
                detail="Writing transcript files",
            )
            transcript = session / "transcripts" / "transcript.json"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("{}", encoding="utf-8")

        result = run_job(self.session, complete)
        self.assertEqual(result.state, "complete")
        self.assertEqual(
            read_json(self.session / "job.json")["state"],
            "complete",
        )
        self.assertEqual(
            read_json(self.session / "job.json")["progress"]["fraction"],
            1.0,
        )
        self.assertEqual(
            read_json(self.session / "meta.json")["state"],
            "transcribed",
        )

    def test_failed_job_is_retryable_then_terminal(self) -> None:
        enqueue_session(self.session, max_attempts=2)

        def fail(_: Path) -> None:
            update_job_progress(
                self.session,
                phase="diarizing_system",
                fraction=0.72,
                detail="Separating speakers",
                indeterminate=True,
            )
            raise RuntimeError("boom")

        first = run_job(self.session, fail)
        retry_job = read_json(self.session / "job.json")
        second = run_job(self.session, fail)
        self.assertEqual(first.state, "pending")
        self.assertEqual(retry_job["progress"]["phase"], "retrying")
        self.assertEqual(retry_job["progress"]["fraction"], 0.72)
        self.assertEqual(second.state, "failed")
        self.assertEqual(second.attempts, 2)

    def test_recover_interrupted_worker(self) -> None:
        enqueue_session(self.session)
        job = read_json(self.session / "job.json")
        job["state"] = "processing"
        write_json(self.session / "job.json", job)

        recovered = recover_sessions(self.root)
        self.assertEqual(recovered, [self.session])
        recovered_job = read_json(self.session / "job.json")
        self.assertEqual(recovered_job["state"], "pending")

    def test_pending_queue_includes_all_sessions(self) -> None:
        enqueue_session(self.session)
        second = self.root / "2026-07-29T10-30-00"
        (second / "audio").mkdir(parents=True)
        (second / "audio" / "mic.caf").write_bytes(b"audio")
        write_json(
            second / "meta.json",
            {
                "state": "recorded",
                "tracks": {
                    "mic": "audio/mic.caf",
                    "system": "audio/system.caf",
                },
            },
        )
        enqueue_session(second)

        self.assertEqual(
            pending_sessions(self.root),
            [self.session, second],
        )

    def test_session_pipeline_defaults_to_speech_filters(self) -> None:
        """Left unfiltered, Whisper fills a listener's silent microphone with
        invented filler - one session produced 88 identical "Okay." segments.

        The filters have to arrive with the leveling, never on their own: the
        VAD judges absolute energy, so on a Bluetooth headset mic recorded
        17 dB down it discarded 82% of the words its owner actually spoke.
        Levelling first is what makes filtering safe, so these three defaults
        belong together.
        """
        args = transcription_args(AppConfig(recordings_dir=self.root))
        self.assertFalse(args.no_vad)
        self.assertEqual(args.hallucination_silence_threshold, 2.0)
        self.assertTrue(args.normalize_audio)

    def test_session_pipeline_honours_configured_speech_filters(self) -> None:
        config = AppConfig(
            recordings_dir=self.root,
            transcription=TranscriptionConfig(
                vad_filter=True,
                hallucination_silence_threshold=2.0,
            ),
        )
        args = transcription_args(config)
        self.assertFalse(args.no_vad)
        self.assertEqual(args.hallucination_silence_threshold, 2.0)


class DiarizationProgressTests(unittest.TestCase):
    """pyannote only reports total/completed for the embeddings step, so that
    is the one that has to produce a moving bar."""

    def run_pipeline_steps(self) -> list[float]:
        seen: list[float] = []
        hook = diarization_progress_hook(seen.append)
        hook("segmentation", object())
        hook("speaker_counting", object())
        hook("embeddings", None, total=4, completed=0)
        for index in range(1, 5):
            hook("embeddings", object(), total=4, completed=index)
        hook("embeddings", object())
        hook("discrete_diarization", object())
        return seen

    def test_progress_is_monotonic_and_complete(self) -> None:
        seen = self.run_pipeline_steps()
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 1.0)

    def test_embeddings_step_reports_intermediate_progress(self) -> None:
        seen = self.run_pipeline_steps()
        between = [value for value in seen if 0.40 < value < 0.90]
        self.assertTrue(between, "embeddings must move the bar, not just jump")

    def test_unknown_step_is_ignored(self) -> None:
        seen: list[float] = []
        hook = diarization_progress_hook(seen.append)
        hook("a_step_pyannote_added_later", object())
        self.assertEqual(seen, [])


class TrackMergeTests(unittest.TestCase):
    def test_track_offsets_labels_and_word_times_are_merged(self) -> None:
        mic = Transcript(
            source="mic.caf",
            model="medium",
            language="it",
            language_probability=0.9,
            duration=3,
            segments=[
                TranscriptSegment(
                    start=0,
                    end=1,
                    text="Ciao",
                    words=[Word(0, 0.5, "Ciao")],
                )
            ],
        )
        system = Transcript(
            source="system.caf",
            model="medium",
            language="it",
            language_probability=0.8,
            duration=3,
            segments=[
                TranscriptSegment(
                    start=0,
                    end=1,
                    text="Buongiorno",
                    speaker="SPEAKER_00",
                ),
                TranscriptSegment(
                    start=1,
                    end=2,
                    text="Salve",
                    speaker="SPEAKER_01",
                ),
            ],
            diarization=[
                DiarizationTurn(0, 1, "SPEAKER_00"),
                DiarizationTurn(1, 2, "SPEAKER_01"),
            ],
        )
        merged = merge_track_transcripts(
            session_dir=Path("/tmp/session"),
            meta={
                "durationSeconds": 4,
                "trackStartOffsets": {"mic": 0.25, "system": 0.5},
            },
            mic=mic,
            system=system,
            model="medium",
        )

        self.assertEqual(
            [segment.speaker for segment in merged.segments],
            ["ME", "REMOTE_01", "REMOTE_02"],
        )
        self.assertEqual(merged.segments[0].track, "mic")
        self.assertEqual(merged.segments[0].words[0].start, 0.25)
        self.assertEqual(merged.diarization[0].start, 0.5)
        self.assertEqual(merged.duration, 4)


class MarkdownOutputTests(unittest.TestCase):
    def test_markdown_keeps_unlabelled_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "transcript.md"
            transcript = Transcript(
                source="audio.m4a",
                model="medium",
                language="it",
                language_probability=1,
                duration=1,
                segments=[
                    TranscriptSegment(0, 1, "Testo senza speaker")
                ],
            )
            write_markdown(output, transcript)
            self.assertIn(
                "Testo senza speaker",
                output.read_text(encoding="utf-8"),
            )


class DegradedDiarizationTests(unittest.TestCase):
    """A recording is worth more than the speaker labels put on it.

    pyannote being absent, or a token being refused, is settled: three
    attempts decide it no differently than one, and failing the job discards
    an hour of speech that transcribed perfectly well. The transcript is
    written without speaker separation instead - and says so where someone
    will actually read it, which is the transcript and not the log.
    """

    def processor(self) -> SessionProcessor:
        return SessionProcessor(
            AppConfig(recordings_dir=Path("/tmp")),
            {"diarize_system": True},
        )

    def test_missing_pyannote_degrades_with_a_warning(self) -> None:
        processor = self.processor()
        with mock.patch(
            "meeting_recorder.session_transcription.load_diarizer",
            side_effect=DiarizationUnavailable(
                "pyannote.audio is not installed; install "
                "requirements-diarization.txt to enable it."
            ),
        ):
            self.assertIsNone(processor.diarizer())

        self.assertEqual(len(processor.warnings), 1)
        self.assertIn("pyannote.audio is not installed", processor.warnings[0])
        self.assertIn("labelled REMOTE", processor.warnings[0])

    def test_an_unexpected_failure_still_fails_the_job(self) -> None:
        # A dropped connection is the case retrying exists for. Degrading here
        # would quietly hand back a worse transcript than a retry would.
        processor = self.processor()
        with mock.patch(
            "meeting_recorder.session_transcription.load_diarizer",
            side_effect=OSError("connection reset"),
        ):
            with self.assertRaises(OSError):
                processor.diarizer()

        self.assertEqual(processor.warnings, [])

    def test_the_warning_reaches_every_readable_output(self) -> None:
        warning = "Speaker separation was skipped. Everyone is REMOTE."
        transcript = Transcript(
            source="session",
            model="medium",
            language="it",
            language_probability=1,
            duration=1,
            segments=[TranscriptSegment(0, 1, "Buongiorno", speaker="ME")],
            warnings=[warning],
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "transcript"
            write_markdown(base.with_suffix(".md"), transcript)
            write_txt(base.with_suffix(".txt"), transcript)
            write_speaker_txt(base.with_suffix(".speakers.txt"), transcript)
            write_srt(base.with_suffix(".srt"), transcript)

            for suffix in (".md", ".txt", ".speakers.txt"):
                written = base.with_suffix(suffix).read_text(encoding="utf-8")
                self.assertIn(warning, written)
                self.assertIn("Buongiorno", written)

            # Not the subtitles: there the warning would play over the video as
            # the first thing anyone hears said.
            self.assertNotIn(
                warning,
                base.with_suffix(".srt").read_text(encoding="utf-8"),
            )

    def test_a_clean_run_adds_nothing(self) -> None:
        transcript = Transcript(
            source="session",
            model="medium",
            language="it",
            language_probability=1,
            duration=1,
            segments=[TranscriptSegment(0, 1, "Buongiorno", speaker="ME")],
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "transcript.txt"
            write_txt(output, transcript)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "Buongiorno\n",
            )


def mean_square(rms: float) -> float:
    return rms * rms


class SpeechGainTests(unittest.TestCase):
    def test_silence_is_excluded_from_the_measurement(self) -> None:
        # A mic that recorded four minutes of silence around ten seconds of
        # speech must be leveled by what the speech needs, not by an average
        # the silence dominates.
        speech = [mean_square(0.01)] * 25
        silence = [mean_square(0.000001)] * 575

        self.assertAlmostEqual(
            gated_speech_gain_db(speech + silence, -20.0),
            gated_speech_gain_db(speech, -20.0),
            places=6,
        )

    def test_gain_lifts_quiet_speech_to_the_target(self) -> None:
        # -40 dBFS speech needs +20 dB to reach a -20 dBFS target.
        self.assertAlmostEqual(
            gated_speech_gain_db([mean_square(0.01)] * 10, -20.0),
            20.0,
            places=6,
        )

    def test_loud_speech_is_attenuated(self) -> None:
        self.assertLess(
            gated_speech_gain_db([mean_square(0.5)] * 10, -20.0),
            0.0,
        )

    def test_gain_is_clamped(self) -> None:
        minimum, maximum = SPEECH_GAIN_LIMITS_DB
        # Audible enough to clear the gate, quiet enough to ask for +34 dB.
        self.assertEqual(
            gated_speech_gain_db([mean_square(0.002)] * 10, -20.0),
            maximum,
        )
        self.assertEqual(
            gated_speech_gain_db([mean_square(1.0)] * 10, -90.0),
            minimum,
        )

    def test_a_wholly_silent_track_is_left_alone(self) -> None:
        self.assertEqual(gated_speech_gain_db([1e-12] * 100, -20.0), 0.0)
        self.assertEqual(gated_speech_gain_db([], -20.0), 0.0)


class RecordingModel:
    """Stands in for WhisperModel, capturing the decode options."""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    def transcribe(self, source: str, **kwargs):
        self.kwargs = kwargs
        info = SimpleNamespace(
            duration=1.0,
            language="en",
            language_probability=1.0,
        )
        return iter(()), info


def decode_options(**overrides) -> dict:
    args = argparse.Namespace(
        model="medium",
        beam_size=5,
        language=None,
        task="transcribe",
        word_timestamps=False,
        no_vad=False,
        hallucination_silence_threshold=2.0,
        condition_on_previous_text=True,
        # The leveling pass is exercised separately; it needs real audio.
        normalize_audio=False,
        target_speech_dbfs=-20.0,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    model = RecordingModel()
    transcribe_source(model, None, Path("meeting.wav"), args)
    return model.kwargs


class DecodeOptionTests(unittest.TestCase):
    def test_auto_detects_language_per_segment(self) -> None:
        # 'auto' reaches transcribe_source as None, and only then should
        # Whisper be free to switch language mid-call.
        self.assertTrue(decode_options(language=None)["multilingual"])

    def test_a_declared_language_stays_pinned(self) -> None:
        for declared in ("en", "it", "es"):
            with self.subTest(language=declared):
                options = decode_options(language=declared)
                self.assertFalse(options["multilingual"])
                self.assertEqual(options["language"], declared)

    def test_english_only_models_never_detect(self) -> None:
        # meet.sh no longer picks an .en model, but one can still be asked for
        # by hand, and it has no second language to find.
        options = decode_options(language=None, model="medium.en")
        self.assertFalse(options["multilingual"])

    def test_zero_threshold_disables_the_heuristic(self) -> None:
        # argparse cannot express None, so 0 is the off switch; passing it
        # straight through would instead mean "every silence is suspect".
        options = decode_options(hallucination_silence_threshold=0.0)
        self.assertIsNone(options["hallucination_silence_threshold"])

    def test_defaults_guard_against_hallucination(self) -> None:
        options = decode_options()
        self.assertTrue(options["vad_filter"])
        self.assertEqual(options["hallucination_silence_threshold"], 2.0)
        self.assertTrue(options["condition_on_previous_text"])


class TranscriptionSettingsTests(unittest.TestCase):
    def test_config_file_overrides_are_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            write_json(
                path,
                {
                    "transcription": {
                        "condition_on_previous_text": False,
                        "normalize_audio": False,
                        "target_speech_dbfs": -16.5,
                    }
                },
            )
            settings = load_config(path).transcription
            self.assertFalse(settings.condition_on_previous_text)
            self.assertFalse(settings.normalize_audio)
            self.assertEqual(settings.target_speech_dbfs, -16.5)
            # Untouched keys keep the hardened defaults.
            self.assertTrue(settings.vad_filter)
            self.assertEqual(settings.hallucination_silence_threshold, 2.0)


if __name__ == "__main__":
    unittest.main()
