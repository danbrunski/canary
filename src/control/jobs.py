"""Restricted background jobs for the daemon control API."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from src.config import CanaryConfig
from src.control.sessions import resolve_session

JobKind = Literal[
    "retrain_if",
    "retrain_lstm",
    "retrain_all",
    "refit_behavior_models",
    "corpus_report",
]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
CommandBuilder = Callable[[str, Mapping[str, Any]], Sequence[str]]
EventCallback = Callable[[dict[str, Any]], None]

ALLOWED_JOB_KINDS: set[str] = {
    "retrain_if",
    "retrain_lstm",
    "retrain_all",
    "refit_behavior_models",
    "corpus_report",
}


class _JobCancelledBeforeSpawnError(Exception):
    """Internal sentinel for a job cancelled before subprocess launch."""


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    kind: str
    status: JobStatus
    created_at: str
    log_path: Path
    started_at: str | None = None
    finished_at: str | None = None
    returncode: int | None = None
    command: tuple[str, ...] = ()
    params: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
            "log_path": str(self.log_path),
            "command": list(self.command),
            "params": self.params or {},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JobRecord:
        status = _job_status(data["status"])
        return cls(
            job_id=str(data["job_id"]),
            kind=str(data["kind"]),
            status=status,
            created_at=str(data["created_at"]),
            started_at=_optional_str(data.get("started_at")),
            finished_at=_optional_str(data.get("finished_at")),
            returncode=_optional_int(data.get("returncode")),
            log_path=Path(str(data["log_path"])),
            command=tuple(str(item) for item in data.get("command", [])),
            params=dict(data.get("params", {})),
        )


class JobController:
    """Run allow-listed maintenance jobs in background threads."""

    def __init__(
        self,
        *,
        config: CanaryConfig,
        repo_root: Path,
        config_path: Path,
        jobs_dir: Path,
        python: Path | None = None,
        command_builder: CommandBuilder | None = None,
        event_callback: EventCallback | None = None,
    ) -> None:
        self._config = config
        self._repo_root = repo_root
        self._config_path = config_path
        self._jobs_dir = jobs_dir
        self._python = python or _default_python(repo_root)
        self._command_builder = command_builder
        self._event_callback = event_callback
        self._ledger_path = jobs_dir / "jobs.jsonl"
        self._jobs: dict[str, JobRecord] = _load_jobs(self._ledger_path)
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancel_requested: set[str] = set()
        self._lock = threading.Lock()

    def set_event_callback(self, callback: EventCallback | None) -> None:
        self._event_callback = callback

    def submit(self, kind: str, params: Mapping[str, Any] | None = None) -> JobRecord:
        if kind not in ALLOWED_JOB_KINDS:
            raise ValueError(f"unsupported job kind: {kind}")
        clean_params = dict(params or {})
        job_id = uuid4().hex
        log_path = self._jobs_dir / f"{job_id}.log"
        command = tuple(self._build_command(kind, clean_params))
        record = JobRecord(
            job_id=job_id,
            kind=kind,
            status="queued",
            created_at=datetime.now(UTC).isoformat(),
            log_path=log_path,
            command=command,
            params=clean_params,
        )
        with self._lock:
            self._jobs[job_id] = record
            self._persist_locked(record)
        self._emit(record)
        thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
        thread.start()
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda job: job.created_at)

    def logs(self, job_id: str) -> str | None:
        job = self.get(job_id)
        if job is None:
            return None
        if not job.log_path.exists():
            return ""
        return job.log_path.read_text(errors="replace")

    def cancel(self, job_id: str) -> JobRecord | None:
        """Request cancellation for a queued/running job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status in {"succeeded", "failed", "cancelled"}:
                return job
            self._cancel_requested.add(job_id)
            process = self._processes.get(job_id)
            if process is None:
                cancelled = replace(
                    job,
                    status="cancelled",
                    finished_at=datetime.now(UTC).isoformat(),
                    returncode=None,
                )
                self._jobs[job_id] = cancelled
                self._persist_locked(cancelled)
                out = cancelled
            else:
                try:
                    process.terminate()
                except OSError:
                    pass
                out = job
        self._emit(out)
        return out

    def _build_command(self, kind: str, params: Mapping[str, Any]) -> Sequence[str]:
        if self._command_builder is not None:
            return self._command_builder(kind, params)

        common_sessions = self._session_args(params)
        config_arg = ["--config", str(self._config_path)]
        bindings_arg = ["--bindings", "data/bindings.yaml"]
        ledger_arg = ["--ledger", str(self._config.daemon.training_ledger)]
        if kind == "retrain_if":
            return [
                str(self._python),
                "scripts/retrain.py",
                *common_sessions,
                *config_arg,
                *bindings_arg,
                *ledger_arg,
                "--if-out",
                str(self._config.daemon.models_dir / "isolation_forest"),
                "--skip-lstm",
            ]
        if kind == "retrain_lstm":
            device = str(params.get("device", "cpu"))
            return [
                str(self._python),
                "scripts/retrain.py",
                *common_sessions,
                *config_arg,
                *bindings_arg,
                *ledger_arg,
                "--lstm-out",
                str(self._config.daemon.models_dir / "lstm_autoencoder"),
                "--skip-if",
                "--device",
                device,
            ]
        if kind == "retrain_all":
            device = str(params.get("device", "cpu"))
            return [
                str(self._python),
                "scripts/retrain.py",
                *common_sessions,
                *config_arg,
                *bindings_arg,
                *ledger_arg,
                "--if-out",
                str(self._config.daemon.models_dir / "isolation_forest"),
                "--lstm-out",
                str(self._config.daemon.models_dir / "lstm_autoencoder"),
                "--device",
                device,
            ]
        if kind == "refit_behavior_models":
            command = [
                str(self._python),
                "scripts/refit_behavior_models.py",
                *common_sessions,
                *config_arg,
                *bindings_arg,
                "--out",
                "data/behavior_models.yaml",
            ]
            for recipe in _string_list(params.get("recipes", [])):
                command.extend(["--recipe", recipe])
            if bool(params.get("dry_run", False)):
                command.append("--dry-run")
            return command
        if kind == "corpus_report":
            out = self._repo_root / "data" / "analyses" / _corpus_report_name()
            return [
                str(self._python),
                "tools/corpus_report.py",
                "--sessions-root",
                str(self._config.collection.storage_path),
                "--bindings",
                "data/bindings.yaml",
                "--out",
                str(out),
            ]
        raise ValueError(f"unsupported job kind: {kind}")

    def _session_args(self, params: Mapping[str, Any]) -> list[str]:
        session_ids = _string_list(params.get("session_ids", []))
        if session_ids:
            return [
                str(resolve_session(self._config.collection.storage_path, session_id))
                for session_id in session_ids
            ]
        if bool(params.get("all", False)):
            return ["--sessions-root", str(self._config.collection.storage_path), "--all"]
        days = int(params.get("days", 30))
        return ["--sessions-root", str(self._config.collection.storage_path), "--days", str(days)]

    def _run_job(self, job_id: str) -> None:
        running = self._start_running(job_id)
        if running is None:
            return
        returncode = 1
        cancelled = False
        try:
            running.log_path.parent.mkdir(parents=True, exist_ok=True)
            with running.log_path.open("w") as log_file:
                log_file.write(f"$ {' '.join(running.command)}\n\n")
                log_file.flush()
                with self._lock:
                    if job_id in self._cancel_requested:
                        cancelled = True
                        returncode = -15
                        raise _JobCancelledBeforeSpawnError
                    process = subprocess.Popen(
                        [*running.command],
                        cwd=self._repo_root,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    self._processes[job_id] = process
                returncode = process.wait()
                with self._lock:
                    self._processes.pop(job_id, None)
                    cancelled = job_id in self._cancel_requested
        except _JobCancelledBeforeSpawnError:
            pass
        except Exception as exc:  # noqa: BLE001 - job failure belongs in job state
            with running.log_path.open("a") as log_file:
                log_file.write(f"\njob runner failed: {exc}\n")
            returncode = 1
            with self._lock:
                cancelled = job_id in self._cancel_requested
        final = replace(
            running,
            status="cancelled" if cancelled else ("succeeded" if returncode == 0 else "failed"),
            finished_at=datetime.now(UTC).isoformat(),
            returncode=returncode,
        )
        self._set(final)

    def _start_running(self, job_id: str) -> JobRecord | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status == "cancelled" or job_id in self._cancel_requested:
                return None
            running = replace(job, status="running", started_at=datetime.now(UTC).isoformat())
            self._jobs[job_id] = running
            self._persist_locked(running)
        self._emit(running)
        return running

    def _set(self, record: JobRecord) -> None:
        with self._lock:
            self._jobs[record.job_id] = record
            self._persist_locked(record)
        self._emit(record)

    def _emit(self, record: JobRecord) -> None:
        if self._event_callback is not None:
            try:
                self._event_callback({"type": "job", "job": record.to_dict()})
            except Exception:
                pass

    def _persist_locked(self, record: JobRecord) -> None:
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self._ledger_path.open("a") as handle:
            handle.write(json.dumps(record.to_dict(), default=str) + "\n")
            handle.flush()


def _default_python(repo_root: Path) -> Path:
    candidate = repo_root / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


def _load_jobs(path: Path) -> dict[str, JobRecord]:
    if not path.exists():
        return {}
    jobs: dict[str, JobRecord] = {}
    with path.open("r") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = JobRecord.from_dict(json.loads(line))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            record = _recover_interrupted_job(record)
            jobs[record.job_id] = record
    return jobs


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _job_status(value: Any) -> JobStatus:
    status = str(value)
    if status not in {"queued", "running", "succeeded", "failed", "cancelled"}:
        raise ValueError(f"unsupported job status: {status}")
    return cast(JobStatus, status)


def _recover_interrupted_job(record: JobRecord) -> JobRecord:
    if record.status not in {"queued", "running"}:
        return record
    return replace(
        record,
        status="failed",
        finished_at=record.finished_at or datetime.now(UTC).isoformat(),
        returncode=record.returncode,
    )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ValueError("expected a string or list of strings")


def _corpus_report_name() -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%S")
    return f"corpus-report-{stamp}.md"


__all__ = ["ALLOWED_JOB_KINDS", "JobController", "JobRecord"]
