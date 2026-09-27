"""One timestamp-to-seconds conversion, and it is unit-safe.

pandas 3 session frames carry ``datetime64[us]``. ``astype("int64") / 1e9``
on such a column reads every interval 1000x short; the behavior evaluator's
8 s persistence window once spanned hours of rows that way, and the same
line sat in the engine-start tracker and the capture-rate evidence
(2026-09-22). ``src.signals.pairing.to_seconds`` pins nanoseconds first, and
this file refuses a second conversion anywhere else.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.signals.pairing import to_seconds

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {ROOT / "src" / "signals" / "pairing.py"}
PATTERN = re.compile(r'astype\(\s*["\']int64["\']\s*\)[^\n]{0,80}(?:/\s*1e9|1_000_000_000)')


@pytest.mark.parametrize("unit", ["ns", "us", "ms", "s"])
def test_to_seconds_is_the_same_in_every_unit(unit: str) -> None:
    stamps = pd.Series(
        pd.to_datetime(["2026-09-22T10:00:00Z", "2026-09-22T10:00:08.25Z"], format="ISO8601")
    )
    stamps = stamps.dt.as_unit(unit) if unit != "s" else stamps.dt.floor("s").dt.as_unit("s")
    seconds = to_seconds(stamps)
    expected = 8.25 if unit != "s" else 8.0
    assert seconds[1] - seconds[0] == pytest.approx(expected, abs=0)


def test_a_microsecond_frame_does_not_compress_intervals() -> None:
    stamps = pd.Series(pd.date_range("2026-09-22T10:00:00Z", periods=3, freq="8s")).dt.as_unit("us")
    seconds = to_seconds(stamps)
    assert np.allclose(np.diff(seconds), 8.0)


def test_nat_is_nan_not_a_sentinel() -> None:
    seconds = to_seconds(pd.Series(["2026-09-22T10:00:00Z", None, "garbage"]))
    assert np.isfinite(seconds[0])
    assert np.isnan(seconds[1]) and np.isnan(seconds[2])


def test_no_other_int64_over_1e9_timestamp_conversion() -> None:
    offenders = []
    for folder in ("src", "scripts", "tools"):
        for path in (ROOT / folder).rglob("*.py"):
            if path in ALLOWED:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                if PATTERN.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, "use src.signals.pairing.to_seconds instead:\n" + "\n".join(offenders)
