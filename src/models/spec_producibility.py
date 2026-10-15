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
:func:`src.features.feature_transform.featurize_wide_frame` — the single
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
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from src.columns import base_channel
from src.config import CanaryConfig
from src.paths import BINDINGS_YAML, DBC_DIR, DIDS_YAML
from src.telemetry import SOURCE_SEP

if TYPE_CHECKING:
    from src.behavior.behavior_models import BehaviorModels

#: Catalog locations, anchored to the repo root (the live source loads the
#: same files; see ``src.daemon.inference.build_live_feature_source``).
#: Re-exported from ``src.paths`` (R01.2).
DEFAULT_BINDINGS_PATH = BINDINGS_YAML
DEFAULT_DIDS_PATH = DIDS_YAML
DEFAULT_DBC_DIR = DBC_DIR

#: Namespaces whose channels come straight from a catalog/collector.
#: External sensors never train or score a detector (operator decision,
#: docs/roadmap/tasks/H00-hat-integration-README.md), so ``imu`` and ``sensor``
#: are deliberately absent, as is ``kline`` (ECU data, but not trainable until
#: the operator decides: it exists only while a K-line session runs).
#: ``gps`` is still listed only because the tracked IF and LSTM manifests were
#: trained with ``gps:speed``; it leaves with the retrain that drops it (H01.1).
CAPTURED_NAMESPACES: tuple[str, ...] = ("decoded", "dbc", "did", "obd", "gps")
#: Namespaces the feature transform computes from the captured ones.
COMPUTED_NAMESPACES: tuple[str, ...] = ("derived", "deviation")

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
        base = base_channel(column)
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


def manifest_behavior_fingerprints(manifest: Mapping[str, Any]) -> dict[str, str] | None:
    """``schema.behavior_model_fingerprints`` as recorded at training, or ``None``.

    ``None`` means the manifest predates the record (B03.4) — *unknown*
    provenance, which :func:`behavior_fingerprint_provenance` reports as
    such. It is never collapsed to "no consumed columns" (``{}``).
    """
    schema = manifest.get("schema")
    recorded = schema.get("behavior_model_fingerprints") if isinstance(schema, Mapping) else None
    if recorded is None:
        return None
    if not isinstance(recorded, Mapping):
        raise ValueError("manifest schema.behavior_model_fingerprints must be a mapping")
    return {str(name): str(value) for name, value in recorded.items()}


#: Verdicts of :func:`behavior_fingerprint_provenance`, one per consumed model.
BEHAVIOR_PROVENANCE_MATCH = "match"
BEHAVIOR_PROVENANCE_MISMATCH = "mismatch"
BEHAVIOR_PROVENANCE_UNKNOWN = "unknown"
BEHAVIOR_PROVENANCE_ABSENT = "absent"


def behavior_fingerprint_provenance(
    feature_columns: Iterable[str],
    recorded: Mapping[str, str] | None,
    behavior_models: BehaviorModels | None,
) -> dict[str, dict[str, Any]]:
    """Per consumed behavior model: does the artifact that produced the column still serve? (B03.4)

    ``recorded`` is the manifest's ``behavior_model_fingerprints`` (see
    :func:`manifest_behavior_fingerprints`); ``behavior_models`` is the set
    that would produce the column today. One entry per behavior model a
    ``deviation:*`` feature column names, keyed by model name:

    * ``match`` — the recorded artifact fingerprint equals the current one.
    * ``mismatch`` — the model was refit since training: the column's
      meaning changed under this detector. Flagged, never silently fed.
    * ``unknown`` — the manifest recorded no fingerprint for it (predates
      B03.4). Reported as unknown provenance: not a pass, not a failure.
    * ``absent`` — no current behavior model of that name; the column is
      also unproducible, which :func:`unproducible_feature_columns` reports.

    Empty when no feature column consumes a behavior model.
    """
    from src.behavior.behavior_decoder import deviation_model_name
    from src.behavior.behavior_models import artifact_fingerprint

    consumed = sorted(
        {
            name
            for column in feature_columns
            if (name := deviation_model_name(str(column))) is not None
        }
    )
    current = (
        {model.name: artifact_fingerprint(model) for model in behavior_models.models}
        if behavior_models is not None
        else {}
    )
    out: dict[str, dict[str, Any]] = {}
    for name in consumed:
        was = recorded.get(name) if recorded is not None else None
        now = current.get(name)
        if now is None:
            status = BEHAVIOR_PROVENANCE_ABSENT
            reason = f"no behavior model {name!r} is loaded; its deviation column is unproducible"
        elif was is None:
            status = BEHAVIOR_PROVENANCE_UNKNOWN
            reason = (
                f"manifest records no artifact fingerprint for behavior model {name!r} "
                "(trained before B03.4): unknown whether the serving artifact is the one "
                "it was trained against"
            )
        elif was == now:
            status = BEHAVIOR_PROVENANCE_MATCH
            reason = f"behavior model {name!r} is the artifact this manifest was trained against"
        else:
            status = BEHAVIOR_PROVENANCE_MISMATCH
            reason = (
                f"behavior model {name!r} was refit since this manifest was trained "
                f"(recorded {was[:12]}…, serving {now[:12]}…): its deviation column no "
                "longer means what the detector learned — retrain the detector "
                "(D02.5/D03.25) or restore the recorded artifact"
            )
        out[name] = {"status": status, "recorded": was, "current": now, "reason": reason}
    return out


def behavior_fingerprint_mismatches(provenance: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Names of the behavior models whose provenance is ``mismatch``."""
    return sorted(
        name
        for name, entry in provenance.items()
        if entry.get("status") == BEHAVIOR_PROVENANCE_MISMATCH
    )


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
    from src.behavior.behavior_models import load_behavior_models
    from src.features.feature_transform import featurize_wide_frame

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
    from src.decode.dids import load_did_map

    # The probe carries every did: column of this catalog, so it describes them.
    did_map = load_did_map(dids_path) if dids_path.exists() else None
    features = featurize_wide_frame(
        probe, config, behavior_models=behavior_models, did_map=did_map
    ).features
    return ProducibleCatalog(
        captured=captured,
        producible=frozenset(str(column) for column in features.columns),
    )


def behavior_feature_conflicts(
    feature_columns: Iterable[str], behavior_models: BehaviorModels | None
) -> dict[str, str]:
    """``{column: reason}`` for consumed ``deviation:*`` columns whose meaning is at risk (B03.4).

    Producibility alone cannot see this: a demoted or quarantined behavior
    model still *produces* its column (so an existing detector is never
    starved of it), but the policy withholding ``learned_feature`` says the
    residual is not one a new model would train on — the consuming detector
    needs a retrain coordinated with D02.5/D03.25 before the behavior model
    is refit or retired. A missing column is reported by
    :func:`unproducible_feature_columns` instead, never here.
    """
    from src.behavior.behavior_decoder import withheld_learned_feature_columns

    return withheld_learned_feature_columns(
        (str(column) for column in feature_columns), behavior_models
    )


def summarize(unproducible: Mapping[str, str], feature_columns: Sequence[str]) -> dict[str, Any]:
    """Compact report: counts plus the offending columns grouped by base."""
    by_base: dict[str, list[str]] = {}
    for column in unproducible:
        by_base.setdefault(base_channel(column), []).append(column)
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
