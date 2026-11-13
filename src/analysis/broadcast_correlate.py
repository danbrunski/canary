"""Correlate broadcast-CAN bytes against Mode 01 PIDs.

The byte↔PID counterpart to ``cross_reference.py`` (Mode 22 DID ↔ Mode 01 PID).
Given a session recorded with ``record.py --obd-port``, for each *probe* — a CAN
id + byte range + endianness — it builds the raw-value time series from
``frames.log``, pairs each Mode 01 PID reading with the nearest raw sample, and
OLS-fits ``raw → PID value``: a high r² names which PID the byte tracks and the
slope/offset give the engineering scaling.

Probes default to the *unpinned* signals in ``bindings.yaml`` (``scale`` is
``None``), so the usual workflow is "for each candidate broadcast byte, which
PID does it track and at what scale/offset?" — exactly what promotes a binding
from hypothesized to confirmed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.analysis.cross_reference import (
    linear_regression,
    load_pid_units,
    load_pids_jsonl,
    pair_by_timestamp,
)
from src.analysis.iso_tp import iter_frames
from src.decode.bindings import Signal, load_bindings
from src.session import FRAMES_FILENAME, PIDS_FILENAME


@dataclass(frozen=True)
class ByteProbe:
    """A broadcast-byte location to correlate against PIDs."""

    can_id: int
    byte_start: int
    byte_length: int = 1
    byte_order: Literal["big", "little"] = "big"
    label: str = ""  # human label, e.g. the bindings signal name
    #: Two's-complement, as the binding's :attr:`Signal.signed`. A signed
    #: byte read unsigned jumps from 255 to 0 as its value crosses zero, and
    #: a straight line fitted across the jump is wrong at both ends.
    signed: bool = False

    @property
    def display(self) -> str:
        if self.byte_length == 1:
            rng = f"byte {self.byte_start}"
        else:
            rng = f"bytes {self.byte_start}-{self.byte_start + self.byte_length - 1}"
        order = ("" if self.byte_order == "big" else " LE") + (" signed" if self.signed else "")
        name = f"  ({self.label})" if self.label else ""
        return f"0x{self.can_id:X} {rng}{order}{name}"

    def raw_from(self, data: bytes) -> int:
        """The probe's integer in ``data`` (long enough for the slice)."""
        return int.from_bytes(
            data[self.byte_start : self.byte_start + self.byte_length],
            self.byte_order,
            signed=self.signed,
        )

    @classmethod
    def from_signal(cls, signal: Signal) -> ByteProbe:
        return cls(
            can_id=signal.can_id,
            byte_start=signal.byte_start,
            byte_length=signal.byte_length,
            byte_order=signal.byte_order,
            label=signal.name,
            signed=signal.signed,
        )


@dataclass(frozen=True)
class PidFit:
    """One OLS fit of a probe's raw value against a PID's decoded value."""

    pid: str
    pid_unit: str
    n_pairs: int
    r_squared: float
    slope: float  # PID_value = raw * slope + offset
    offset: float
    decoded_range: tuple[float, float]


@dataclass(frozen=True)
class BroadcastCorrelation:
    """Per-probe result: raw range plus the PID fits that cleared the threshold."""

    probe: ByteProbe
    n_frames: int
    raw_range: tuple[int, int] | None  # None when the probe matched no frames
    fits: tuple[PidFit, ...]  # sorted by r² descending

    @property
    def best(self) -> PidFit | None:
        return self.fits[0] if self.fits else None


def byte_timeseries(frames_path: Path, probe: ByteProbe) -> list[tuple[float, int]]:
    """Extract ``(timestamp, raw_int)`` samples for ``probe`` from a candump log."""
    end = probe.byte_start + probe.byte_length
    out: list[tuple[float, int]] = []
    for frame in iter_frames(frames_path):
        if frame.can_id != probe.can_id or len(frame.data) < end:
            continue
        out.append((frame.timestamp, probe.raw_from(frame.data)))
    out.sort()
    return out


def correlate_broadcast(
    session_dir: Path,
    probes: Iterable[ByteProbe],
    *,
    min_pairs: int = 30,
    min_r2: float = 0.5,
    tolerance_seconds: float = 0.6,
) -> list[BroadcastCorrelation]:
    """Regress each probe's raw byte value against every numeric Mode 01 PID.

    One pass over ``frames.log`` (probes are dispatched by CAN id). Returns one
    ``BroadcastCorrelation`` per probe, with PID fits sorted by r² and filtered
    to ``r² >= min_r2`` and ``>= min_pairs`` paired samples.
    """
    session_dir = Path(session_dir)
    pids_path = session_dir / PIDS_FILENAME
    frames_path = session_dir / FRAMES_FILENAME
    if not pids_path.exists():
        raise FileNotFoundError(
            f"no {PIDS_FILENAME} in {session_dir} — record the session with `--obd-port`"
        )
    if not frames_path.exists():
        raise FileNotFoundError(f"no {FRAMES_FILENAME} in {session_dir}")

    probe_list = list(probes)
    pid_series = load_pids_jsonl(pids_path)
    pid_units = load_pid_units(pids_path)

    # Single pass over frames; collect raw samples for every probe that wants
    # this CAN id.
    by_id: dict[int, list[ByteProbe]] = defaultdict(list)
    for probe in probe_list:
        by_id[probe.can_id].append(probe)
    raw_samples: dict[ByteProbe, list[tuple[float, int]]] = {probe: [] for probe in probe_list}
    for frame in iter_frames(frames_path):
        wanted = by_id.get(frame.can_id)
        if not wanted:
            continue
        for probe in wanted:
            end = probe.byte_start + probe.byte_length
            if len(frame.data) < end:
                continue
            raw_samples[probe].append((frame.timestamp, probe.raw_from(frame.data)))
    for samples in raw_samples.values():
        samples.sort()

    results: list[BroadcastCorrelation] = []
    for probe in probe_list:
        samples = raw_samples[probe]
        if not samples:
            results.append(BroadcastCorrelation(probe, 0, None, ()))
            continue
        raw_values = [v for _, v in samples]
        raw_range = (min(raw_values), max(raw_values))
        fits: list[PidFit] = []
        for pid, series in pid_series.items():
            # Pair the sparse PID series against the dense raw series, then fit
            # PID_value = slope * raw + offset.
            paired = pair_by_timestamp(series, samples, tolerance_seconds=tolerance_seconds)
            if len(paired) < min_pairs:
                continue
            xy = [(raw, pid_val) for pid_val, raw in paired]
            slope, offset, r2 = linear_regression(xy)
            if r2 < min_r2:
                continue
            decoded = [y for _, y in xy]
            fits.append(
                PidFit(
                    pid=pid,
                    pid_unit=pid_units.get(pid, ""),
                    n_pairs=len(paired),
                    r_squared=r2,
                    slope=slope,
                    offset=offset,
                    decoded_range=(min(decoded), max(decoded)),
                )
            )
        fits.sort(key=lambda fit: -fit.r_squared)
        results.append(BroadcastCorrelation(probe, len(samples), raw_range, tuple(fits)))
    return results


def probes_from_bindings(bindings_path: Path, *, unpinned_only: bool = True) -> list[ByteProbe]:
    """Build probes from ``bindings.yaml`` — by default only signals whose
    ``scale`` is still ``None`` (the ones a cross-reference could pin)."""
    bindings = load_bindings(bindings_path)
    return [
        ByteProbe.from_signal(signal)
        for signal in bindings.signals
        if signal.scale is None or not unpinned_only
    ]
