"""Restricted background jobs for the daemon control API."""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
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
JobStatus = Literal["queued", "running", "succeeded", "failed"]
CommandBuilder = Callable[[str, Mapping[str, Any]], Sequence[str]]

ALLOWED_JOB_KINDS: set[str] = {
    "retrain_if",
    "retrain_lstm",
    "retrain_all",
    "refit_behavior_models",
    "corpus_report",
}


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
        }


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
    ) -> None:
        self._config = config
        self._repo_root = repo_root
        self._config_path = config_path
        self._jobs_dir = jobs_dir
        self._python = python or _default_python(repo_root)
        self._command_builder = command_builder
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, params: Mapping[str, Any] | None = None) -> JobRecord:
        if kind not in ALLOWED_JOB_KINDS:
            raise ValueError(f"unsupported job kind: {kind}")
        job_id = uuid4().hex
        log_path = self._jobs_dir / f"{job_id}.log"
        command = tuple(self._build_command(kind, params or {}))
        record = JobRecord(
            job_id=job_id,
            kind=kind,
            status="queued",
            created_at=datetime.now(UTC).isoformat(),
            log_path=log_path,
            command=command,
        )
        with self._lock:
            self._jobs[job_id] = record
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

    def _build_command(self, kind: str, params: Mapping[str, Any]) -> Sequence[str]:
        if self._command_builder is not None:
            return self._command_builder(kind, params)

        common_sessions = self._session_args(params)
        config_arg = ["--config", str(self._config_path)]
        bindings_arg = ["--bindings", "data/bindings.yaml"]
        if kind == "retrain_if":
            return [
                str(self._python),
                "scripts/retrain.py",
                *common_sessions,
                *config_arg,
                *bindings_arg,
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
        job = self.get(job_id)
        if job is None:
            return
        running = replace(job, status="running", started_at=datetime.now(UTC).isoformat())
        self._set(running)
        returncode = 1
        try:
            running.log_path.parent.mkdir(parents=True, exist_ok=True)
            with running.log_path.open("w") as log_file:
                log_file.write(f"$ {' '.join(running.command)}\n\n")
                log_file.flush()
                completed = subprocess.run(
                    [*running.command],
                    cwd=self._repo_root,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                )
                returncode = completed.returncode
        except Exception as exc:  # noqa: BLE001 - job failure belongs in job state
            with running.log_path.open("a") as log_file:
                log_file.write(f"\njob runner failed: {exc}\n")
            returncode = 1
        final = replace(
            running,
            status="succeeded" if returncode == 0 else "failed",
            finished_at=datetime.now(UTC).isoformat(),
            returncode=returncode,
        )
        self._set(final)

    def _set(self, record: JobRecord) -> None:
        with self._lock:
            self._jobs[record.job_id] = record


def _default_python(repo_root: Path) -> Path:
    candidate = repo_root / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


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
