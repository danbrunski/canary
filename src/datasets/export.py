"""Dataset capture + export pipeline (Task 6.20).

Turns recorded sessions into a portable, *traceable* dataset: select
channels, optionally resample, include/exclude tag windows, and write
CSV / JSONL / Parquet / a model-ready feature matrix / a ZIP — always
alongside a `dataset_manifest.json` carrying vehicle, config hash, channel
schema, sessions, tags, time range, purpose, and data-quality warnings.

The manifest exists because Risk R12 is real: a training export copied off
the Pi with no provenance is un-auditable. Every export here is
self-describing — the config hash + session list + schema make it
reproducible, and the quality warnings make it honest about what it
contains (high-NaN channels, parse-error-heavy sessions, short captures,
requested-but-absent channels) instead of silently shipping a thin set.

Pure library + a thin CLI (`scripts/export_dataset.py`); the offline
companion (6.36) and a control endpoint can both call ``export_dataset``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import zipfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CanaryConfig
from src.control.sessions import write_session_metadata
from src.preprocessing.loader import read_session, to_wide
from src.preprocessing.resampler import ResampleConfig, resample_wide
from src.session import MANIFEST_FILENAME, build_windows, load_tags, session_paths

log = logging.getLogger(__name__)

__all__ = [
    "DatasetExportResult",
    "DatasetExportSpec",
    "DatasetManifest",
    "build_dataset",
    "config_hash",
    "export_dataset",
    "mark_training_candidate",
    "resolve_sessions",
]

_INDEX_COLUMNS = ("session", "timestamp")
_VALID_FORMATS = {"csv", "jsonl", "parquet", "feature_matrix"}
#: A channel with more than this fraction of NaN over the export window is
#: flagged — not dropped (the operator may still want it), just surfaced.
_HIGH_NAN_FRACTION = 0.5


@dataclass(frozen=True)
class DatasetExportSpec:
    """What to export. ``sessions`` are resolved session directory paths."""

    sessions: list[str]
    channels: list[str] | None = None  # wide column names; None = every channel
    resample_hz: float | None = None  # None = raw last-value wide; else uniform grid
    include_tags: list[str] | None = None  # keep only rows in these tag windows
    exclude_tags: list[str] | None = None  # drop rows in these tag windows
    formats: list[str] = field(default_factory=lambda: ["parquet"])
    purpose: str = ""
    zip: bool = True

    def validate(self) -> None:
        if not self.sessions:
            raise ValueError("at least one session is required")
        bad = set(self.formats) - _VALID_FORMATS
        if bad:
            raise ValueError(f"unknown formats {sorted(bad)}; valid: {sorted(_VALID_FORMATS)}")
        if not self.formats:
            raise ValueError("at least one output format is required")


@dataclass
class DatasetManifest:
    """Self-describing provenance for one export (Risk R12)."""

    created_at: str
    purpose: str
    vehicle: dict[str, Any]
    config_hash: str
    sessions: list[str]
    channel_schema: dict[str, str]  # column -> dtype
    tags: list[str]
    time_range: dict[str, str | None]
    row_count: int
    quality_warnings: list[str]
    spec: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DatasetExportResult:
    out_dir: Path
    files: list[Path]
    manifest: DatasetManifest
    zip_path: Path | None


def config_hash(config: CanaryConfig) -> str:
    """Stable sha256 of the loaded config — the reproducibility handle."""
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _session_window_filter(
    wide: pd.DataFrame,
    session_dir: Path,
    include: list[str] | None,
    exclude: list[str] | None,
) -> tuple[pd.Series, list[str]]:
    """Boolean keep-mask for one session's rows + the tag labels seen.

    No tags file and an ``include`` filter ⇒ keep nothing for that session
    (the operator asked for tagged rows and there are none) — surfaced as a
    quality warning by the caller, not silently treated as "keep all".
    """
    paths = session_paths(session_dir)
    if not paths.tags.exists():
        keep = pd.Series(include is None, index=wide.index)
        return keep, []
    tags = load_tags(paths.tags)
    # POSIX seconds independent of the column's datetime unit (TELEMETRY_SCHEMA
    # stores timestamp[us]; a unit-naive int64 cast would be 1000× off).
    epochs = pd.to_datetime(wide["timestamp"], utc=True).map(pd.Timestamp.timestamp)
    if not tags:
        return pd.Series(include is None, index=wide.index), []
    session_end = float(epochs.max()) + 1.0 if len(epochs) else 0.0
    session_start = float(epochs.min()) if len(epochs) else None
    windows = build_windows(tags, session_end, session_start=session_start)
    labels = sorted({w.label for w in windows})
    keep = pd.Series(include is None, index=wide.index)
    for w in windows:
        in_window = (epochs >= w.start) & (epochs < w.end)
        if include is not None and w.label in include:
            keep |= in_window
        if exclude is not None and w.label in exclude:
            keep &= ~in_window
    return keep, labels


def build_dataset(
    spec: DatasetExportSpec, storage_root: Path, config: CanaryConfig
) -> tuple[pd.DataFrame, DatasetManifest]:
    """Materialize the wide dataset + its manifest. No files written."""
    spec.validate()
    warnings: list[str] = []
    parts: list[pd.DataFrame] = []
    tag_labels: set[str] = set()

    for session_id in spec.sessions:
        session_dir = storage_root / session_id
        if not session_dir.exists():
            warnings.append(f"session not found, skipped: {session_id}")
            continue
        long = read_session(session_dir)
        if long.empty:
            warnings.append(f"session has no telemetry rows: {session_id}")
            continue
        wide = to_wide(long, coerce_numeric=True)
        if spec.resample_hz is not None:
            wide = resample_wide(wide, config=ResampleConfig(hz=spec.resample_hz))
        if spec.include_tags or spec.exclude_tags:
            mask, labels = _session_window_filter(
                wide, session_dir, spec.include_tags, spec.exclude_tags
            )
            tag_labels.update(labels)
            kept = int(mask.sum())
            if spec.include_tags and kept == 0:
                warnings.append(
                    f"no rows matched include_tags in {session_id} "
                    f"(labels present: {sorted(labels) or 'none'})"
                )
            wide = wide.loc[mask]
        _note_parse_errors(session_dir, session_id, warnings)
        parts.append(wide)

    if not parts:
        frame = pd.DataFrame(columns=list(_INDEX_COLUMNS))
    else:
        frame = pd.concat(parts, ignore_index=True)

    if spec.channels is not None:
        requested = set(spec.channels)
        missing = sorted(requested - set(frame.columns))
        if missing:
            warnings.append(f"requested channels absent from data: {missing}")
        keep_cols = [*_INDEX_COLUMNS, *[c for c in spec.channels if c in frame.columns]]
        frame = frame[[c for c in keep_cols if c in frame.columns]]

    _note_channel_quality(frame, warnings)
    manifest = _build_manifest(spec, config, frame, sorted(tag_labels), warnings)
    return frame, manifest


def _note_parse_errors(session_dir: Path, session_id: str, warnings: list[str]) -> None:
    manifest_path = session_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return
    try:
        meta = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    errors = meta.get("parse_errors")
    frames = meta.get("frames_recorded")
    if isinstance(errors, int) and isinstance(frames, int) and frames > 0 and errors > frames:
        warnings.append(f"{session_id}: parse_errors ({errors}) exceed frames_recorded ({frames})")


def _note_channel_quality(frame: pd.DataFrame, warnings: list[str]) -> None:
    if frame.empty:
        warnings.append("export is empty (0 rows)")
        return
    for col in frame.columns:
        if col in _INDEX_COLUMNS:
            continue
        nan_fraction = float(frame[col].isna().mean())
        if nan_fraction > _HIGH_NAN_FRACTION:
            warnings.append(f"channel {col!r} is {nan_fraction:.0%} empty over the window")


def _build_manifest(
    spec: DatasetExportSpec,
    config: CanaryConfig,
    frame: pd.DataFrame,
    tags: list[str],
    warnings: list[str],
) -> DatasetManifest:
    time_range: dict[str, str | None] = {"start": None, "end": None}
    if "timestamp" in frame.columns and not frame.empty:
        ts = pd.to_datetime(frame["timestamp"], utc=True)
        time_range = {"start": ts.min().isoformat(), "end": ts.max().isoformat()}
    schema = {str(c): str(frame[c].dtype) for c in frame.columns if c not in ("session",)}
    return DatasetManifest(
        created_at=datetime.now(UTC).isoformat(),
        purpose=spec.purpose,
        vehicle=config.vehicle.model_dump(mode="json"),
        config_hash=config_hash(config),
        sessions=list(spec.sessions),
        channel_schema=schema,
        tags=tags,
        time_range=time_range,
        row_count=int(len(frame)),
        quality_warnings=warnings,
        spec=asdict(spec),
    )


def export_dataset(
    spec: DatasetExportSpec,
    storage_root: Path,
    config: CanaryConfig,
    out_dir: Path,
    *,
    name: str | None = None,
) -> DatasetExportResult:
    """Build + write the dataset and its manifest under ``out_dir/<name>/``."""
    frame, manifest = build_dataset(spec, storage_root, config)
    name = name or f"dataset-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    target = out_dir / name
    target.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []

    for fmt in spec.formats:
        if fmt == "csv":
            path = target / "dataset.csv"
            frame.to_csv(path, index=False)
        elif fmt == "jsonl":
            path = target / "dataset.jsonl"
            frame.to_json(path, orient="records", lines=True, date_format="iso")
        elif fmt == "parquet":
            path = target / "dataset.parquet"
            frame.to_parquet(path, index=False)
        elif fmt == "feature_matrix":
            path = target / "feature_matrix.parquet"
            _write_feature_matrix(spec, storage_root, config, path, manifest)
        else:  # pragma: no cover - validate() already rejected unknowns
            continue
        files.append(path)

    manifest_path = target / "dataset_manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True, default=str))
    files.append(manifest_path)

    zip_path: Path | None = None
    if spec.zip:
        zip_path = out_dir / f"{name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, arcname=f"{name}/{f.name}")
    return DatasetExportResult(out_dir=target, files=files, manifest=manifest, zip_path=zip_path)


def _write_feature_matrix(
    spec: DatasetExportSpec,
    storage_root: Path,
    config: CanaryConfig,
    path: Path,
    manifest: DatasetManifest,
) -> None:
    """Model-ready matrix via the shared training feature pipeline.

    Imported lazily — ``build_feature_frame`` pulls in the decode/behavior
    stack, and a CSV-only export shouldn't pay that import cost.
    """
    from src.preprocessing.feature_pipeline import (
        build_feature_frame,
        trainable_feature_columns,
    )

    session_dirs = [storage_root / s for s in spec.sessions if (storage_root / s).exists()]
    if not session_dirs:
        pd.DataFrame().to_parquet(path, index=False)
        manifest.quality_warnings.append("feature_matrix: no resolvable sessions")
        return
    features = build_feature_frame(session_dirs, config)
    cols = trainable_feature_columns(features)
    keep = [c for c in ("session", "timestamp", "operating_mode", *cols) if c in features.columns]
    features[keep].to_parquet(path, index=False)


def mark_training_candidate(
    session_dir: Path, *, purpose: str = "", candidate: bool = True
) -> dict[str, Any]:
    """Flag (or un-flag) a session as a training candidate.

    Just session metadata — the dataset builder doesn't *require* it, but the
    dashboard / dataset-health view (6.23) filters on it so the operator can
    curate a training set during a drive ("this one's a keeper").
    """
    return write_session_metadata(
        session_dir,
        {"training_candidate": candidate, "training_purpose": purpose},
    )


def resolve_sessions(storage_root: Path, requested: Sequence[str]) -> list[str]:
    """Validate session ids exist under the root (no path traversal)."""
    out: list[str] = []
    for sid in requested:
        candidate = storage_root / sid
        if candidate.is_dir() and candidate.parent.resolve() == storage_root.resolve():
            out.append(sid)
        else:
            log.warning("ignoring unknown/invalid session id: %s", sid)
    return out
