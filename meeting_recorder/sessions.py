from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


META_FILE = "meta.json"
JOB_FILE = "job.json"
LOG_FILE = "transcribe.log"
TRANSCRIPT_FILE = "transcripts/transcript.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return loaded


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def append_log(session_dir: Path, message: str) -> None:
    timestamp = utc_now()
    with (session_dir / LOG_FILE).open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message.rstrip()}\n")


def track_paths(session_dir: Path, meta: dict[str, Any]) -> dict[str, Path]:
    configured = meta.get("tracks")
    if not isinstance(configured, dict):
        configured = {}
    return {
        "mic": session_dir
        / str(configured.get("mic", "audio/mic.caf")),
        "system": session_dir
        / str(configured.get("system", "audio/system.caf")),
    }


def has_recorded_audio(session_dir: Path, meta: dict[str, Any]) -> bool:
    return any(
        path.is_file() and path.stat().st_size > 0
        for path in track_paths(session_dir, meta).values()
    )


def discover_sessions(recordings_dir: Path) -> Iterable[Path]:
    if not recordings_dir.exists():
        return []
    return sorted(
        path.parent
        for path in recordings_dir.glob(f"*/{META_FILE}")
        if path.is_file()
    )


def default_job(max_attempts: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "state": "pending",
        "progress": {
            "phase": "queued",
            "fraction": 0.0,
            "detail": "Waiting for the transcription worker",
            "indeterminate": False,
        },
        "attempts": 0,
        "max_attempts": max_attempts,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "last_error": None,
    }


def update_job_progress(
    session_dir: Path,
    *,
    phase: str,
    fraction: float,
    detail: str,
    indeterminate: bool = False,
) -> None:
    job_path = session_dir / JOB_FILE
    job = read_json(job_path)
    job["progress"] = {
        "phase": phase,
        "fraction": min(1.0, max(0.0, float(fraction))),
        "detail": detail,
        "indeterminate": indeterminate,
    }
    job["updated_at"] = utc_now()
    atomic_write_json(job_path, job)


def enqueue_session(
    session_dir: Path,
    *,
    max_attempts: int = 3,
    force: bool = False,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session_dir = session_dir.expanduser().resolve()
    meta_path = session_dir / META_FILE
    if not meta_path.exists():
        raise FileNotFoundError(f"Missing {META_FILE}: {session_dir}")
    meta = read_json(meta_path)
    if not has_recorded_audio(session_dir, meta):
        raise ValueError(f"No non-empty audio tracks in {session_dir}")

    job_path = session_dir / JOB_FILE
    if job_path.exists() and not force:
        job = read_json(job_path)
        if job.get("state") == "complete":
            return job
        if int(job.get("attempts", 0)) >= int(
            job.get("max_attempts", max_attempts)
        ):
            return job
    else:
        job = default_job(max_attempts)

    job["state"] = "pending"
    job["updated_at"] = utc_now()
    job["progress"] = {
        "phase": "queued",
        "fraction": 0.0,
        "detail": "Waiting for the transcription worker",
        "indeterminate": False,
    }
    if options:
        job["options"] = options
    if force:
        job["attempts"] = 0
        job["last_error"] = None
    atomic_write_json(job_path, job)
    append_log(session_dir, "Session queued")
    return job


def recover_sessions(
    recordings_dir: Path,
    *,
    max_attempts: int = 3,
) -> list[Path]:
    recovered: list[Path] = []
    for session_dir in discover_sessions(recordings_dir):
        meta = read_json(session_dir / META_FILE)
        if (session_dir / TRANSCRIPT_FILE).exists():
            continue
        if not has_recorded_audio(session_dir, meta):
            continue

        job_path = session_dir / JOB_FILE
        if not job_path.exists():
            enqueue_session(session_dir, max_attempts=max_attempts)
            recovered.append(session_dir)
            continue

        job = read_json(job_path)
        if job.get("state") == "processing":
            job["state"] = "pending"
            job["updated_at"] = utc_now()
            job["last_error"] = "Recovered after interrupted worker"
            atomic_write_json(job_path, job)
            append_log(session_dir, "Recovered interrupted worker")
            recovered.append(session_dir)
    return recovered


@dataclass
class JobResult:
    session_dir: Path
    state: str
    attempts: int
    error: str | None = None


def run_job(
    session_dir: Path,
    processor: Callable[[Path], None],
) -> JobResult:
    job_path = session_dir / JOB_FILE
    job = read_json(job_path)
    if job.get("state") == "complete":
        return JobResult(
            session_dir,
            "complete",
            int(job.get("attempts", 0)),
        )

    attempts = int(job.get("attempts", 0)) + 1
    maximum = int(job.get("max_attempts", 3))
    job.update(
        {
            "state": "processing",
            "attempts": attempts,
            "started_at": utc_now(),
            "updated_at": utc_now(),
        }
    )
    atomic_write_json(job_path, job)
    update_job_progress(
        session_dir,
        phase="starting",
        fraction=0.02,
        detail=f"Starting attempt {attempts}/{maximum}",
    )
    append_log(session_dir, f"Processing attempt {attempts}/{maximum}")

    try:
        processor(session_dir)
    except Exception as exc:
        job = read_json(job_path)
        state = "failed" if attempts >= maximum else "pending"
        job.update(
            {
                "state": state,
                "updated_at": utc_now(),
                "last_error": f"{type(exc).__name__}: {exc}",
            }
        )
        job["progress"] = {
            "phase": "failed" if state == "failed" else "retrying",
            "fraction": float(
                job.get("progress", {}).get("fraction", 0.0)
            ),
            "detail": f"{type(exc).__name__}: {exc}",
            "indeterminate": False,
        }
        atomic_write_json(job_path, job)
        append_log(session_dir, f"Attempt failed: {job['last_error']}")
        return JobResult(session_dir, state, attempts, job["last_error"])

    job.update(
        {
            "state": "complete",
            "completed_at": utc_now(),
            "updated_at": utc_now(),
            "last_error": None,
        }
    )
    job["progress"] = {
        "phase": "complete",
        "fraction": 1.0,
        "detail": "Transcript ready",
        "indeterminate": False,
    }
    atomic_write_json(job_path, job)
    meta_path = session_dir / META_FILE
    meta = read_json(meta_path)
    meta["state"] = (
        "transcribed"
        if (session_dir / TRANSCRIPT_FILE).exists()
        else "processed"
    )
    meta["transcribed_at"] = utc_now()
    atomic_write_json(meta_path, meta)
    append_log(session_dir, "Transcription complete")
    return JobResult(session_dir, "complete", attempts)


def pending_sessions(
    recordings_dir: Path,
    requested: Path | None = None,
) -> list[Path]:
    candidates = (
        [requested.expanduser().resolve()]
        if requested is not None
        else list(discover_sessions(recordings_dir))
    )
    pending: list[Path] = []
    for session_dir in candidates:
        job_path = session_dir / JOB_FILE
        if not job_path.exists():
            continue
        job = read_json(job_path)
        if job.get("state") == "pending":
            pending.append(session_dir)
    return sorted(pending)


def run_on_stop(command: str, session_dir: Path) -> None:
    environment = os.environ.copy()
    environment["TAPRECORD_SESSION_DIR"] = str(session_dir)
    completed = subprocess.run(
        ["/bin/zsh", "-lc", f'{command} "$1"', "taprecord-on-stop", str(session_dir)],
        cwd=session_dir,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"on_stop exited with status {completed.returncode}"
        )
