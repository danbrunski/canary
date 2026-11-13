"""Restricted background jobs for the daemon control API."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from src.coerce import optional_str as _optional_str
from src.config import CanaryConfig
from src.jsonl_store import append_jsonl_line
from src.models.override_reason import require_override_reason, stated_override_reason
from src.session import format_session_timestamp
from src.storage.sessions import resolve_session

log = logging.getLogger(__name__)

JobKind = Literal[
    "retrain_if",
    "retrain_lstm",
    "retrain_tcn",
    "retrain_all",
    "refit_behavior_models",
    "corpus_report",
    "export_dataset",
    "did_sweep",
]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
CommandBuilder = Callable[[str, Mapping[str, Any]], Sequence[str]]
EventCallback = Callable[[dict[str, Any]], None]

ALLOWED_JOB_KINDS: set[str] = {
    "retrain_if",
    "retrain_lstm",
    "retrain_tcn",
    "retrain_all",
    "refit_behavior_models",
    "corpus_report",
    "replay_window",
    "export_dataset",
    "did_sweep",
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
        compute_target: str = "pi",
    ) -> None:
        self._config = config
        self._repo_root = repo_root
        self._config_path = config_path
        self._jobs_dir = jobs_dir
        self._python = python or _default_python(repo_root)
        self._command_builder = command_builder
        self._event_callback = event_callback
        #: Task 6.36 — threaded into `scripts/retrain.py --compute-target` by
        #: the default command builder below so the shared training-run
        #: ledger records where a job actually ran. The Pi daemon's own
        #: `JobController` (created without an override) is "pi"; the
        #: companion's (`_make_job_controller`) passes "laptop". Ignored by
        #: a custom `command_builder`, if one is supplied.
        self._compute_target = compute_target
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
        if "accept_thin_calibration" in clean_params:
            reason = clean_params["accept_thin_calibration"]
            # A02.2: the override is an operator's stated reason, stamped into
            # the manifest. Anything but a non-empty string is not a reason.
            refusal = "accept_thin_calibration must be a non-empty reason string"
            if not isinstance(reason, str):
                raise ValueError(refusal)
            clean_params["accept_thin_calibration"] = require_override_reason(
                reason, refusal=refusal
            )
        job_id = uuid4().hex
        log_path = self._jobs_dir / f"{job_id}.log"
        command = tuple(self._build_command(kind, clean_params, job_id=job_id))
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

    def _build_command(
        self,
        kind: str,
        params: Mapping[str, Any],
        *,
        job_id: str | None = None,
    ) -> Sequence[str]:
        if self._command_builder is not None:
            return self._command_builder(kind, params)

        common_sessions = self._session_args(params)
        config_arg = ["--config", str(self._config_path)]
        bindings_arg = ["--bindings", "data/bindings.yaml"]
        ledger_arg = ["--ledger", str(self._config.daemon.training_ledger)]
        job_arg = ["--job-id", job_id] if job_id is not None else []
        # Task 6.36 — every retrain.py invocation records where it ran.
        compute_target_arg = ["--compute-target", self._compute_target]
        if kind == "retrain_if":
            return [
                str(self._python),
                "scripts/retrain.py",
                *common_sessions,
                *config_arg,
                *bindings_arg,
                *ledger_arg,
                *job_arg,
                *compute_target_arg,
                "--if-out",
                str(self._config.daemon.models_dir / "isolation_forest"),
                "--skip-sequence",
            ]
        # A02.2: the sequence trainer refuses a calibration that does not cover
        # what it will score; an operator's stated reason (and only that)
        # publishes anyway, stamped into the manifest.
        thin_reason = stated_override_reason(str(params.get("accept_thin_calibration") or ""))
        thin_arg = ["--accept-thin-calibration", thin_reason] if thin_reason is not None else []
        if kind in ("retrain_lstm", "retrain_tcn"):
            device = str(params.get("device", "cpu"))
            sequence_model = "lstm" if kind == "retrain_lstm" else "tcn"
            model_type = "lstm_autoencoder" if kind == "retrain_lstm" else "tcn_autoencoder"
            return [
                str(self._python),
                "scripts/retrain.py",
                *common_sessions,
                *config_arg,
                *bindings_arg,
                *ledger_arg,
                *job_arg,
                *compute_target_arg,
                "--sequence-model",
                sequence_model,
                "--sequence-out",
                str(self._config.daemon.models_dir / model_type),
                "--skip-if",
                "--device",
                device,
                *thin_arg,
            ]
        if kind == "retrain_all":
            device = str(params.get("device", "cpu"))
            # IF + the *configured* sequence backbone (retrain.py defaults
            # --sequence-model to models.sequence_detector); point its output at
            # the matching artifact dir so the daemon's ModelManager finds it.
            sequence_model = self._config.models.sequence_detector
            model_type = "lstm_autoencoder" if sequence_model == "lstm" else "tcn_autoencoder"
            return [
                str(self._python),
                "scripts/retrain.py",
                *common_sessions,
                *config_arg,
                *bindings_arg,
                *ledger_arg,
                *job_arg,
                *compute_target_arg,
                "--if-out",
                str(self._config.daemon.models_dir / "isolation_forest"),
                "--sequence-model",
                sequence_model,
                "--sequence-out",
                str(self._config.daemon.models_dir / model_type),
                "--device",
                device,
                *thin_arg,
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
        if kind == "replay_window":
            return self._replay_window_command(params, job_id=job_id, config_arg=config_arg)
        if kind == "export_dataset":
            return self._export_dataset_command(params, config_arg=config_arg)
        if kind == "did_sweep":
            return self._did_sweep_command(params, job_id=job_id, config_arg=config_arg)
        raise ValueError(f"unsupported job kind: {kind}")

    def _replay_window_command(
        self,
        params: Mapping[str, Any],
        *,
        job_id: str | None,
        config_arg: list[str],
    ) -> list[str]:
        """Build the ``tools/replay_window.py`` invocation.

        ``params`` must include ``session_id``. ``start``/``end`` are
        optional ISO timestamps; ``if_model``/``lstm_model``/``anomalies_dir``
        are optional path overrides. The job's output JSON lands at
        ``<jobs_dir>/<job_id>.replay.json`` so the MCP client can read it
        once the job reaches a terminal state.
        """
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("replay_window requires params.session_id")
        out_path = (
            self._jobs_dir / f"{job_id}.replay.json"
            if job_id is not None
            else self._jobs_dir / "replay.json"
        )
        command: list[str] = [
            str(self._python),
            "tools/replay_window.py",
            "--session-id",
            session_id,
            *config_arg,
            "--out",
            str(out_path),
            "--pretty",
        ]
        for key, flag in (
            ("start", "--start"),
            ("end", "--end"),
            ("if_model", "--if-model"),
            ("lstm_model", "--lstm-model"),
            ("anomalies_dir", "--anomalies-dir"),
        ):
            value = params.get(key)
            if value is not None:
                command.extend([flag, str(value)])
        max_rows = params.get("max_rows")
        if isinstance(max_rows, int) and max_rows > 0:
            command.extend(["--max-rows", str(max_rows)])
        max_windows = params.get("max_windows")
        if isinstance(max_windows, int) and max_windows > 0:
            command.extend(["--max-windows", str(max_windows)])
        return command

    def _did_sweep_command(
        self,
        params: Mapping[str, Any],
        *,
        job_id: str | None,
        config_arg: list[str],
    ) -> list[str]:
        """Build the ``tools/did_sweep.py`` invocation.

        ``params`` must include ``start``/``end`` (DID range, int or hex
        string) and ``label`` (tags the snapshot — the two ends of a
        before/after comparison need distinct labels). The job's output JSON
        lands at ``<jobs_dir>/<job_id>.did_sweep.json``, the same
        ``<job_id>.<kind>.json`` convention ``replay_window`` uses, so
        ``diff_did_snapshots`` (and the dashboard's ``/result`` route) can
        find it from the job record alone.
        """
        start = params.get("start")
        end = params.get("end")
        label = params.get("label")
        if start is None or end is None:
            raise ValueError("did_sweep requires params.start and params.end")
        if not isinstance(label, str) or not label:
            raise ValueError("did_sweep requires params.label")
        out_path = (
            self._jobs_dir / f"{job_id}.did_sweep.json"
            if job_id is not None
            else self._jobs_dir / "did_sweep.json"
        )
        command = [
            str(self._python),
            "tools/did_sweep.py",
            "--start",
            str(start),
            "--end",
            str(end),
            "--label",
            label,
            *config_arg,
            "--out",
            str(out_path),
        ]
        for key, flag in (
            ("header", "--header"),
            ("session", "--session"),
            ("keepalive", "--keepalive"),
            ("delay_seconds", "--delay-seconds"),
            ("read_timeout", "--read-timeout"),
            # Same-instant batched reads. `widths_from` points at an earlier
            # snapshot; the tool refuses batch > 1 without it, because a
            # multi-DID response cannot be split without measured widths.
            ("batch", "--batch"),
            ("widths_from", "--widths-from"),
        ):
            value = params.get(key)
            if value is not None:
                command.extend([flag, str(value)])
        return command

    def _export_dataset_command(
        self, params: Mapping[str, Any], *, config_arg: list[str]
    ) -> list[str]:
        """Build the ``scripts/export_dataset.py`` invocation (Task 5.6b) —
        the "Export" panel's backend, wired to the existing CLI/
        :mod:`src.datasets.export` rather than a parallel implementation.
        """
        session_ids = _string_list(params.get("session_ids", []))
        if not session_ids:
            raise ValueError("export_dataset requires params.session_ids (at least one)")
        command = [str(self._python), "scripts/export_dataset.py"]
        for session_id in session_ids:
            command.extend(["--session", session_id])
        for channel in _string_list(params.get("channels", [])):
            command.extend(["--channel", channel])
        resample_hz = params.get("resample_hz")
        if resample_hz is not None:
            command.extend(["--resample-hz", str(resample_hz)])
        for tag in _string_list(params.get("include_tags", [])):
            command.extend(["--include-tag", tag])
        for tag in _string_list(params.get("exclude_tags", [])):
            command.extend(["--exclude-tag", tag])
        for fmt in _string_list(params.get("formats", ["csv"])) or ["csv"]:
            command.extend(["--format", fmt])
        command.extend(config_arg)
        purpose = str(params.get("purpose", ""))
        if purpose:
            command.extend(["--purpose", purpose])
        if not bool(params.get("zip", True)):
            command.append("--no-zip")
        out_dir = params.get("out_dir")
        command.extend(
            ["--out", str(out_dir) if out_dir else str(self._repo_root / "data" / "datasets")]
        )
        name = params.get("name")
        if name:
            command.extend(["--name", str(name)])
        return command

    def _session_args(self, params: Mapping[str, Any]) -> list[str]:
        session_ids = _string_list(params.get("session_ids", []))
        if session_ids:
            return [
                str(resolve_session(self._config.collection.storage_path, session_id))
                for session_id in session_ids
            ]
        if bool(params.get("all", False)):
            return ["--sessions-root", str(self._config.collection.storage_path), "--all"]
        since = params.get("since")
        if since is not None:
            # Task 3.1/3.2: the online-training scheduler's rolling window —
            # `retrain.py --since` (sliding window, calibration holdout always
            # reserved) rather than `--days`'s relative lookback. Only ever
            # reached via `since` in `params`, never `--no-calibration-holdout`
            # — that flag has no params-level path into this builder at all,
            # so a scheduled retrain structurally cannot disable the holdout.
            return [
                "--sessions-root",
                str(self._config.collection.storage_path),
                "--since",
                str(since),
            ]
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
                # A publisher that raises must not fail the job: the record is
                # already persisted and the dashboard re-reads it. Say so, once
                # per transition, rather than lose the reason.
                log.warning(
                    "job event callback failed; the job record is unaffected",
                    extra={"job_id": record.job_id, "job_status": record.status},
                    exc_info=True,
                )

    def _persist_locked(self, record: JobRecord) -> None:
        # Task 3.4: this ledger keeps an in-memory `dict[str, JobRecord]`
        # cache mutated incrementally under `self._lock` (concurrent job
        # execution needs that, unlike the re-scan-on-every-read stores), so
        # it doesn't fit `JsonlStore` itself — but the append body is the
        # same primitive.
        append_jsonl_line(self._ledger_path, record.to_dict(), sort_keys=False)


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
    stamp = format_session_timestamp(datetime.now(UTC))
    return f"corpus-report-{stamp}.md"


__all__ = ["ALLOWED_JOB_KINDS", "JobController", "JobRecord"]
