#!/usr/bin/env python
"""Replay a stored session through the live inference pipeline on a
deterministic clock, and diff the events two source trees emit (R03.9).

The daemon's own replay (``start_replay``) paces rows on the wall clock and
the pipeline ticks on the wall clock, so two replays of one session differ by
timing alone: which rows landed before which tick, and how many ticks the
M-of-N persistence window spans. That makes a "before/after refactor" replay
diff meaningless. This tool removes every wall-clock input:

- rows are fed to ``LiveFeatureSource.observe`` with their **recorded**
  timestamps (the same rows ``replay_session`` would emit: its own
  ``_iter_rows`` and ``probe_rederivability``);
- the pipeline ticks on a fixed grid in **session time**
  (``1 / preprocessing.resample_hz``): every row stamped at or before the tick
  is observed first, ``wall_clock`` returns the tick time, and each tick's
  compute is awaited before the next, so nothing is dropped as
  ``compute_busy``;
- ``time.monotonic`` as seen by ``src.*`` (the persistence gate, the
  vehicle-state cache) reads the same session clock.

The pipeline sees a running replay, as it does under ``start_replay``:
vehicle state ``replay``, and the capture-profile gate driven by the replayed
session's ``capture_profile.json`` (``source: replay_manifest``), never by the
process's live profile — a tuning capture stays unscored (R03.12).

Everything the pipeline emits is recorded: the anomaly events the emitter
wrote (``anomalies_dir/**/*.jsonl``) and every ``/ws/scores`` message.

Subcommands::

    # one tree (default: this checkout), one session -> JSON
    .venv/bin/python tools/replay_event_diff.py run \\
        --session ~/canary-data/sessions-0918/2026-09-18T213818 \\
        --models-dir models --seconds 900 --out after.json

    # compare two runs: categorical fields exact, floats to a tolerance
    .venv/bin/python tools/replay_event_diff.py diff before.json after.json

    # both at once, against two source trees (e.g. `git archive <rev>`)
    .venv/bin/python tools/replay_event_diff.py ab \\
        --before-tree <scratch>/pre --after-tree <scratch>/post \\
        --session ... --models-dir $PWD/models --seconds 900 --out-dir <scratch>

``run`` imports ``src`` from ``--tree`` (it only uses APIs present since
before R03), so ``ab`` runs each side in its own interpreter. Model binaries
are read from ``--models-dir`` (profile-scoped, as the daemon scopes it) and
nothing is written outside ``--scratch``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Fields that carry wall-clock measurements, not detector output.
NONDETERMINISTIC_KEYS = frozenset({"latency_ms"})
DEFAULT_REL_TOL = 1e-9


def _convert(value: Any) -> Any:
    """:func:`src.jsonable.jsonable` of *this checkout*, not of ``--tree``.

    ``run`` imports ``src`` from the tree under test, and ``ab --before-tree``
    may be a revision older than ``src/jsonable.py``. The converter is the
    tool's own instrument, not part of what is measured, so it is loaded from
    the tool's checkout by path: both sides of an ``ab`` serialize alike, and
    an old tree needs nothing added to it.
    """
    global _CONVERTER
    if _CONVERTER is None:
        path = REPO_ROOT / "src" / "jsonable.py"
        spec = importlib.util.spec_from_file_location("_replay_event_diff_jsonable", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONVERTER = module.jsonable
    return _CONVERTER(value, stringify=True)


_CONVERTER: Callable[..., Any] | None = None
DEFAULT_ABS_TOL = 1e-12


# --------------------------------------------------------------------------- run


class _SessionClock:
    """The one clock every tick reads: session time, advanced by the driver."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self._epoch = start

    def monotonic(self) -> float:
        return (self.now - self._epoch).total_seconds()


class _TimeProxy(types.ModuleType):
    """``time`` with ``monotonic`` bound to the session clock."""

    def __init__(self, clock: _SessionClock) -> None:
        super().__init__("time")
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock.monotonic()

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


def _patch_monotonic(proxy: _TimeProxy, seen: set[str], patched: list[types.ModuleType]) -> None:
    """Point every loaded ``src.*`` module's ``time`` global at ``proxy``.

    Re-run before each tick: a module imported lazily by the first tick is
    patched before the second. ``patched`` collects what to restore.
    """
    for name, module in list(sys.modules.items()):
        if name in seen or not (name == "src" or name.startswith("src.")):
            continue
        seen.add(name)
        if getattr(module, "time", None) is time:
            module.time = proxy  # type: ignore[attr-defined]
            patched.append(module)


