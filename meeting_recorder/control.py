from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from .cli import load_env_files
from .config import CONFIG_PATH, load_config
from .session_transcription import SessionProcessor
from .sessions import (
    enqueue_session,
    pending_sessions,
    read_json,
    recover_sessions,
    run_job,
)


PROJECT_DIR = Path(__file__).resolve().parent.parent
RECORDER_BINARY = PROJECT_DIR / ".recorder"
MENUBAR_BINARY = (
    PROJECT_DIR / "MeetRec.app" / "Contents" / "MacOS" / "MeetRec"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="taprecord",
        description="Manage recording sessions and transcription jobs.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="Configuration file.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("session", type=Path)
    enqueue.add_argument("--force", action="store_true")
    add_transcription_options(enqueue)

    process = subparsers.add_parser("process")
    process.add_argument("session", type=Path)
    process.add_argument("--force", action="store_true")
    add_transcription_options(process)

    worker = subparsers.add_parser("worker")
    worker.add_argument("--session", type=Path)
    worker.add_argument(
        "--once",
        action="store_true",
        help="Process the current queue once and exit.",
    )

    subparsers.add_parser("recover")
    subparsers.add_parser("doctor")
    subparsers.add_parser("show-config")
    return parser.parse_args()


def add_transcription_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model")
    parser.add_argument(
        "--language",
        help="Language code, or 'auto' for automatic detection.",
    )
    diarization = parser.add_mutually_exclusive_group()
    diarization.add_argument(
        "--diarize-system",
        action="store_true",
        dest="diarize_system",
    )
    diarization.add_argument(
        "--no-diarize-system",
        action="store_false",
        dest="diarize_system",
    )
    parser.set_defaults(diarize_system=None)
    parser.add_argument("--num-speakers", type=int)


def requested_options(args: argparse.Namespace) -> dict[str, object]:
    options: dict[str, object] = {}
    if args.model:
        options["model"] = args.model
    if args.language:
        options["language"] = (
            None if args.language == "auto" else args.language
        )
    if args.diarize_system is not None:
        options["diarize_system"] = args.diarize_system
    if args.num_speakers is not None:
        options["num_speakers"] = args.num_speakers
    return options


def doctor(config_path: Path) -> int:
    config = load_config(config_path)
    checks: list[tuple[str, bool, str]] = []
    version = platform.mac_ver()[0]
    major = int(version.split(".", 1)[0]) if version else 0
    checks.append(("macOS 13+", major >= 13, version or "unknown"))
    checks.append(
        ("Swift compiler", shutil.which("swiftc") is not None, shutil.which("swiftc") or "missing")
    )
    checks.append(
        ("Recorder binary", RECORDER_BINARY.is_file(), str(RECORDER_BINARY))
    )
    checks.append(
        ("Menu bar binary", MENUBAR_BINARY.is_file(), str(MENUBAR_BINARY))
    )
    checks.append(
        (
            "faster-whisper",
            importlib.util.find_spec("faster_whisper") is not None,
            "Python module",
        )
    )
    if config.transcription.diarize_system:
        checks.append(
            (
                "pyannote.audio",
                importlib.util.find_spec("pyannote.audio") is not None,
                "required by diarize_system=true",
            )
        )
        checks.append(
            (
                "Hugging Face token",
                bool(
                    os.getenv("HF_TOKEN")
                    or os.getenv("HUGGINGFACE_TOKEN")
                ),
                "HF_TOKEN or HUGGINGFACE_TOKEN",
            )
        )

    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    checks.append(
        (
            "Recordings directory",
            os.access(config.recordings_dir, os.W_OK),
            str(config.recordings_dir),
        )
    )

    for name, passed, detail in checks:
        marker = "✓" if passed else "✗"
        print(f"{marker} {name}: {detail}")

    # Deliberately reported separately and excluded from the exit code: macOS
    # attributes TCC grants to the responsible process, so probing the binary
    # from here describes this terminal, not the menu-bar app. A failure would
    # be a false negative.
    if MENUBAR_BINARY.is_file():
        completed = subprocess.run(
            [str(MENUBAR_BINARY), "--doctor"],
            check=False,
            capture_output=True,
            text=True,
        )
        detail = completed.stdout.strip() or completed.stderr.strip()
        print(f"\nPermissions as seen by this terminal: {detail}")
        print(
            "This reflects the calling process, not MeetRec.app. Check the "
            "real grants in System Settings → Privacy & Security → "
            "Microphone and Screen & System Audio Recording."
        )

    return 0 if all(item[1] for item in checks) else 1


def process_queue(
    *,
    config_path: Path,
    requested: Path | None,
) -> int:
    config = load_config(config_path)
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    with worker_lock(config.recordings_dir):
        recover_sessions(
            config.recordings_dir,
            max_attempts=config.transcription.max_attempts,
        )
        sessions = pending_sessions(config.recordings_dir, requested)
        if not sessions:
            print("No pending sessions.")
            return 0

        exit_code = 0
        for session_dir in sessions:
            job = read_json(session_dir / "job.json")
            options = job.get("options")
            if not isinstance(options, dict):
                options = {}
            processor = SessionProcessor(config, options)
            result = run_job(session_dir, processor)
            print(
                f"{session_dir.name}: {result.state} "
                f"(attempt {result.attempts})"
            )
            if result.state != "complete":
                exit_code = 2
        return exit_code


@contextmanager
def worker_lock(recordings_dir: Path):
    lock_path = recordings_dir / ".worker.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main() -> int:
    load_env_files()
    args = parse_args()
    config = load_config(args.config)

    if args.command == "show-config":
        print(
            json.dumps(
                {
                    "recordings_dir": str(config.recordings_dir),
                    "on_stop": config.on_stop,
                    "transcription": vars(config.transcription),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "doctor":
        return doctor(args.config)
    if args.command == "recover":
        recovered = recover_sessions(
            config.recordings_dir,
            max_attempts=config.transcription.max_attempts,
        )
        for session in recovered:
            print(f"Recovered: {session}")
        print(f"Recovered sessions: {len(recovered)}")
        return 0
    if args.command in {"enqueue", "process"}:
        enqueue_session(
            args.session,
            max_attempts=config.transcription.max_attempts,
            force=args.force,
            options=requested_options(args),
        )
        print(f"Queued: {args.session}")
        if args.command == "enqueue":
            return 0
        return process_queue(
            config_path=args.config,
            requested=args.session,
        )
    if args.command == "worker":
        return process_queue(
            config_path=args.config,
            requested=args.session,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
