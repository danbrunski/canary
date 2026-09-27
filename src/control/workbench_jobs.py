"""Run the workbench's whole-session reads without competing with the collector.

The Logs and Analysis pages (W04) read whole sessions — a frame of one log, or
a surface pooled over fifteen. Each of those is the same kind of work as the
trip-end digest, and inherits the digest's three protections plus two of its
own (AGENTS.md, "No analysis on the Pi while it collects"):

1. **Refused while a capture is recording.** A capture is the one time the
   collector must not lose memory or CPU to a report; a nice-19 pandas run once
   cost a 3-minute outage and 150k dropped records. The refusal names the
   profile and the laptop path instead of competing.
2. **Size preflight.** A session over ``daemon.digest_row_budget`` (raw
   reads), or over ``daemon.digest_span_budget_seconds`` for ``classify``,
   which reads part by part (P02.1), is not read; it is reported as
   ``over_budget`` beside the result.
3. **One child at a time.** The spawn child and ``RLIMIT_AS`` ceiling are the
   digest's own (:mod:`src.control.trip_end`), and the job waits for neither:
   an identical request in flight is shared, anything else — including a
   running digest — is refused as busy rather than queued behind it.
"""

from __future__ import annotations

import asyncio
import functools
import json
from concurrent.futures import BrokenExecutor, Executor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from src.analysis.digest import (
    digest_address_space_bytes,
    digest_row_budget,
    digest_span_budget_seconds,
)
from src.analysis.rom_trace import cell_residency, load_viewer_definition
from src.analysis.tune_revisions import resolve_session_revision
from src.analysis.tuning_context import context_fields
from src.analysis.workbench import (
    Unavailable,
    WorkbenchInputs,
    pooled_delivery_error,
    pull_comparison,
    session_frame,
    timing_surface,
    trim_surface,
)
from src.collection import tuning_profile
from src.config import CanaryConfig
from src.control.trip_end import (
    DIGEST_TIMEOUT_SECONDS,
    _discard_pool,
    _spawn_pool,
    any_digest_in_flight,
    limit_address_space,
)
from src.profiles import active_registry, profile_dir
from src.session import session_sort_key
from src.storage.session_files import count_session_rows, session_time_span_seconds
from src.storage.sessions import is_session_dir_name, resolve_session, session_started_at

#: Most sessions one pooled request may name. Each is read in turn in the
#: child, so this bounds wall time rather than memory.
MAX_POOLED_SESSIONS = 40

JOB_KINDS = (
    "frame",
    "delivery_error",
    "trim_surface",
    "timing",
    "pull_comparison",
    "cell_residency",
    # R03.2: POST /control/sessions/{id}/classification. The only kind that
    # writes (the classification + fingerprint sidecars); it needs neither
    # the bindings nor the DID catalog.
    "classify",
)

#: Kinds that compute from the session and ``config`` alone, without the
#: profile's workbench inputs.
_CONFIG_ONLY_KINDS = frozenset({"classify"})


class WorkbenchRefusedError(Exception):
    """The request was refused before any data was read. ``status`` is the
    HTTP code the route answers with; ``detail`` its JSON body."""

    def __init__(self, status: int, detail: dict[str, Any]) -> None:
        super().__init__(str(detail.get("message", detail)))
        self.status = status
        self.detail = detail


def laptop_path(config: CanaryConfig, sessions: list[str] | None = None) -> str:
    """The instruction a deferred request carries: tar-stream the session(s)
    to the laptop and analyse there — never per-file rsync (AGENTS.md)."""
    names = " ".join(sessions) if sessions else "<session>"
    storage = config.collection.storage_path
    return (
        f"ssh canary@canary.local 'tar -cf - -C {storage} {names}' | tar -xf - "
        "then run the same analysis locally (canary MCP tuning tools or src.analysis.workbench)."
    )