def _restore_monotonic(patched: list[types.ModuleType]) -> None:
    for module in patched:
        module.time = time  # type: ignore[attr-defined]


def _session_config(scratch: Path, models_dir: Path | None) -> Any:
    from src.validation.harness import harness_config

    config = harness_config(scratch, models_dir=models_dir)
    models = config.models.model_copy(
        update={"out_of_process_scoring": False, "out_of_process_feature_build": False}
    )
    return config.model_copy(update={"models": models})


def _rows(session: Path, *, until_epoch: float | None) -> Iterator[tuple[float, Any]]:
    """``(recorded_epoch, TelemetryRecord)`` in the order ``replay_session``
    emits them, stamped with the recorded time instead of the emit time."""
    from src.collection.replay import _iter_rows, probe_rederivability
    from src.telemetry import TelemetryRecord

    probe = probe_rederivability(session)
    for epoch, fields in _iter_rows(session, skip_sources=probe.skip_sources):
        if until_epoch is not None and epoch > until_epoch:
            return
        yield (
            epoch,
            TelemetryRecord(
                timestamp=datetime.fromtimestamp(epoch, tz=UTC),
                source=fields["source"],  # type: ignore[arg-type]
                channel=str(fields["channel"]),
                value=fields["value"],  # type: ignore[arg-type]
                unit=str(fields["unit"]),
                payload=fields["payload"],  # type: ignore[arg-type]
                metadata=fields["metadata"],  # type: ignore[arg-type]
            ),
        )


@dataclass(frozen=True)
class _SessionReplayState:
    """What ``ReplayController.status()`` reports while ``session`` replays.

    Only the fields the inference path reads during a replay: ``running`` and
    ``replay_id`` (vehicle state ``replay``, the generation token) and the
    replayed session's capture profile, which drives the capture-profile gate.
    """

    running: bool
    replay_id: str
    source_session_id: str
    source_capture_profile: str | None
    source_corpus_eligible: bool


class _SessionReplay:
    """Stands in for the daemon's replay controller for one stored session.

    Without it ``capture_profile_gate`` read the process's live capture
    profile, so a replayed tuning capture was scored and emitted as if it were
    default collection. The daemon judges a replay by the replayed session's
    ``capture_profile.json`` (R03.12); so does this, read once, the way
    ``ReplayController.start`` reads it.
    """

    def __init__(self, session: Path) -> None:
        from src.storage.capture_manifest import read_capture_manifest

        manifest = read_capture_manifest(session)
        self._state = _SessionReplayState(
            running=True,
            replay_id=f"drive-session-{session.name}",
            source_session_id=session.name,
            source_capture_profile=None if manifest is None else manifest.profile,
            source_corpus_eligible=True if manifest is None else manifest.corpus_eligible,
        )

    def status(self) -> _SessionReplayState:
        return self._state


def _session_start(session: Path) -> float:
    import pandas as pd

    first = min(session.glob("telemetry-*.parquet"))
    stamps = pd.to_datetime(pd.read_parquet(first, columns=["timestamp"])["timestamp"], utc=True)
    return float(stamps.min().timestamp())


