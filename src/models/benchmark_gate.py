"""The regression view of an A02 evaluation report (R06.5).

``scripts/evaluate_detectors.py`` is the one detector-evaluation protocol
(A02.4): held-out *drives*, realistic faults swept over severities on the same
windows, false alarms per engine-hour on clean rows. Retrain's post-train
benchmark gate and the champion/challenger promotion gate
(:mod:`src.models.promotion_gate`) both read its JSON report through this
module. The older synthetic-fault grid (``detector_benchmark``: spike/dropout
injections on five OBD channels) is retired (operator decision 2026-09-25,
R06.5); its reports are refused by name, never compared against this
protocol's.

- :func:`gate_view` reduces a report to what the gates grade: per
  (detector, fault kind, severity) recall and median time to detect, and per
  detector false-alarm episodes per engine-hour over the in-era drives.
- :func:`diff_reports` is the regression compare: two reports of the same
  drives under the same protocol parameters, cell by cell, against a relative
  tolerance. A cell the baseline graded and the current run did not is a
  regression (a detector that stopped being graded has lost coverage, R04.5).
- :func:`incomparable_reason` says why two reports cannot be compared (another
  protocol, other drives, other injection parameters, another holdout).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.models.alert_budget import CADENCE_BOUND, CADENCE_INCIDENT, recorded_cadence
from src.models.manifest import manifest_path_for
from src.models.sequence_protocol import TrainedSequenceModel
from src.models.sequence_registry import SEQUENCE_BACKENDS

log = logging.getLogger(__name__)

#: ``report["protocol"]["name"]`` of an ``evaluate_detectors`` report.
PROTOCOL_NAME = "evaluate_detectors"
#: Bumped whenever a change makes two reports' numbers incomparable (what is
#: scored, how a detection is attributed, how false alarms are counted).
PROTOCOL_VERSION = 1

#: The served stack a promotion or regression is about. The A02.5 baselines
#: (robust-z, PCA, ...) and the ``sequence_global`` ablation are comparison
#: references in the same report, not artifacts under test.
SERVED_DETECTORS: tuple[str, ...] = ("isolation_forest", "sequence", "fused")

#: Relative regression tolerance. The old grid used 0.15 because a per-cell
#: recall over ~8 runs swings a whole run with no behaviour change. Here two
#: comparable reports score *identical* injected windows (same drives, same
#: seed, same severities), so a flipped injection is a change in the model,
#: but one flip is still 1/n of a small cell; 0.15 is kept so the gate's
#: sensitivity does not move with the protocol switch.
DEFAULT_TOLERANCE = 0.15

#: Which direction is "worse" for each graded metric.
_WORSE: dict[str, str] = {
    "recall": "down",
    "median_time_to_detect_seconds": "up",
    "per_hour": "up",
}


class RetiredProtocolError(ValueError):
    """A report from a protocol the gates no longer read (the retired grid)."""


def protocol_mismatch(report: Mapping[str, Any]) -> str | None:
    """Why ``report`` is not this protocol's, or ``None``."""
    protocol = report.get("protocol")
    if not isinstance(protocol, Mapping):
        if "table" in report or "cells" in report:
            return (
                "report is from the retired detector_benchmark grid (R06.5); "
                "re-run scripts/evaluate_detectors.py on it"
            )
        return "report names no evaluation protocol (not an evaluate_detectors report)"
    name = protocol.get("name")
    version = protocol.get("version")
    if name != PROTOCOL_NAME:
        return f"report protocol is {name!r}, not {PROTOCOL_NAME!r}"
    if version != PROTOCOL_VERSION:
        return f"report protocol version {version!r}, this gate reads {PROTOCOL_VERSION}"
    return None


@dataclass(frozen=True)
class GateView:
    """What the gates grade, derived from one report (never stored beside it)."""

    #: One row per (detector, fault kind, severity) the report swept.
    table: list[dict[str, Any]] = field(default_factory=list)
    #: Per served detector the report scored: false-alarm episodes and engine
    #: hours over the in-era drives, and their ratio (``None`` without engine
    #: time — never 0.0).
    nominal_fp: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Served detectors the report withheld, with the reason (A02.9).
    withheld: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "nominal_fp": self.nominal_fp, "withheld": self.withheld}