def capture_deferral(config: CanaryConfig, sessions: list[str] | None = None) -> None:
    """Raise the named refusal while a capture is recording."""
    if not tuning_profile.is_active():
        return
    name = tuning_profile.active_profile().name
    raise WorkbenchRefusedError(
        409,
        {
            "error": "analysis_deferred",
            "reason": "capture_active",
            "profile": name,
            "message": f"analysis is deferred while {name!r} is recording",
            "laptop_path": laptop_path(config, sessions),
        },
    )


def workbench_inputs(
    config: CanaryConfig, *, bindings_path: Path, dids_path: Path
) -> WorkbenchInputs:
    """The vehicle's :class:`TuningContext` plus the catalogs, as one picklable bundle."""
    return WorkbenchInputs(
        **context_fields(config), bindings_path=bindings_path, dids_path=dids_path
    )


def _started(session: Path) -> tuple[datetime, str]:
    """Sort key: parsed start time (:func:`src.session.session_sort_key`)."""
    return session_sort_key(session)


def _all_session_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    found = [
        child
        for child in root.iterdir()
        if child.is_dir() and is_session_dir_name(child.name) and session_started_at(child)
    ]
    return sorted(found, key=_started)


@dataclass
class Selection:
    """Which sessions a pooled request reads, and what it left out and why."""

    usable: list[Path]
    over_budget: list[Unavailable] = field(default_factory=list)
    #: Sessions on the requested revision beyond the per-request limit, oldest
    #: first. Named in the response — a capped selection is stated, not silent.
    omitted: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sessions": [p.name for p in self.usable],
            "omitted_by_limit": list(self.omitted),
            "limit": MAX_POOLED_SESSIONS,
        }


def select_sessions(
    config: CanaryConfig,
    *,
    sessions: list[str],
    revision: str | None = None,
    reads: Literal["raw", "value_number"] = "raw",
) -> Selection:
    """Resolve named sessions and/or every session on ``revision``.

    Ordered by :func:`session_started_at`, never by directory name. Named
    sessions beyond the limit are refused; a revision selection keeps the
    newest and names the rest in ``omitted``. Sessions over the budget come
    back as ``over_budget`` rather than being read.

    ``reads`` says what the job reads, which decides the budget: ``"raw"``
    (the default, every analysis job) reads whole sessions of raw rows and is
    bounded by ``digest_row_budget``; ``"value_number"`` (``classify``) runs
    the part-by-part ``resample_and_segment`` chain (P02.1), whose peak
    follows the recorded span, and is bounded by ``digest_span_budget_seconds``.
    """
    root = config.collection.storage_path
    chosen: dict[str, Path] = {}
    for name in sessions:
        try:
            chosen[name] = resolve_session(root, name)
        except FileNotFoundError as exc:
            raise WorkbenchRefusedError(
                404, {"error": "session_not_found", "message": str(exc)}
            ) from exc
    if len(chosen) > MAX_POOLED_SESSIONS:
        raise WorkbenchRefusedError(
            400,
            {
                "error": "too_many_sessions",
                "message": (
                    f"{len(chosen)} sessions selected; one request pools at most "
                    f"{MAX_POOLED_SESSIONS}. Narrow the selection."
                ),
            },
        )
    omitted: list[str] = []
    if revision is not None:
        registry = active_registry(config)
        on_revision = [
            session
            for session in _all_session_dirs(root)
            if session.name not in chosen
            and resolve_session_revision(session, registry)[0] == revision
        ]
        room = MAX_POOLED_SESSIONS - len(chosen)
        keep = on_revision[-room:] if room > 0 else []
        omitted = [s.name for s in on_revision if s not in keep]
        for session in keep:
            chosen[session.name] = session
    if not chosen:
        raise WorkbenchRefusedError(
            400,
            {
                "error": "no_sessions",
                "message": (
                    "Choose sessions or a revision"
                    if revision is None
                    else f"no session resolves to revision {revision!r}"
                ),
            },
        )
    ordered = sorted(chosen.values(), key=_started)
    usable: list[Path] = []
    over: list[Unavailable] = []
    for session in ordered:
        reason = _over_budget(session, config, reads)
        if reason is not None:
            over.append(Unavailable(session.name, "over_budget", reason))
        else:
            usable.append(session)
    return Selection(usable=usable, over_budget=over, omitted=omitted)


