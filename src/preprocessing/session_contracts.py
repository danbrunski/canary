"""Column and frame contracts shared by the stored-session readers (R13.2).

A leaf: this module imports nothing from ``src.preprocessing``, so a reader, a
frame transform and a cache can each take one constant without importing the
loader (the ``loader`` ↔ ``decode`` / ``derived`` / ``imu_*`` cycle R13
removed). Everything here is a name for a shape the readers agree on; none of
it reads or transforms rows.
"""

from __future__ import annotations

from typing import Literal

#: Index of a wide frame (:func:`~src.preprocessing.wide_frame.to_wide`'s default).
INDEX_COLUMNS = ["session", "timestamp"]

#: Which column of a long frame :func:`~src.preprocessing.wide_frame.to_wide` pivots.
TelemetryValueKind = Literal["value", "value_number", "value_text", "value_bool", "payload"]

#: How a reader gets a session's ``decoded`` rows (``session_reader``'s
#: docstring): ``on_read`` decodes them from the frames with the current
#: catalog, whatever the session stored; ``as_recorded`` returns the rows
#: exactly as written.
DecodedRows = Literal["on_read", "as_recorded"]

#: The stored columns of a telemetry chunk (``ParquetWriter``'s schema), the
#: names ``read_session``'s ``columns`` may ask for.
STORED_COLUMNS = (
    "timestamp",
    "source",
    "channel",
    "value_number",
    "value_text",
    "value_bool",
    "unit",
    "payload",
    "metadata_json",
)

#: Read by every ``read_session``, whatever ``columns`` asks: the sort key.
KEY_COLUMNS = ("timestamp", "source", "channel")

#: The three stored value columns a long frame's ``value`` coalesces.
VALUE_COLUMNS = ("value_number", "value_text", "value_bool")

#: The columns the ``value_number`` chain keeps.
NUMERIC_COLUMNS = ["timestamp", "source", "channel", "value_number"]
