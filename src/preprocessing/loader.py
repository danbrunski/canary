"""Compatibility facade for the stored-session loading API (R13).

``loader`` used to own all of it -- the Parquet scan, the chunk planning, the
frame contracts, the wide pivot, the IMU decimation and the cached
``resample_and_segment`` pipeline -- and the modules that pipeline selects
(``derived``, ``derived_store``, ``imu_stream``, ``imu_session``,
``session_decode``, ``decode``) imported it back, a seven-module cycle held
together by function-level imports. The implementations now sit one way down
a chain, each module importing only the ones below it:

==========================  ==================================================
``session_contracts``       column and frame contracts (a leaf)
``decode``                  row-local decode; ``imu_decimation``,
                            ``wide_frame``: frame transforms
``session_reader``          raw Parquet scan, chunk plan, filters, parts
``decoded_reader``          read a session, decode it with a chosen catalog
``imu_session``,            IMU sidecar / solver; the framed ``value_number``
``imu_stream``              stream
``derived``,                the bounded reduction, its cache, and the
``session_decode``,         decoded reduction
``derived_store``
``session_pipeline``        ``resample_and_segment``, which picks among them
==========================  ==================================================

This module only re-exports the public names that callers import from
``src.preprocessing.loader``, each the owner's own object (``loader.read_session
is session_reader.read_session``). **No module in ``src/preprocessing`` imports
it** -- ``tests/test_preprocessing_boundaries.py`` fails if one does -- and new
code should import the owner. Patch the owner, not this module: rebinding a
name here changes nothing the readers see.
"""

from __future__ import annotations

from src.preprocessing.session_contracts import (
    INDEX_COLUMNS as INDEX_COLUMNS,
)
from src.preprocessing.session_contracts import (
    KEY_COLUMNS as KEY_COLUMNS,
)
from src.preprocessing.session_contracts import (
    STORED_COLUMNS as STORED_COLUMNS,
)
from src.preprocessing.session_contracts import (
    DecodedRows as DecodedRows,
)
from src.preprocessing.session_contracts import (
    TelemetryValueKind as TelemetryValueKind,
)
from src.preprocessing.session_pipeline import resample_and_segment as resample_and_segment
from src.preprocessing.session_reader import (
    SESSION_PART_ROWS as SESSION_PART_ROWS,
)
from src.preprocessing.session_reader import (
    discover_sessions as discover_sessions,
)
from src.preprocessing.session_reader import (
    filter_rows as filter_rows,
)
from src.preprocessing.session_reader import (
    iter_session_numeric_parts as iter_session_numeric_parts,
)
from src.preprocessing.session_reader import (
    iter_session_parts as iter_session_parts,
)
from src.preprocessing.session_reader import (
    read_session as read_session,
)
from src.preprocessing.session_reader import (
    read_sessions as read_sessions,
)
from src.preprocessing.wide_frame import (
    coerce_numeric_columns as coerce_numeric_columns,
)
from src.preprocessing.wide_frame import (
    non_numeric_columns as non_numeric_columns,
)
from src.preprocessing.wide_frame import (
    numeric_value_columns as numeric_value_columns,
)
from src.preprocessing.wide_frame import (
    to_wide as to_wide,
)
from src.telemetry import channel_key as channel_key