def _over_budget(
    session: Path, config: CanaryConfig, reads: Literal["raw", "value_number"]
) -> str | None:
    """Why ``session`` is over the budget for what the job reads, or ``None``."""
    if reads == "value_number":
        span_budget = digest_span_budget_seconds(config)
        span = session_time_span_seconds(session)
        if span_budget and span is not None and span > span_budget:
            return (
                f"{span / 3600:,.2f} h of recorded span exceeds the "
                f"{span_budget / 3600:,.2f} h analysis budget; analyse it on the laptop"
            )
        return None
    budget = digest_row_budget(config)
    rows = count_session_rows(session)
    if budget and rows is not None and rows > budget:
        return f"{rows:,} rows exceeds the {budget:,}-row analysis budget; analyse it on the laptop"
    return None


def run_workbench_job(
    kind: str,
    params: dict[str, Any],
    config: CanaryConfig,
    *,
    bindings_path: Path | None,
    dids_path: Path | None,
    address_space_bytes: int | None = None,
) -> dict[str, Any]:
    """The child-process entry point: cap, load the profile data, compute.

    Module-level and picklable — ``spawn`` imports it by name. The limit goes
    on before any data is read, as :func:`~src.control.trip_end.compute_digest_under_limit`
    does.
    """
    if address_space_bytes is not None:
        limit_address_space(address_space_bytes)
    if kind == "classify":
        from src.control.classification import classify_and_write

        return classify_and_write(config, str(params["session_id"]))
    if bindings_path is None or dids_path is None:
        raise ValueError(f"workbench job {kind!r} needs bindings_path and dids_path")
    inputs = workbench_inputs(config, bindings_path=bindings_path, dids_path=dids_path)
    sessions = [Path(p) for p in params.get("sessions", [])]
    skipped = [Unavailable(**item) for item in params.get("unavailable", [])]
    if kind == "frame":
        return session_frame(
            Path(params["session"]),
            list(params["specs"]),
            inputs=inputs,
            grid=params.get("grid", "1s"),
            regime=params.get("regime", "all"),
            offset=int(params.get("offset", 0)),
            max_rows=int(params.get("max_rows", 20_000)),
        )
    if kind == "delivery_error":
        return pooled_delivery_error(
            sessions,
            inputs=inputs,
            measured=params["measured"],
            min_load=params.get("min_load"),
            unavailable=skipped,
        )
    if kind == "trim_surface":
        return trim_surface(
            sessions,
            inputs=inputs,
            rpm=params["rpm"],
            airflow=params["airflow"],
            min_n=int(params["min_n"]),
            unavailable=skipped,
        )
    if kind == "timing":
        return timing_surface(
            sessions,
            inputs=inputs,
            timing=params["timing"],
            rpm=params["rpm"],
            airflow=params["airflow"],
            min_n=int(params["min_n"]),
            unavailable=skipped,
        )
    if kind == "pull_comparison":
        return pull_comparison(
            [Path(p) for p in params["sessions_a"]],
            [Path(p) for p in params["sessions_b"]],
            revision_a=params["revision_a"],
            revision_b=params["revision_b"],
            inputs=inputs,
            value=params["value"],
            rpm=params["rpm"],
            airflow=params["airflow"],
            min_n=int(params["min_n"]),
            floor_channel=params.get("floor_channel"),
            unavailable=skipped,
        )
    if kind == "cell_residency":
        table = load_viewer_definition(profile_dir(config)).table(params["table"])
        if table is None:
            raise ValueError(f"no ROM table {params['table']!r} in this vehicle's rom_viewer.yaml")
        session = Path(params["session"])
        revision, source, _conflict = resolve_session_revision(session, inputs.registry)
        return cell_residency(
            session,
            table=table,
            revision=revision,
            revision_source=source,
            calibration=inputs.calibration,
            bindings_path=bindings_path,
            dids_path=dids_path,
        )
    raise ValueError(f"unknown workbench job {kind!r}")


