"""Can the current catalog still produce every column a model was trained on?

D02.5's lesson: *a binding retraction is a feature-spec change*. The July
Isolation Forest went blind because six of its 88 features
(``decoded:ECM_AIRFLOW_PROXY_23D`` / ``decoded:ECM_CALC_LOAD_B`` and their
rolling derivatives) were corrected out of ``data/bindings.yaml`` after it was
trained; on 2026-09-18 a DID relabel had to migrate the model manifests by
hand. Both times nothing refused the edit — the daemon discovered it at serve
time as ``missing_feature_rate: 1.0``.

:mod:`src.models.feature_spec_health` answers the same question *at runtime*,
from what the live pipeline actually emitted. This module answers it
*statically*, from the catalog files alone, so a catalog edit that strands a
tracked model fails the test suite instead of a drive
(``tests/test_manifest_producibility.py``) and a deployed manifest can be
checked on the Pi without a drive (``tools/check_manifest_producibility.py``).

The producible set is not re-derived from naming rules by hand. The captured
namespaces are enumerated from the same catalogs the live source loads
(bindings → ``decoded:``, DBC → ``dbc:``, dids.yaml → ``did:``, the Mode 01
registry → ``obd:``, the GPS record builder → ``gps:``); then one synthetic
frame carrying every one of those columns is pushed through
:func:`src.preprocessing.feature_pipeline.featurize_wide_frame` — the single
wide → model-ready transform both train and serve call. Whatever it emits is
producible, including ``derived:``/``deviation:`` channels and the
``__mean``/``__std``/``__roc`` expansion, so a derived channel whose *input*
was renamed away is caught transitively.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.config import CanaryConfig
from src.telemetry import SOURCE_SEP

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Catalog locations, anchored to the repo root (the live source loads the
#: same files; see ``src.daemon.inference.build_live_feature_source``).
DEFAULT_BINDINGS_PATH = _REPO_ROOT / "data" / "bindings.yaml"
DEFAULT_DIDS_PATH = _REPO_ROOT / "data" / "dids.yaml"
DEFAULT_DBC_DIR = _REPO_ROOT / "data" / "dbc"

#: Namespaces whose channels come straight from a catalog/collector.
CAPTURED_NAMESPACES: tuple[str, ...] = ("decoded", "dbc", "did", "obd", "gps")
#: Namespaces the feature transform computes from the captured ones.
COMPUTED_NAMESPACES: tuple[str, ...] = ("derived", "deviation")

_ROLLING_SUFFIXES: tuple[str, ...] = ("__mean", "__std", "__roc")

#: Where each captured namespace's names come from, for the report.
_CATALOG_OF: dict[str, str] = {
    "decoded": "data/bindings.yaml signals",
    "dbc": "data/dbc/*.dbc signals",
    "did": "data/dids.yaml dids",
    "obd": "the Mode 01 registry (src.collection.obd_registry)",
    "gps": "the GPS record builder (src.collection.gps_collector)",
}

#: Synthetic frame length: comfortably past the rolling window so every
#: rolling derivative has finite values.
_PROBE_ROWS = 120


@dataclass(frozen=True)
class ProducibleCatalog:
    """Every wide feature column the current catalog can produce.

    ``captured`` maps each captured namespace to its base columns
    (``"did" -> {"did:ECM_AIR_CHARGE", ...}``); ``producible`` is the full
    output column set of the feature transform over all of them.
    """

    captured: Mapping[str, frozenset[str]]
    producible: frozenset[str]


def base_feature_name(column: str) -> str:
    """``obd:RPM__mean`` → ``obd:RPM``; a raw column is its own base."""
    for suffix in _ROLLING_SUFFIXES:
        if column.endswith(suffix):
            return column[: -len(suffix)]
    return column


def _namespace(column: str) -> str | None:
    source, sep, _ = column.partition(SOURCE_SEP)
    return source if sep else None


def unproducible_feature_columns(
    feature_columns: Iterable[str], catalog: ProducibleCatalog
) -> dict[str, str]:
    """``{column: reason}`` for every feature column no current source produces.

    Pure: the answer depends only on ``feature_columns`` and ``catalog``.
    Empty means the spec is satisfiable. Order follows ``feature_columns``.
    """
    out: dict[str, str] = {}
    for raw in feature_columns:
        column = str(raw)
        if column in catalog.producible:
            continue
        base = base_feature_name(column)
        namespace = _namespace(base)
        if namespace is not None and namespace in catalog.captured:
            if base not in catalog.captured[namespace]:
                reason = f"no channel {base!r} in {_CATALOG_OF.get(namespace, namespace)}"
            else:
                reason = f"{base!r} is captured but the feature transform does not emit {column!r}"
        elif namespace in COMPUTED_NAMESPACES:
            if base in catalog.producible:
                reason = f"{base!r} is produced but the feature transform does not emit {column!r}"
            else:
                reason = (
                    f"the feature transform does not produce {base!r} from the current "
                    "catalog (a channel it is computed from was renamed or removed, or "
                    "the definition itself changed)"
                )
        else:
            reason = (
                f"namespace {namespace!r} is not modelled by the producibility check; "
                "extend src/models/spec_producibility.py before training on it"
            )
        out[column] = reason
    return out


def manifest_feature_columns(manifest: Mapping[str, Any]) -> list[str]:
    """The ordered feature spec a model manifest was trained on.

    Refuses a manifest without one — "nothing to check" must not read as
    "satisfiable".
    """
    schema = manifest.get("schema")
    columns = schema.get("feature_columns") if isinstance(schema, Mapping) else None
    if not isinstance(columns, list) or not columns:
        raise ValueError("manifest has no schema.feature_columns")
    return [str(column) for column in columns]


def load_manifest_feature_columns(path: Path) -> list[str]:
    """:func:`manifest_feature_columns` for a manifest file."""
    return manifest_feature_columns(json.loads(Path(path).read_text()))


def _gps_channels() -> frozenset[str]:
    """Every ``gps:`` channel the collector can emit, from the record builder."""
    from src.collection.gps_collector import reading_to_records
    from src.collection.gps_nmea import GpsReading

    full_fix = GpsReading(
        has_fix=True,
        speed_kmh=50.0,
        heading_deg=90.0,
        latitude=40.0,
        longitude=-105.0,
        altitude_m=1600.0,
        fix_quality=1,
        num_satellites=12,
        hdop=0.8,
    )
    return frozenset(
        f"{record.source}{SOURCE_SEP}{record.channel}"
        for record in reading_to_records(full_fix, 0.0)
    )


def _obd_channels() -> frozenset[str]:
    from src.collection.obd_registry import (
        MISFIRE_CYLINDER_MID_COUNT,
        MODE01_REGISTRY,
        misfire_count_channel,
    )

    names = {definition.channel for definition in MODE01_REGISTRY.values()}
    names |= {misfire_count_channel(c) for c in range(1, MISFIRE_CYLINDER_MID_COUNT + 1)}
    return frozenset(f"obd{SOURCE_SEP}{name}" for name in names)


def captured_channels(
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    dids_path: Path = DEFAULT_DIDS_PATH,
    dbc_dir: Path = DEFAULT_DBC_DIR,
) -> dict[str, frozenset[str]]:
    """Base columns per captured namespace, from the catalogs on disk.

    A missing catalog file contributes nothing — the live source treats a
    missing bindings/dids file the same way (no decoded/did columns at all).
    """
    from src.decode.bindings import load_bindings
    from src.decode.dbc import load_dbc_dir
    from src.decode.dids import load_did_map

    decoded: frozenset[str] = frozenset()
    if bindings_path.exists():
        decoded = frozenset(f"decoded:{s.name}" for s in load_bindings(bindings_path).signals)
    did: frozenset[str] = frozenset()
    if dids_path.exists():
        did = frozenset(f"did:{entry.name}" for entry in load_did_map(dids_path).dids)
    dbc: frozenset[str] = frozenset()
    database = load_dbc_dir(dbc_dir)
    if database is not None:
        dbc = frozenset(
            f"dbc:{signal.name}" for message in database.messages for signal in message.signals
        )
    return {
        "decoded": decoded,
        "dbc": dbc,
        "did": did,
        "obd": _obd_channels(),
        "gps": _gps_channels(),
    }


def build_producible_catalog(
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    dids_path: Path = DEFAULT_DIDS_PATH,
    dbc_dir: Path = DEFAULT_DBC_DIR,
    behavior_models_path: Path | None = None,
) -> ProducibleCatalog:
    """Enumerate the catalog, then let the real feature transform name the rest.

    ``config`` must be the *profile-scoped* config the model serves under:
    vehicle parameters, AFR calibration and behavior models all decide which
    ``derived:``/``deviation:`` channels exist.
    """
    from src.decode.behavior_models import load_behavior_models
    from src.preprocessing.feature_pipeline import featurize_wide_frame

    captured = captured_channels(bindings_path=bindings_path, dids_path=dids_path, dbc_dir=dbc_dir)
    raw_columns = sorted(set().union(*captured.values()))
    rng = np.random.default_rng(0)
    probe = pd.DataFrame({column: rng.uniform(1.0, 100.0, _PROBE_ROWS) for column in raw_columns})
    probe.insert(
        0, "timestamp", pd.date_range("2026-01-01", periods=_PROBE_ROWS, freq="1s", tz="UTC")
    )
    probe.insert(0, "session", "producibility-probe")

    models_path = behavior_models_path or config.daemon.behavior_models
    behavior_models = load_behavior_models(models_path) if models_path.exists() else None
    features = featurize_wide_frame(probe, config, behavior_models=behavior_models).features
    return ProducibleCatalog(
        captured=captured,
        producible=frozenset(str(column) for column in features.columns),
    )


def summarize(unproducible: Mapping[str, str], feature_columns: Sequence[str]) -> dict[str, Any]:
    """Compact report: counts plus the offending columns grouped by base."""
    by_base: dict[str, list[str]] = {}
    for column in unproducible:
        by_base.setdefault(base_feature_name(column), []).append(column)
    return {
        "expected_count": len(feature_columns),
        "unproducible_count": len(unproducible),
        "unproducible_fraction": round(len(unproducible) / len(feature_columns), 4)
        if feature_columns
        else 0.0,
        "unproducible_by_base": {
            base: {"columns": columns, "reason": unproducible[columns[0]]}
            for base, columns in by_base.items()
        },
    }