async def drive_session(
    session: Path,
    *,
    scratch: Path,
    models_dir: Path | None,
    start_offset_seconds: float = 0.0,
    seconds: float | None = None,
    configure: Callable[[Any], Any] | None = None,
    prepare_components: Callable[[Any], None] | None = None,
    observe_pipeline: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Tick ``session`` through ``LiveInferencePipeline`` in session time.

    Returns the run's events, per-tick score messages and emitter counters.
    ``configure`` may replace the loaded config and ``prepare_components``
    may swap model managers before the first tick (tests).
    ``observe_pipeline`` receives the constructed pipeline before the first
    tick, so a caller can record what its detectors see each tick (B03.7:
    ``tools/replay_behavior_compare.py`` records every behavior residual).
    """
    from src.daemon.components import build_components
    from src.daemon.inference import LiveInferencePipeline, build_live_feature_source
    from src.daemon.inference_scheduling import TickSlot

    config = _session_config(scratch, models_dir)
    if configure is not None:
        config = configure(config)
    interval = 1.0 / max(config.preprocessing.resample_hz, 1.0)

    start_epoch = _session_start(session) + start_offset_seconds
    # First tick on the grid after the first row, as a live daemon would be.
    first_tick = math.floor(start_epoch / interval) * interval + interval
    until = None if seconds is None else start_epoch + seconds
    clock = _SessionClock(datetime.fromtimestamp(first_tick, tz=UTC))
    proxy = _TimeProxy(clock)
    seen: set[str] = set()
    patched: list[types.ModuleType] = []

    components = build_components(config)
    replay = _SessionReplay(session)
    components.replay_controller = replay  # type: ignore[assignment]
    if prepare_components is not None:
        prepare_components(components)
    loaded = {
        name: getattr(getattr(manager, "current", None), "serving_version", None)
        for name, manager in sorted(components.model_managers.items())
    }
    scores: list[Any] = []
    publish = components.scores_broadcaster.publish

    def _record_score(message: Any) -> int:
        scores.append(_convert(message))
        return publish(message)

    components.scores_broadcaster.publish = _record_score  # type: ignore[method-assign]
    source = build_live_feature_source(components)
    pipeline = LiveInferencePipeline(
        source,
        components,
        interval_seconds=interval,
        recorder=components.inference_timing,
        wall_clock=lambda: clock.now,
    )
    if observe_pipeline is not None:
        observe_pipeline(pipeline)

    rows = _rows(session, until_epoch=until)
    pending = next(rows, None)
    while pending is not None and pending[0] < start_epoch:
        pending = next(rows, None)
    index = 0
    tick_epoch = first_tick
    try:
        while pending is not None:
            while pending is not None and pending[0] <= tick_epoch:
                source.observe(pending[1])
                pending = next(rows, None)
            clock.now = datetime.fromtimestamp(tick_epoch, tz=UTC)
            _patch_monotonic(proxy, seen, patched)
            await pipeline.run_tick(
                TickSlot(
                    index=index,
                    deadline=float(index) * interval,
                    started=float(index) * interval,
                    lateness_seconds=0.0,
                    skipped_before=0,
                )
            )
            inflight = pipeline.inflight
            if inflight is not None:
                await inflight
            index += 1
            tick_epoch = first_tick + index * interval
    finally:
        await pipeline.close(timeout_seconds=30.0)
        components.event_logger.close()
        _restore_monotonic(patched)

    events: list[Any] = []
    for path in sorted(Path(config.daemon.anomalies_dir).rglob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))
    counters = components.emitter.counters
    return {
        "meta": {
            "session": str(session),
            "tree": str(Path.cwd()),
            "ticks": index,
            "interval_seconds": interval,
            "first_tick": clock_iso(first_tick),
            "last_tick": clock_iso(first_tick + (index - 1) * interval),
            "loaded_models": loaded,
            "capture_profile_gate": {
                "profile": replay.status().source_capture_profile,
                "scoring_allowed": replay.status().source_corpus_eligible,
                "source": "replay_manifest",
            },
        },
        "counters": _convert(
            counters.to_dict() if hasattr(counters, "to_dict") else vars(counters)
        ),
        "events": events,
        "scores": scores,
    }


def clock_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _run(args: argparse.Namespace) -> int:
    tree = Path(args.tree).resolve()
    os.chdir(tree)
    sys.path.insert(0, str(tree))
    scratch = Path(args.scratch or tempfile.mkdtemp(prefix="replay-event-diff-")).resolve()
    result = asyncio.run(
        drive_session(
            Path(args.session).expanduser().resolve(),
            scratch=scratch,
            models_dir=None if args.models_dir is None else Path(args.models_dir).resolve(),
            start_offset_seconds=args.start_offset,
            seconds=args.seconds,
        )
    )
    Path(args.out).write_text(json.dumps(result, indent=1, sort_keys=True))
    meta = result["meta"]
    print(
        f"{meta['tree']}: {meta['ticks']} ticks, {len(result['events'])} events, "
        f"{len(result['scores'])} score messages, models {meta['loaded_models']}"
    )
    return 0


# -------------------------------------------------------------------------- diff


@dataclass
class Difference:
    path: str
    before: Any
    after: Any


@dataclass
class DiffReport:
    differences: list[Difference] = field(default_factory=list)
    compared_floats: int = 0
    max_abs_float_delta: float = 0.0
    #: Record keys (``<timestamp>|<source>|<n>``) present on one side only.
    only_before: list[str] = field(default_factory=list)
    only_after: list[str] = field(default_factory=list)
    compared_by_source: dict[str, int] = field(default_factory=dict)
    differing_by_source: dict[str, int] = field(default_factory=dict)

    @property
    def identical(self) -> bool:
        return not self.differences


def _floatish(value: Any) -> bool:
    return isinstance(value, float | int) and not isinstance(value, bool)


def compare_values(
    before: Any,
    after: Any,
    *,
    rel_tol: float = DEFAULT_REL_TOL,
    abs_tol: float = DEFAULT_ABS_TOL,
    path: str = "",
    report: DiffReport | None = None,
) -> DiffReport:
    """Structural compare: numbers to ``rel_tol``/``abs_tol``, everything else
    exact. Keys in :data:`NONDETERMINISTIC_KEYS` are skipped."""
    report = report if report is not None else DiffReport()
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after), key=str):
            if key in NONDETERMINISTIC_KEYS:
                continue
            sub = f"{path}.{key}" if path else str(key)
            if key not in before or key not in after:
                report.differences.append(Difference(sub, before.get(key), after.get(key)))
                continue
            compare_values(
                before[key], after[key], rel_tol=rel_tol, abs_tol=abs_tol, path=sub, report=report
            )
        return report
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            report.differences.append(Difference(f"{path}[len]", len(before), len(after)))
        for i, (b, a) in enumerate(zip(before, after, strict=False)):
            compare_values(
                b, a, rel_tol=rel_tol, abs_tol=abs_tol, path=f"{path}[{i}]", report=report
            )
        return report
    if _floatish(before) and _floatish(after):
        b, a = float(before), float(after)
        if math.isnan(b) and math.isnan(a):
            return report
        report.compared_floats += 1
        if not (math.isinf(b) or math.isinf(a)):
            report.max_abs_float_delta = max(report.max_abs_float_delta, abs(a - b))
        if not math.isclose(b, a, rel_tol=rel_tol, abs_tol=abs_tol):
            report.differences.append(Difference(path, before, after))
        return report
    if before != after:
        report.differences.append(Difference(path, before, after))
    return report


def _keyed(records: Sequence[Mapping[str, Any]] | None) -> dict[str, Mapping[str, Any]]:
    """Records keyed ``<timestamp>|<source>|<n>`` (``n`` numbers repeats).

    Keying rather than indexing means one extra or missing record reads as
    exactly that, instead of shifting every later record into a mismatch.
    """
    keyed: dict[str, Mapping[str, Any]] = {}
    seen: dict[tuple[str, str], int] = {}
    for record in records or ():
        base = (str(record.get("timestamp")), str(record.get("source")))
        n = seen.get(base, 0)
        seen[base] = n + 1
        keyed[f"{base[0]}|{base[1]}|{n}"] = record
    return keyed


def _compare_records(
    section: str,
    before: Sequence[Mapping[str, Any]] | None,
    after: Sequence[Mapping[str, Any]] | None,
    *,
    rel_tol: float,
    abs_tol: float,
) -> DiffReport:
    left, right = _keyed(before), _keyed(after)
    report = DiffReport()
    report.only_before = sorted(set(left) - set(right))
    report.only_after = sorted(set(right) - set(left))
    for key in report.only_before:
        report.differences.append(Difference(f"{section}[{key}]", "present", None))
    for key in report.only_after:
        report.differences.append(Difference(f"{section}[{key}]", None, "present"))
    for key in sorted(set(left) & set(right)):
        source = key.split("|")[1]
        report.compared_by_source[source] = report.compared_by_source.get(source, 0) + 1
        found = len(report.differences)
        compare_values(
            left[key],
            right[key],
            rel_tol=rel_tol,
            abs_tol=abs_tol,
            path=f"{section}[{key}]",
            report=report,
        )
        if len(report.differences) > found:
            report.differing_by_source[source] = report.differing_by_source.get(source, 0) + 1
    return report


def diff_runs(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    rel_tol: float = DEFAULT_REL_TOL,
    abs_tol: float = DEFAULT_ABS_TOL,
) -> dict[str, DiffReport]:
    """Per-section reports for two ``run`` outputs (``meta.tree`` excluded).

    Events and scores are paired by ``(timestamp, source)``; counters and meta
    structurally.
    """
    reports: dict[str, DiffReport] = {}
    for section in ("events", "scores"):
        reports[section] = _compare_records(
            section, before.get(section), after.get(section), rel_tol=rel_tol, abs_tol=abs_tol
        )
    reports["counters"] = compare_values(
        before.get("counters"),
        after.get("counters"),
        rel_tol=rel_tol,
        abs_tol=abs_tol,
        path="counters",
    )
    meta_b = {k: v for k, v in before.get("meta", {}).items() if k != "tree"}
    meta_a = {k: v for k, v in after.get("meta", {}).items() if k != "tree"}
    reports["meta"] = compare_values(meta_b, meta_a, path="meta")
    return reports


def _event_summary(run: Mapping[str, Any]) -> str:
    counts: dict[tuple[str, str], int] = {}
    for event in run.get("events", []):
        key = (str(event.get("source")), str(event.get("role", "alert")))
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{s}/{r}={n}" for (s, r), n in sorted(counts.items())) or "none"


def print_diff(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    reports: Mapping[str, DiffReport],
    *,
    limit: int,
) -> None:
    print(f"before: {len(before.get('events', []))} events ({_event_summary(before)})")
    print(f"after:  {len(after.get('events', []))} events ({_event_summary(after)})")
    for section, report in reports.items():
        status = "identical" if report.identical else f"{len(report.differences)} differences"
        print(
            f"{section}: {status}; {report.compared_floats} floats compared, "
            f"max |delta| {report.max_abs_float_delta:.3g}"
        )
        if report.only_before or report.only_after:
            print(f"  only before: {report.only_before}")
            print(f"  only after:  {report.only_after}")
        for source, compared in sorted(report.compared_by_source.items()):
            differing = report.differing_by_source.get(source, 0)
            print(f"  {source}: {differing} of {compared} paired records differ")
        for difference in report.differences[:limit]:
            print(f"  {difference.path}: {difference.before!r} -> {difference.after!r}")


def _diff(args: argparse.Namespace) -> int:
    before = json.loads(Path(args.before).read_text())
    after = json.loads(Path(args.after).read_text())
    reports = diff_runs(before, after, rel_tol=args.rel_tol, abs_tol=args.abs_tol)
    print_diff(before, after, reports, limit=args.limit)
    return 0 if all(r.identical for r in reports.values()) else 1


def _ab(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}
    for label, tree in (("before", args.before_tree), ("after", args.after_tree)):
        outputs[label] = out_dir / f"{label}.json"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "run",
            "--tree",
            str(Path(tree).resolve()),
            "--session",
            str(args.session),
            "--out",
            str(outputs[label]),
            "--scratch",
            str(out_dir / f"{label}-scratch"),
            "--start-offset",
            str(args.start_offset),
        ]
        if args.seconds is not None:
            command += ["--seconds", str(args.seconds)]
        if args.models_dir is not None:
            command += ["--models-dir", str(Path(args.models_dir).resolve())]
        env = {**os.environ, "PYTHONPATH": str(Path(tree).resolve())}
        subprocess.run(command, check=True, env=env)
    return _diff(
        argparse.Namespace(
            before=outputs["before"],
            after=outputs["after"],
            rel_tol=args.rel_tol,
            abs_tol=args.abs_tol,
            limit=args.limit,
        )
    )


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_session_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--session", type=Path, required=True, help="Stored session directory.")
        p.add_argument(
            "--models-dir",
            type=Path,
            default=None,
            help="Unscoped models dir holding the artifacts (profile slug appended). "
            "Default: the tree's own models/ (manifests only in a clean checkout).",
        )
        p.add_argument(
            "--start-offset", type=float, default=0.0, help="Skip this many session seconds."
        )
        p.add_argument("--seconds", type=float, default=None, help="Replay only this many seconds.")

    def add_tolerance_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--rel-tol", type=float, default=DEFAULT_REL_TOL)
        p.add_argument("--abs-tol", type=float, default=DEFAULT_ABS_TOL)
        p.add_argument("--limit", type=int, default=20, help="Differences printed per section.")

    run = sub.add_parser("run", help="Replay one session in one tree.")
    add_session_args(run)
    run.add_argument("--tree", type=Path, default=REPO_ROOT, help="Source tree to import src from.")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument(
        "--scratch", type=Path, default=None, help="Writable state (default: a tmp dir)."
    )

    diff = sub.add_parser("diff", help="Compare two run outputs.")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    add_tolerance_args(diff)

    ab = sub.add_parser("ab", help="Run two trees and diff them.")
    add_session_args(ab)
    ab.add_argument("--before-tree", type=Path, required=True)
    ab.add_argument("--after-tree", type=Path, default=REPO_ROOT)
    ab.add_argument("--out-dir", type=Path, required=True)
    add_tolerance_args(ab)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "diff":
        return _diff(args)
    return _ab(args)


if __name__ == "__main__":
    raise SystemExit(main())