_IN_FLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}


def _job_key(kind: str, params: dict[str, Any]) -> str:
    return kind + ":" + json.dumps(params, sort_keys=True, default=str)


async def run_off_loop(
    kind: str,
    params: dict[str, Any],
    config: CanaryConfig,
    *,
    bindings_path: Path | None = None,
    dids_path: Path | None = None,
    executor: Executor | None = None,
    timeout: float = DIGEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one job in the bounded child; the only way a route may compute one.

    ``bindings_path`` and ``dids_path`` are required by every kind except
    those in ``_CONFIG_ONLY_KINDS``; the child refuses without them.

    Single-flight: the same request already running is awaited and shared;
    a different one (or a digest) running raises ``analysis_busy``. The child
    is the digest's single-use ``spawn`` pool under its address-space ceiling.
    """
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown workbench job {kind!r}")
    if kind not in _CONFIG_ONLY_KINDS and (bindings_path is None or dids_path is None):
        raise ValueError(f"workbench job {kind!r} needs bindings_path and dids_path")
    key = _job_key(kind, params)
    existing = _IN_FLIGHT.get(key)
    if existing is not None:
        return await asyncio.shield(existing)
    if _IN_FLIGHT or any_digest_in_flight():
        raise WorkbenchRefusedError(
            409,
            {
                "error": "analysis_busy",
                "reason": "child_busy",
                "message": (
                    "another whole-session analysis or digest is running; one runs at a "
                    "time so it cannot compete with the collector for memory. Retry when "
                    "it finishes."
                ),
            },
        )
    task = asyncio.ensure_future(
        _compute(
            kind,
            params,
            config,
            bindings_path=bindings_path,
            dids_path=dids_path,
            executor=executor,
            timeout=timeout,
        )
    )
    _IN_FLIGHT[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and _IN_FLIGHT.get(key) is task:
            _IN_FLIGHT.pop(key, None)
        else:
            task.add_done_callback(lambda _t: _IN_FLIGHT.pop(key, None))


async def _compute(
    kind: str,
    params: dict[str, Any],
    config: CanaryConfig,
    *,
    bindings_path: Path | None,
    dids_path: Path | None,
    executor: Executor | None,
    timeout: float,
) -> dict[str, Any]:
    ceiling = digest_address_space_bytes(config)
    call = functools.partial(
        run_workbench_job,
        kind,
        params,
        config,
        bindings_path=bindings_path,
        dids_path=dids_path,
        address_space_bytes=ceiling,
    )
    owned = executor is None
    pool: Executor = _spawn_pool() if executor is None else executor
    finished = False
    try:
        future = asyncio.get_running_loop().run_in_executor(pool, call)
        try:
            result = await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise WorkbenchRefusedError(
                503,
                {
                    "error": "analysis_unavailable",
                    "reason": "child_timed_out",
                    "message": f"analysis did not finish within {timeout:.0f}s",
                },
            ) from exc
        except BrokenExecutor as exc:
            raise WorkbenchRefusedError(
                503,
                {
                    "error": "analysis_unavailable",
                    "reason": "child_failed",
                    "message": f"analysis process failed: {type(exc).__name__}: {exc}",
                },
            ) from exc
        except MemoryError as exc:
            raise WorkbenchRefusedError(
                503,
                {
                    "error": "analysis_unavailable",
                    "reason": "child_out_of_memory",
                    "message": (
                        f"analysis exceeded its {ceiling // (1024 * 1024)} MiB address-space "
                        "ceiling; analyse on the laptop"
                    ),
                },
            ) from exc
        finished = True
        return result
    finally:
        if owned:
            _discard_pool(pool, terminate=not finished)


__all__ = [
    "JOB_KINDS",
    "MAX_POOLED_SESSIONS",
    "Selection",
    "WorkbenchRefusedError",
    "capture_deferral",
    "laptop_path",
    "run_off_loop",
    "run_workbench_job",
    "select_sessions",
    "workbench_inputs",
]