def gate_view(report: Mapping[str, Any]) -> GateView:
    """The graded cells of an ``evaluate_detectors`` report.

    Raises :class:`RetiredProtocolError` for any other report, so a grid-era
    table can never be compared against this protocol's by accident.

    - ``recall`` is ``detected / graded`` over the injections the detector
      could be graded on (``withheld`` and ``masked`` windows excluded, as the
      report does); ``None`` when none could.
    - ``per_hour`` is false-alarm episodes per engine-hour summed over the
      drives the report kept out of its cross-era set — the same drives its
      own ``false_alarms`` summary pools — and is computed for every served
      detector whatever its role. (The report's summary leaves an
      ``evidence``-role detector's rate empty because its episodes are not
      alerts; a regression is still a regression.)
    """
    reason = protocol_mismatch(report)
    if reason is not None:
        raise RetiredProtocolError(reason)
    scored = [name for name in report.get("detectors", []) if name in SERVED_DETECTORS]

    table: list[dict[str, Any]] = []
    recall = report.get("recall") or {}
    for detector in scored:
        for kind, by_severity in sorted((recall.get(detector) or {}).items()):
            for severity, block in sorted(by_severity.items(), key=lambda item: float(item[0])):
                graded = int(block.get("graded", 0))
                detected = int(block.get("detected", 0))
                table.append(
                    {
                        "detector": detector,
                        "fault_kind": kind,
                        "severity": float(severity),
                        "injections": int(block.get("injections", 0)),
                        "graded": graded,
                        "detected": detected,
                        "withheld": int(block.get("withheld", 0)),
                        "masked": int(block.get("masked", 0)),
                        "recall": (detected / graded) if graded else None,
                        "median_time_to_detect_seconds": block.get("median_time_to_detect_seconds"),
                    }
                )

    nominal: dict[str, dict[str, Any]] = {}
    drives = [d for d in report.get("drives", []) if not d.get("cross_era")]
    for detector in scored:
        episodes = 0
        hours = 0.0
        counted = 0
        for drive in drives:
            block = (drive.get("detectors") or {}).get(detector)
            if block is None:
                continue
            counted += 1
            episodes += int(block.get("false_alarm_episodes", 0))
            hours += float(drive.get("engine_hours") or 0.0)
        nominal[detector] = {
            "false_alarm_episodes": episodes,
            "engine_hours": hours,
            "drives": counted,
            "per_hour": (episodes / hours) if hours > 0 else None,
        }

    withheld = {
        name: str(reason)
        for name, reason in (report.get("withheld_detectors") or {}).items()
        if name in SERVED_DETECTORS
    }
    return GateView(table=table, nominal_fp=nominal, withheld=withheld)


