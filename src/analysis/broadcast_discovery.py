"""Find which broadcast byte carries which signal, from stored frame bytes.

The discovery counterpart to :mod:`src.analysis.channels`. That reader
deliberately refuses unbound payloads — its contract is engineering units, and
an unbound byte range has none. This module is where the binding comes *from*:
it sweeps every plausible byte slice of every broadcast id in a session and
asks which reference channel each slice tracks.

Why this exists as a parquet sweep rather than reuse
:mod:`src.analysis.broadcast_correlate`: that tool reads ``frames.log`` +
``pids.jsonl`` from ``record.py`` sessions and regresses against Mode 01 PIDs
only. Daemon captures are parquet, and since the all-DID tuning profile they
carry Mode 22 DIDs at ~15 Hz in the *same* file as the raw frames — far better
reference material than 1 Hz PIDs, and the reason 25 of 29 observed ids can be
attacked at all.

**Correlation alone does not establish a binding.** On a single drive nearly
every engine signal correlates with RPM, so r² ranks candidates but cannot
confirm them. The discriminator is *scale stability*: fit the same probe on
disjoint sub-windows and require the slope to hold. A byte that really is the
signal keeps its scale; a byte that merely co-varies drifts. ``slope_drift_pct``
carries that, and :func:`credible_fits` refuses to return a fit without it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from src.analysis.channels import read_channels
from src.analysis.compare import STABLE_DRIFT_PCT, ScaleVerdict, regress, scale_stability

#: A probe must clear this to be worth a human's attention at all.
MIN_R_SQUARED = 0.90

#: Fewer pairs than this and neither r² nor the drift figure means anything.
#: Higher than :data:`src.analysis.compare.MIN_PAIRS` because a sweep tests
#: hundreds of probes against every reference, so a threshold that is merely
#: adequate for one deliberate comparison lets chance matches through here.
MIN_PAIRS = 200


@dataclass(frozen=True)
class Probe:
    """One candidate byte slice of one broadcast id."""

    can_id: int
    byte_start: int
    byte_length: int
    byte_order: Literal["big", "little"]

    @property
    def display(self) -> str:
        rng = (
            f"byte {self.byte_start}"
            if self.byte_length == 1
            else f"bytes {self.byte_start}-{self.byte_start + self.byte_length - 1}"
        )
        order = "" if self.byte_length == 1 or self.byte_order == "big" else " LE"
        return f"0x{self.can_id:03X} {rng}{order}"


@dataclass(frozen=True)
class ProbeFit:
    """A probe regressed against one reference channel.

    ``slope``/``offset`` map raw counts to the reference's units:
    ``reference_value = raw * slope + offset``.
    """

    probe: Probe
    reference: str
    n_pairs: int
    r_squared: float
    slope: float
    offset: float
    #: Slope spread across :data:`STABILITY_WINDOWS` disjoint sub-windows, as a
    #: percentage of the mean slope. ``None`` when the session could not be
    #: split at all.
    slope_drift_pct: float | None
    #: Whether that spread means the scale held, wandered, or could not be
    #: judged from this session. Read this rather than thresholding
    #: ``slope_drift_pct`` yourself — a large spread on a slow signal is a
    #: statement about the session, not about the binding.
    scale_verdict: ScaleVerdict

    @property
    def stable(self) -> bool:
        return self.scale_verdict == "stable"

    def describe(self) -> str:
        drift = "n/a" if self.slope_drift_pct is None else f"{self.slope_drift_pct:.2f}%"
        return (
            f"{self.probe.display} -> {self.reference}: "
            f"r2={self.r_squared:.4f} n={self.n_pairs} "
            f"scale={self.slope:.6g} offset={self.offset:.6g} "
            f"drift={drift} [{self.scale_verdict}]"
        )


def candidate_probes(payload_length: int, *, max_byte_length: int = 2) -> list[Probe]:
    """Every byte slice of a frame of ``payload_length`` bytes.

    Two-byte slices are swept in both orders; one-byte slices only in ``big``,
    since endianness is meaningless for them and emitting both would double the
    multiple-comparison burden for nothing.
    """
    probes: list[Probe] = []
    for length in range(1, max_byte_length + 1):
        for start in range(0, payload_length - length + 1):
            orders: tuple[Literal["big", "little"], ...] = (
                ("big",) if length == 1 else ("big", "little")
            )
            for order in orders:
                probes.append(
                    Probe(
                        can_id=0,
                        byte_start=start,
                        byte_length=length,
                        byte_order=order,
                    )
                )
    return probes


def _raw_values(payloads: np.ndarray, probe: Probe) -> np.ndarray:
    """Vectorised big/little-endian unsigned decode of one byte slice."""
    window = payloads[:, probe.byte_start : probe.byte_start + probe.byte_length]
    if probe.byte_length == 1:
        return window[:, 0].astype(np.float64)
    indices = list(range(probe.byte_length))
    if probe.byte_order == "little":
        indices.reverse()
    out = np.zeros(len(window), dtype=np.float64)
    for index in indices:  # most-significant byte first, whichever end that is
        out = out * 256.0 + window[:, index].astype(np.float64)
    return out


def _payload_matrix(frame: pd.DataFrame, can_id: int) -> tuple[pd.Series, np.ndarray] | None:
    """Timestamps and a fixed-width byte matrix for one broadcast id.

    Frames whose length differs from the id's modal length are dropped rather
    than zero-padded: a short frame is a different message, and padding it
    silently invents byte values that were never on the wire.
    """
    channel = f"0x{can_id:03X}"
    rows = frame[(frame["source"] == "can") & (frame["channel"].str.upper() == channel.upper())]
    rows = rows[rows["payload"].map(lambda p: isinstance(p, (bytes, bytearray)))]
    if rows.empty:
        return None
    lengths = rows["payload"].map(len)
    modal = int(lengths.mode().iloc[0])
    rows = rows[lengths == modal]
    if rows.empty:
        return None
    matrix = np.frombuffer(b"".join(bytes(p) for p in rows["payload"]), dtype=np.uint8)
    return rows["timestamp"].reset_index(drop=True), matrix.reshape(len(rows), modal)


def sweep_session(
    session: Path | pd.DataFrame,
    references: Sequence[str],
    *,
    can_ids: Sequence[int] | None = None,
    grid: str = "200ms",
    max_byte_length: int = 2,
    min_pairs: int = MIN_PAIRS,
    bindings_path: Path = Path("data/bindings.yaml"),
    dids_path: Path = Path("data/dids.yaml"),
) -> list[ProbeFit]:
    """Regress every byte slice of every broadcast id against every reference.

    ``references`` are :mod:`src.analysis.channels` specs, so they arrive in
    engineering units and the returned ``slope``/``offset`` are directly the
    scaling a binding needs.

    Both sides are averaged onto a shared ``grid`` before fitting. Broadcast
    frames run ~100 Hz against DIDs at ~15 Hz, so pairing raw nearest-neighbour
    would weight the fit by whichever side happened to sample first; binning
    puts them on one clock explicitly.

    Results are returned unfiltered and sorted by r² — screening is
    :func:`credible_fits`'s job, kept separate so a caller can inspect the
    near-misses that ranking alone would hide.
    """
    frame = session if isinstance(session, pd.DataFrame) else _load_session(session)
    reference_frame = read_channels(
        frame, list(references), bindings_path=bindings_path, dids_path=dids_path
    )

    binned_references: dict[str, pd.Series] = {}
    for spec, part in reference_frame.groupby("spec"):
        series = pd.Series(
            part["value"].to_numpy(dtype=float),
            index=pd.to_datetime(part["timestamp"], utc=True),
        )
        binned = series.resample(grid).mean().dropna()
        if len(binned) >= min_pairs:
            binned_references[str(spec)] = binned

    ids = list(can_ids) if can_ids is not None else _observed_can_ids(frame)
    fits: list[ProbeFit] = []
    for can_id in ids:
        loaded = _payload_matrix(frame, can_id)
        if loaded is None:
            continue
        timestamps, payloads = loaded
        index = pd.to_datetime(timestamps, utc=True)
        for template in candidate_probes(payloads.shape[1], max_byte_length=max_byte_length):
            probe = Probe(
                can_id=can_id,
                byte_start=template.byte_start,
                byte_length=template.byte_length,
                byte_order=template.byte_order,
            )
            raw_series = pd.Series(_raw_values(payloads, probe), index=index)
            binned_raw = raw_series.resample(grid).mean().dropna()
            if len(binned_raw) < min_pairs:
                continue
            for spec, reference in binned_references.items():
                joined = pd.concat(
                    {"raw": binned_raw, "ref": reference}, axis=1, join="inner"
                ).dropna()
                if len(joined) < min_pairs:
                    continue
                raw_values = joined["raw"].to_numpy(dtype=float)
                ref_values = joined["ref"].to_numpy(dtype=float)
                fit = regress(raw_values, ref_values)
                if fit is None:
                    continue
                drift_pct, verdict = scale_stability(raw_values, ref_values)
                fits.append(
                    ProbeFit(
                        probe=probe,
                        reference=spec,
                        n_pairs=len(joined),
                        r_squared=fit.r_squared,
                        slope=fit.slope,
                        offset=fit.offset,
                        slope_drift_pct=drift_pct,
                        scale_verdict=verdict,
                    )
                )
    return sorted(fits, key=lambda fit: fit.r_squared, reverse=True)


def credible_fits(
    fits: Sequence[ProbeFit],
    *,
    min_r_squared: float = MIN_R_SQUARED,
    include_unresolved: bool = False,
) -> list[ProbeFit]:
    """The fits worth acting on, best reference per probe.

    A fit must clear r² *and* hold its scale across sub-windows. Dropping the
    stability test would readmit every byte that merely co-varies with RPM over
    a single drive, which is most of them.

    ``include_unresolved`` widens this to probes whose scale this session could
    not judge. Those are candidates for a *targeted* capture, never for
    promotion straight to ``confirmed`` — which is why they are excluded by
    default rather than merged into the same list with a flag to inspect.
    """
    allowed: tuple[ScaleVerdict, ...] = (
        ("stable", "unresolved") if include_unresolved else ("stable",)
    )
    best: dict[Probe, ProbeFit] = {}
    for fit in fits:
        if fit.r_squared < min_r_squared or fit.scale_verdict not in allowed:
            continue
        incumbent = best.get(fit.probe)
        if incumbent is None or fit.r_squared > incumbent.r_squared:
            best[fit.probe] = fit
    return sorted(best.values(), key=lambda fit: fit.r_squared, reverse=True)


def _observed_can_ids(frame: pd.DataFrame) -> list[int]:
    channels = frame.loc[frame["source"] == "can", "channel"].dropna().unique()
    ids: list[int] = []
    for channel in channels:
        try:
            ids.append(int(str(channel), 16))
        except ValueError:
            continue
    return sorted(ids)


def _load_session(session: Path) -> pd.DataFrame:
    import duckdb

    pattern = str(session / "telemetry-*.parquet")
    return duckdb.connect().execute(f"SELECT * FROM read_parquet('{pattern}')").df()


__all__ = [
    "MIN_PAIRS",
    "MIN_R_SQUARED",
    "STABLE_DRIFT_PCT",
    "Probe",
    "ProbeFit",
    "candidate_probes",
    "credible_fits",
    "sweep_session",
]
