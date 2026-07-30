from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from meeting_recorder.cli import (
    DiarizationTurn,
    Transcript,
    TranscriptSegment,
    Word,
    write_markdown,
)
from meeting_recorder.benchmark import word_error_counts
from meeting_recorder.config import AppConfig, TranscriptionConfig
from meeting_recorder.session_transcription import (
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

    def test_session_pipeline_disables_speech_filters_by_default(self) -> None:
        """VAD and the hallucination heuristic silently drop quiet microphone
        speech, so the session pipeline must leave both off unless asked."""
        args = transcription_args(AppConfig(recordings_dir=self.root))
        self.assertTrue(args.no_vad)
        self.assertIsNone(args.hallucination_silence_threshold)

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


class BenchmarkTests(unittest.TestCase):
    def test_word_error_counts_normalizes_case_and_punctuation(self) -> None:
        errors, words = word_error_counts(
            "Ciao, MONDO!",
            "ciao mondo",
        )
        self.assertEqual((errors, words), (0, 2))

    def test_word_error_counts_handles_substitution(self) -> None:
        errors, words = word_error_counts(
            "uno due tre",
            "uno quattro tre",
        )
        self.assertEqual((errors, words), (1, 3))

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


if __name__ == "__main__":
    unittest.main()
