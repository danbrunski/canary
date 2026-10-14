"""Read a stored session and decode it with a chosen catalog (R13.2).

The read-then-decode services: :func:`read_session_decoded` (the standard
pipeline entry point) and :func:`read_session_decoded_subset` (the bounded
read for a subset of broadcast signals). They compose a stored-row reader
(``session_reader``) with the row-local decode (``decode``), which is why they
live above both: ``decode`` is a pure row transform and must not read
sessions, and the reader must not decide how a caller wants rows decoded.
``decode`` still resolves these two names for callers of its old path.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Literal

import pandas as pd

from src.decode.bindings import Bindings, load_bindings
from src.preprocessing.decode import (
    DEFAULT_BINDINGS_PATH,
    decode_can_rows,
    decode_on_read,
    decoded_subset_plan,
    keep_can_ids,
)
from src.preprocessing.session_reader import read_session


def read_session_decoded(
    session_dir: Path,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    confidence_levels: set[str] | None = None,
) -> pd.DataFrame:
    """The session read with ``bindings_path`` as its decode catalog.

    Convenience for the standard pipeline entry point: the raw rows, and a
    ``decoded`` row for each frame the catalog's (confidence-filtered)
    signals read, decoded here. No stored ``decoded`` row survives (P02.8),
    and neither does the loader's own decode on read, which uses the repo
    catalog: every decoded row comes from ``bindings_path``. If it doesn't
    exist, the frame has no decoded rows — so the pipeline still works in a
    checkout without the bindings file.
    """
    long_frame = read_session(session_dir, decoded="as_recorded")
    long_frame = decode_on_read(long_frame, None).reset_index(drop=True)
    if not bindings_path.exists():
        return long_frame
    bindings = load_bindings(bindings_path)
    return decode_can_rows(long_frame, bindings, confidence_levels=confidence_levels)


def read_session_decoded_subset(
    session_dir: Path,
    *,
    bindings: Bindings | None,
    decoded: Iterable[str],
    rows: Literal["on_read", "as_recorded"] = "on_read",
) -> tuple[pd.DataFrame, Bindings | None]:
    """One bounded read of a session for a *subset* of broadcast signals (R04.14).

    Returns ``(long, subset)``: the session's long frame holding every non-CAN
    row plus only the raw CAN rows whose id one of the ``decoded`` bindings
    reads -- pushed down to parquet over every era's channel spelling
    (:func:`can_channel_spellings`) and matched exactly by parsed id -- and
    ``bindings`` narrowed to those signals (``None`` when there is nothing to
    decode). Decoding is the caller's: ``decode_can_rows(long, subset)`` or
    the resample chain. A whole-bus read of one 42 MB drive peaked above 6 GB,
    which is why both bounded readers (``plausibility.offline.session_long``,
    ``analysis.behavior_validation.build_evaluation_frame``) exist at all.

    ``rows`` is the loader's ``decoded`` mode: the session's ``decoded`` rows
    decoded on read (default), or as recorded for a caller that drops every
    ``decoded`` row it is given and decodes what it keeps itself.
    """
    filters, subset, ids = decoded_subset_plan(bindings, decoded)
    long = read_session(Path(session_dir), filters=filters, decoded=rows)
    return keep_can_ids(long, ids), subset
