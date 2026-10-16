"""The bounded offline evaluation frame (R20): the reproducible report's input.

:func:`build_evaluation_frame` runs the training preprocessing chain over the
sessions, bounded to the channels evaluation reads
(:func:`evaluation_decoded_signals`), and stamps each row's profile verdict
through :mod:`src.analysis.behavior_validation_inputs`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import pandas as pd

from src.analysis.behavior_validation_inputs import ProfileScope, stamp_profile_exclusions
from src.behavior.behavior_eligibility import serving_spec
from src.behavior.behavior_models import BehaviorModels
from src.config import CanaryConfig
from src.preprocessing import drives as _drives
from src.preprocessing.decode import decode_on_read
from src.preprocessing.decoded_reader import read_session_decoded_subset


def evaluation_decoded_signals(models: BehaviorModels) -> set[str]:
    """Binding names the evaluation frame must decode from raw CAN: the
    segmenter's ``decoded:`` mode candidates plus any ``decoded:`` channel a
    model reads (input, output, gate or required context)."""
    from src.behavior.behavior_eligibility import required_channel_groups
    from src.features.feature_transform import segmenter_input_columns

    names: set[str] = set()
    columns: list[str] = list(segmenter_input_columns())
    for model in models.models:
        for group in required_channel_groups(model):
            columns.extend(group)
        columns.append(serving_spec(model).output)
    for column in columns:
        if column.startswith("decoded:"):
            names.add(column.split(":", 1)[1])
    return names


def build_evaluation_frame(
    sessions: Sequence[Path],
    config: CanaryConfig,
    models: BehaviorModels,
    *,
    bindings_path: Path,
    dids_path: Path,
    decode_can: Literal["none", "mode-inputs"] = "mode-inputs",
    profile_scope: ProfileScope = "default_profile",
) -> pd.DataFrame:
    """The training preprocessing chain, bounded to what evaluation reads.

    ``profile_scope`` (B05.3): by default every row is stamped with its
    session's profile verdict (:func:`stamp_profile_exclusions`), so tuning
    captures are refused wherever evidence is counted. ``"every_recording"``
    leaves the frame unstamped, for reproducing numbers an artifact recorded
    before that rule; evidence built on it refuses a capture piece.

    Identical decode → reject gate → resample → derive → segment → prerequisite
    chain as ``build_feature_frame`` (shared functions, not copies), except raw
    CAN frames are read only for the ids of the bindings in
    :func:`evaluation_decoded_signals` — a whole-bus decode of one 42 MB drive
    peaked above 6 GB. The bounded read is
    :func:`src.preprocessing.decoded_reader.read_session_decoded_subset`, the one
    reader that pushes the ids down under every era's channel spelling
    (R04.14; this reader used to push ``0x{id:X}`` only and silently decoded
    nothing from an older session). DBC decoding is not run. Returns the operating frame
    (deviation and prerequisite columns included), one row per resampled
    second, with ``session`` naming the piece.

    ``decode_can="none"`` skips raw CAN entirely: the segmenter's decoded
    fallback candidates did not exist when the July 2026 artifacts were fit,
    so reproducing their persisted held-out numbers needs the historical
    input scope (the idle holdout is 90 rows without it, 114 with it). That
    scope has no ``decoded:`` channel at all (P02.8): neither the loader's
    decode on read nor an old session's stored rows, so a July session reads
    as it did when those numbers were made. ``"mode-inputs"`` reads the
    session with its ``decoded:`` rows decoded on read, as every reader does,
    and decodes its own signals over them.
    """
    from src.decode.bindings import load_bindings
    from src.decode.dids import load_did_map
    from src.features.feature_transform import featurize_wide_frame
    from src.features.session_resampling import drop_fully_stale_rows, resample_long_session
    from src.preprocessing.resampler import resample_config_for

    bindings = load_bindings(bindings_path) if bindings_path.exists() else None
    did_map = load_did_map(dids_path) if dids_path.exists() else None
    wanted = evaluation_decoded_signals(models) if decode_can == "mode-inputs" else set()
    resample_config = resample_config_for(config)
    parts = []
    for session in sessions:
        if decode_can == "none":
            long, subset = read_session_decoded_subset(
                Path(session), bindings=bindings, decoded=wanted, rows="as_recorded"
            )
            long = decode_on_read(long, None)  # drops stored decoded rows, decodes none
        else:
            long, subset = read_session_decoded_subset(
                Path(session), bindings=bindings, decoded=wanted
            )
        parts.append(
            resample_long_session(
                long, subset, None, did_map, resample_config, session_name=Path(session).name
            )
        )
    resampled = drop_fully_stale_rows(pd.concat(parts, ignore_index=True))
    operating = featurize_wide_frame(
        resampled, config, behavior_models=models, did_map=did_map, bindings=bindings
    ).operating_frame
    # Each row's physical drive, as build_feature_frame stamps it, so the
    # evaluator's drive grouping is the trainers' (src.preprocessing.drives).
    stamped = _drives.stamp_physical_drives(operating, [Path(s) for s in sessions])
    if profile_scope == "every_recording":
        return stamped
    return stamp_profile_exclusions(stamped, [Path(s) for s in sessions])
