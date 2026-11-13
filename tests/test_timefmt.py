"""R24.4: the coercion and epoch helpers reproduce each copy they replaced.

Each ``_old_*`` below is one of the deleted per-file functions, verbatim. A
migrated site is only equivalent if the helper gives the same value for every
input shape the copy accepted, so the matrix is the one the item names: naive,
aware non-UTC, aware UTC, a pandas Timestamp, microseconds present or absent.
For the epoch formatters the comparison is on the *text*, because that is what
the ledgers and sidecars store.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta, timezone

import pandas as pd
import pytest

from src.timefmt import as_utc, assume_utc, format_epoch, format_timestamp, utc_datetime

PLUS_ONE = timezone(timedelta(hours=1))
MINUS_SEVEN = timezone(timedelta(hours=-7))

MOMENTS = [
    datetime(2026, 3, 4, 5, 6, 7),
    datetime(2026, 3, 4, 5, 6, 7, 123456),
    datetime(2026, 3, 4, 5, 6, 7, tzinfo=PLUS_ONE),
    datetime(2026, 3, 4, 5, 6, 7, 123456, tzinfo=MINUS_SEVEN),
    datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC),
    datetime(2026, 3, 4, 5, 6, 7, 999999, tzinfo=UTC),
]


# --- the deleted copies, verbatim ---------------------------------------


def _old_convert(moment: datetime) -> datetime:
    """evaluation_corpus._utc, drives._utc, sessions._as_utc, labelled_events._utc."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _old_keep(moment: datetime) -> datetime:
    """interventions._aware, tune_revisions._aware, bus_freshness._utc, live.py x5."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _old_pandas(stamp: datetime | pd.Timestamp | str) -> datetime:
    """delivery_evidence/compare_evidence/change_evaluation/twin_evidence._aware."""
    value = pd.Timestamp(stamp)
    value = value.tz_localize(UTC) if value.tzinfo is None else value.tz_convert(UTC)
    return value.floor("us").to_pydatetime()


def _old_pandas_ns(seconds: float) -> str:
    """cold_start_inventory, target_phi, target_phi_stream ``_iso``."""
    return pd.Timestamp(seconds, unit="s", tz="UTC").isoformat()


def _old_plain(seconds: float) -> str:
    """plant_validation ``_iso``."""
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def _old_zulu(seconds: float) -> str:
    """plausibility.evaluator and decode_uprev_flash ``_iso``."""
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def _old_committees(seconds: float) -> str:
    """signals.committees ``_iso``."""
    return format_timestamp(datetime.fromtimestamp(seconds, tz=UTC))


# --- coercion -------------------------------------------------------------


@pytest.mark.parametrize("moment", MOMENTS)
def test_as_utc_is_the_convert_copy(moment: datetime) -> None:
    got = as_utc(moment)
    assert got == _old_convert(moment)
    assert got.isoformat() == _old_convert(moment).isoformat()
    assert got.utcoffset() == timedelta(0)


@pytest.mark.parametrize("moment", MOMENTS)
def test_assume_utc_is_the_keep_aware_copy(moment: datetime) -> None:
    got = assume_utc(moment)
    assert got == _old_keep(moment)
    assert got.isoformat() == _old_keep(moment).isoformat()
    if moment.tzinfo is not None:
        assert got is moment, "an aware value is returned untouched, offset and all"


def test_the_two_coercions_differ_only_on_aware_non_utc() -> None:
    """Why there are two functions: the same instant, two spellings."""
    aware = datetime(2026, 3, 4, 5, 6, 7, tzinfo=PLUS_ONE)
    assert as_utc(aware) == assume_utc(aware)
    assert as_utc(aware).isoformat() != assume_utc(aware).isoformat()
    naive = datetime(2026, 3, 4, 5, 6, 7)
    assert as_utc(naive).isoformat() == assume_utc(naive).isoformat()


STAMPS: list[datetime | pd.Timestamp | str] = [
    *MOMENTS,
    pd.Timestamp("2026-03-04T05:06:07"),
    pd.Timestamp("2026-03-04T05:06:07.123456789"),
    pd.Timestamp("2026-03-04T05:06:07+01:00"),
    pd.Timestamp("2026-03-04T05:06:07.5", tz="UTC"),
    "2026-03-04T05:06:07",
    "2026-03-04 05:06:07.250",
    "2026-03-04T05:06:07Z",
    "2026-03-04T05:06:07.123456789+02:00",
]


@pytest.mark.parametrize("stamp", STAMPS, ids=repr)
def test_utc_datetime_is_the_pandas_copy(stamp: datetime | pd.Timestamp | str) -> None:
    got = utc_datetime(stamp)
    assert got == _old_pandas(stamp)
    assert got.isoformat() == _old_pandas(stamp).isoformat()
    assert got.microsecond == _old_pandas(stamp).microsecond
    assert type(got) is datetime


# --- epoch text -----------------------------------------------------------


def _epochs() -> list[float]:
    rng = random.Random(24)
    fixed = [0.0, 5.0, 1.7e9, 1700000000.5, 1708740829.9991355]
    return fixed + [1.7e9 + rng.random() * 1e7 for _ in range(500)]


@pytest.mark.parametrize("seconds", _epochs())
def test_format_epoch_flavors_reproduce_each_copy(seconds: float) -> None:
    assert format_epoch(seconds) == _old_plain(seconds)
    assert format_epoch(seconds, zulu=True) == _old_zulu(seconds)
    assert format_epoch(seconds, zulu=True) == _old_committees(seconds)
    assert format_epoch(seconds, nanoseconds=True) == _old_pandas_ns(seconds)


def test_nanoseconds_flag_is_load_bearing() -> None:
    """The pandas copies keep a float's nanosecond noise; rounding to
    microseconds would change every stored ``acquired_at``."""
    seconds = 1708740829.9991355
    assert format_epoch(seconds, nanoseconds=True) == "2024-02-24T02:13:49.999135494+00:00"
    assert format_epoch(seconds) == "2024-02-24T02:13:49.999135+00:00"
    assert format_epoch(seconds, zulu=True).endswith("Z")