def _drive_key(report: Mapping[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    return sorted(
        (str(d.get("drive")), tuple(sorted(d.get("sessions", []))))
        for d in report.get("drives", [])
    )


#: Protocol parameters two compared reports must share: they decide which
#: windows are injected, with what, and when an alert counts.
_PARAMETERS = (
    "seed",
    "faults",
    "injections_per_drive",
    "window_seconds",
    "lead_seconds",
    "grace_seconds",
    "persistence",
)


def incomparable_reason(baseline: Mapping[str, Any], current: Mapping[str, Any]) -> str | None:
    """Why two reports cannot be diffed, or ``None``.

    Comparing them anyway would be comparing aggregates of different
    populations: other drives, other injected windows, another gate.
    """
    for side, report in (("baseline", baseline), ("current", current)):
        reason = protocol_mismatch(report)
        if reason is not None:
            return f"{side}: {reason}"
    base_drives = _drive_key(baseline)
    cur_drives = _drive_key(current)
    if base_drives != cur_drives:
        return (
            f"scored different drives ({[d for d, _ in base_drives]} vs "
            f"{[d for d, _ in cur_drives]})"
        )
    base_params = baseline["protocol"]
    cur_params = current["protocol"]
    for key in _PARAMETERS:
        if base_params.get(key) != cur_params.get(key):
            return (
                f"protocol parameter {key} differs "
                f"({base_params.get(key)!r} vs {cur_params.get(key)!r})"
            )
    base_holdout = (baseline.get("holdout") or {}).get("evaluation")
    cur_holdout = (current.get("holdout") or {}).get("evaluation")
    if base_holdout is not None and cur_holdout is not None:
        if sorted(base_holdout) != sorted(cur_holdout):
            return (
                f"different evaluation holdouts ({sorted(base_holdout)} vs {sorted(cur_holdout)})"
            )
    return None


def detector_drives(report: Mapping[str, Any], detector: str) -> tuple[str, ...]:
    """The in-era drives ``detector`` was scored on in ``report``."""
    return tuple(
        sorted(
            str(d.get("drive"))
            for d in report.get("drives", [])
            if not d.get("cross_era") and detector in (d.get("detectors") or {})
        )
    )


def _in_sample_drives(report: Mapping[str, Any], detector: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(d.get("drive"))
            for d in report.get("drives", [])
            if detector in (d.get("in_sample") or {})
        )
    )


def incomparable_detectors(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, str]:
    """Served detectors two otherwise comparable reports scored on different
    drives, and why (A02.3).

    A drive that chose the sequence artifact's alert level is in-sample for
    the sequence detectors and scored for the rest, so one report can grade
    the IF on drives where it does not grade the sequence detector. Its
    numbers are then over another population, and diffing them would compare
    aggregates of different drives. A detector the current report scored on
    *no* drive with none in-sample is not listed: that is ``missing``, which
    regresses.

    A06.6: two reports that scored the sequence detector at different forward
    cadences (``sequence_forward_every_n_ticks``; absent reads as 1) graded
    different windows, so every cadence-bound detector (``sequence``,
    ``sequence_global``, ``fused``) is incomparable between them. The IF and
    the baselines do not depend on the cadence and are still diffed.
    """
    out: dict[str, str] = {}
    base_cadence, cur_cadence = recorded_cadence(baseline), recorded_cadence(current)
    if base_cadence != cur_cadence:
        for detector in sorted(CADENCE_BOUND):
            out[detector] = (
                f"scored at different sequence forward cadences (every {base_cadence} vs "
                f"every {cur_cadence} tick(s)); {CADENCE_INCIDENT}"
            )
    for detector in SERVED_DETECTORS:
        if detector in out:
            continue
        base = detector_drives(baseline, detector)
        cur = detector_drives(current, detector)
        if base == cur:
            continue
        in_sample = sorted(
            set(_in_sample_drives(baseline, detector)) | set(_in_sample_drives(current, detector))
        )
        if not in_sample and (not base or not cur):
            continue
        out[detector] = f"scored on different drives ({list(base)} vs {list(cur)})" + (
            f"; in-sample on {in_sample}" if in_sample else ""
        )
    return out


def _worse(direction: str, base: float, cur: float, tolerance: float) -> bool:
    if direction == "down":
        return cur < base * (1 - tolerance)
    return cur > base * (1 + tolerance)


def diff_reports(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    """Cell-by-cell regression compare of two comparable reports.

    Recall cells are keyed ``(detector, fault_kind, severity)``; false-alarm
    cells ``(detector, "false_alarms", None)``. Statuses:

    - ``compared`` — both graded it; ``regressed`` when any metric moved the
      wrong way by more than ``tolerance`` (relative).
    - ``missing`` — the baseline graded it and the current run did not (no
      row, no graded injection, or no engine time): **regressed**, and named.
      The old grid recorded this as ``removed`` and passed it.
    - ``added`` — only the current run graded it: recorded, never regressed.

    Incomparable reports return ``comparable: False`` with the reason and
    ``regressed: False``: a baseline from other drives is not a regression
    check, and the caller says so rather than failing on it.
    """
    reason = incomparable_reason(baseline, current)
    if reason is not None:
        return {
            "tolerance": tolerance,
            "regressed": False,
            "comparable": False,
            "reason": reason,
            "cells": [],
        }
    base_view = gate_view(baseline)
    cur_view = gate_view(current)

    def recall_cells(view: GateView) -> dict[tuple[str, str, float | None], dict[str, Any]]:
        return {(r["detector"], r["fault_kind"], r["severity"]): r for r in view.table}

    base_cells: dict[tuple[str, str, float | None], dict[str, Any]] = recall_cells(base_view)
    cur_cells = recall_cells(cur_view)
    for name, block in base_view.nominal_fp.items():
        base_cells[(name, "false_alarms", None)] = block
    for name, block in cur_view.nominal_fp.items():
        cur_cells[(name, "false_alarms", None)] = block

    cells: list[dict[str, Any]] = []
    any_regressed = False
    # A02.3: a detector graded on different drives is not diffed; it is named,
    # cell by cell, and the rest of the report is still compared.
    excluded = incomparable_detectors(baseline, current)
    for key in sorted(set(base_cells) | set(cur_cells), key=lambda k: (k[0], k[1], k[2] or 0.0)):
        detector, kind, severity = key
        if detector in excluded:
            cells.append(
                {
                    "detector": detector,
                    "fault_kind": kind,
                    "severity": severity,
                    "status": "incomparable",
                    "regressed": False,
                    "reason": excluded[detector],
                    "metrics": {},
                }
            )
            continue
        metrics_of = (
            ("per_hour",) if kind == "false_alarms" else ("recall", "median_time_to_detect_seconds")
        )
        base_row = base_cells.get(key)
        cur_row = cur_cells.get(key)
        head = {"detector": detector, "fault_kind": kind, "severity": severity}
        primary = metrics_of[0]
        base_graded = base_row is not None and base_row.get(primary) is not None
        cur_graded = cur_row is not None and cur_row.get(primary) is not None
        if not base_graded:
            if cur_graded:
                cells.append({**head, "status": "added", "regressed": False, "metrics": {}})
            continue
        if not cur_graded:
            cells.append({**head, "status": "missing", "regressed": True, "metrics": {}})
            any_regressed = True
            continue
        assert base_row is not None and cur_row is not None
        metrics: dict[str, Any] = {}
        cell_regressed = False
        for metric in metrics_of:
            base_value = base_row.get(metric)
            cur_value = cur_row.get(metric)
            regressed = (
                base_value is not None
                and cur_value is not None
                and _worse(_WORSE[metric], float(base_value), float(cur_value), tolerance)
            )
            metrics[metric] = {"baseline": base_value, "current": cur_value, "regressed": regressed}
            cell_regressed = cell_regressed or regressed
        cells.append(
            {**head, "status": "compared", "regressed": cell_regressed, "metrics": metrics}
        )
        any_regressed = any_regressed or cell_regressed
    return {
        "tolerance": tolerance,
        "regressed": any_regressed,
        "comparable": True,
        "incomparable_detectors": excluded,
        "cells": cells,
    }


def regression_summary(diff: Mapping[str, Any]) -> list[str]:
    """One line per regressed cell, for logs and the CLI."""
    if not diff.get("comparable", True):
        return [f"incomparable: {diff.get('reason')}"]
    lines: list[str] = [
        f"{detector}: not compared, {reason}"
        for detector, reason in sorted((diff.get("incomparable_detectors") or {}).items())
    ]
    for cell in diff.get("cells", []):
        if not cell.get("regressed"):
            continue
        label = f"{cell['detector']} {cell['fault_kind']}" + (
            f" @{cell['severity']:g}" if cell.get("severity") is not None else ""
        )
        if cell["status"] == "missing":
            lines.append(f"{label}: graded in the baseline, not graded now")
            continue
        for metric, values in cell["metrics"].items():
            if values["regressed"]:
                lines.append(
                    f"{label}: {metric} {values['baseline']:.4g} -> {values['current']:.4g}"
                )
    return lines


def sequence_backend_for_artifact(path: Path) -> str | None:
    """The registered sequence selector whose ``model_type`` the artifact's
    manifest names, or ``None`` (no manifest, or an unregistered type)."""
    manifest_path = manifest_path_for(path)
    try:
        model_type = json.loads(manifest_path.read_text()).get("model_type")
    except (OSError, ValueError):
        return None
    for selector, backend in SEQUENCE_BACKENDS.items():
        if backend.model_type == model_type:
            return selector
    return None


def load_sequence_artifact(path: Path) -> TrainedSequenceModel:
    """Load a sequence-detector artifact with the backend its manifest names.

    The retired grid loaded through the *configured* backend, so a
    ``--sequence-model tcn`` retrain under an LSTM config would have read a
    TCN file with the LSTM loader; the evaluation harness only loaded LSTMs.
    The manifest says what the file is.
    """
    selector = sequence_backend_for_artifact(path)
    if selector is None:
        raise ValueError(f"{path}: manifest names no registered sequence model_type")
    return SEQUENCE_BACKENDS[selector].load_model(path)


__all__ = [
    "DEFAULT_TOLERANCE",
    "PROTOCOL_NAME",
    "PROTOCOL_VERSION",
    "SERVED_DETECTORS",
    "GateView",
    "RetiredProtocolError",
    "detector_drives",
    "diff_reports",
    "incomparable_detectors",
    "gate_view",
    "incomparable_reason",
    "load_sequence_artifact",
    "protocol_mismatch",
    "regression_summary",
    "sequence_backend_for_artifact",
]
