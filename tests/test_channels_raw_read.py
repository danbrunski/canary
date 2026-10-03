"""P02.2 — ``read_channels`` reads only the rows its specs select.

The filtered read must return exactly what the whole-session read returned,
for every source a spec can name. Two committed slices cover them all: the
ground-truth drive (raw CAN frames, decoded broadcast, GPS, Mode 01) and a
tuning capture (Mode 01, DIDs, memory channels).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import src.analysis.channels as channels
from src.analysis.channels import raw_read_filters, read_channels
from src.paths import DIDS_YAML
from src.preprocessing.loader import read_session

FIXTURES = Path(__file__).parent / "fixtures"
DRIVE = FIXTURES / "ground_truth" / "drive-slice"
CAPTURE = FIXTURES / "lambda_delivery" / "_capture-tuning-2026-09-21T223734"


def _specs(session: Path) -> list[str]:
    """One spec per stored channel, in every form a reader takes."""
    import yaml

    frame = read_session(session)
    pairs = {(str(s), str(c)) for s, c in frame[["source", "channel"]].itertuples(index=False)}
    specs: set[str] = set()
    for source, channel in pairs:
        if source == "obd" and channel.startswith("mem:"):
            specs.add(channel)
        elif source == "obd" and not channel.startswith("DID_"):
            specs.add(f"obd:{channel}")
        elif source in ("gps", "decoded"):
            specs.add(f"{source}:{channel}")
    for entry in yaml.safe_load(DIDS_YAML.read_text()).get("dids") or []:
        if ("obd", f"DID_{int(entry['did']):04X}") in pairs:
            specs.add(f"did:{entry['name']}")
    decoded = sorted(s.split(":", 1)[1] for s in specs if s.startswith("decoded:"))
    specs.update(f"can:{name}" for name in decoded[:3])
    return sorted(specs)


def _unfiltered(
    monkeypatch: pytest.MonkeyPatch, session: Path, specs: list[str], **kwargs: bool
) -> pd.DataFrame:
    with monkeypatch.context() as patch:
        patch.setattr(channels, "_raw_read_filters", lambda parsed, catalogs: None)
        return read_channels(session, specs, **kwargs)


@pytest.mark.parametrize("session", [DRIVE, CAPTURE], ids=["drive", "capture"])
@pytest.mark.parametrize("acquired_time", [False, True])
def test_a_filtered_read_returns_exactly_the_whole_session_read(
    monkeypatch: pytest.MonkeyPatch, session: Path, acquired_time: bool
) -> None:
    specs = _specs(session)
    sources = {spec.split(":", 1)[0] for spec in specs}
    # Every filter branch is exercised by one slice or the other.
    assert sources >= ({"obd", "decoded", "can", "gps"} if session == DRIVE else {"obd", "did"})
    for spec in specs:
        expected = _unfiltered(monkeypatch, session, [spec], acquired_time=acquired_time)
        got = read_channels(session, [spec], acquired_time=acquired_time)
        assert not expected.empty, spec
        pd.testing.assert_frame_equal(
            got.reset_index(drop=True), expected.reset_index(drop=True), check_exact=True
        )
    together_expected = _unfiltered(monkeypatch, session, specs, acquired_time=acquired_time)
    together = read_channels(session, specs, acquired_time=acquired_time)
    pd.testing.assert_frame_equal(
        together.reset_index(drop=True),
        together_expected.reset_index(drop=True),
        check_exact=True,
    )


def test_the_acquisition_clock_has_one_unit_whatever_rows_were_read() -> None:
    """``fillna`` once took the stored unit (µs) when no row carried an
    acquisition stamp and ns when one did, so a narrower read changed the
    dtype. A GPS-only read has no acquisition stamps at all."""
    gps = read_channels(DRIVE, ["gps:speed"], acquired_time=True)
    obd = read_channels(DRIVE, ["obd:RPM"], acquired_time=True)
    assert str(gps["timestamp"].dtype) == str(obd["timestamp"].dtype) == "datetime64[ns, UTC]"


def test_an_unresolvable_spec_adds_no_rows_and_still_fails_on_its_own() -> None:
    """``raw_read_filters`` is the shared read for callers that read once and
    ask spec by spec: one bad name contributes nothing to it."""
    assert raw_read_filters(["did:NOT_A_DID", "obd:RPM"]) == [[("source", "==", "obd")]]
    with pytest.raises(channels.ChannelSpecError):
        read_channels(CAPTURE, ["did:NOT_A_DID"])
