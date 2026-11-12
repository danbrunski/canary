"""Cross-reference confirmed bindings against real captured frames (Task 4.D.4).

For every signal in ``data/bindings.yaml`` marked ``confidence: confirmed``,
walk the on-disk sessions in ``data/sessions/`` and find a real frame whose
arbitration ID matches the binding. Decode that frame via the same path the
daemon uses (``decode_raw`` + ``apply_scaling``) and assert the decoded
value is inside the binding's declared ``plausible_min`` / ``plausible_max``.

What this catches:

* off-by-one ``byte_start`` errors (the easy bug — a 16-bit signal shifted
  one byte typically decodes to nonsense magnitudes, falling out of bounds);
* sign / endianness mistakes (``ECM_COOLANT_TEMP`` decoding to −40 °C
  because the "no reading" sentinel was treated as data — caught with this
  exact tool on `decode_bindings.py`);
* anyone editing ``bindings.yaml`` and breaking a previously-pinned signal.

The test is **skipped** when no captured sessions are present (the repo
checkout in CI doesn't carry vehicle data — this is consistent with the
"no test fixtures committed" decision recorded in the implementation plan).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.decode.bindings import Signal, apply_scaling, decode_raw, is_plausible, load_bindings
from src.yaml_load import YamlModelError

SESSIONS_ROOT = Path("data/sessions")
BINDINGS_PATH = Path("data/bindings.yaml")
FRAME_RE = re.compile(r"^\(([\d.]+)\)\s+\S+\s+([0-9A-Fa-f]+)#([0-9A-Fa-f]+)")


def _frame_files() -> list[Path]:
    if not SESSIONS_ROOT.exists():
        return []
    return sorted(SESSIONS_ROOT.glob("*/frames.log"))


def _iter_frames(path: Path, can_id: int) -> Iterator[bytes]:
    """Yield payload bytes for every frame in ``path`` matching ``can_id``."""
    target = f"{can_id:X}"
    with path.open() as handle:
        for line in handle:
            m = FRAME_RE.match(line)
            if not m:
                continue
            if m.group(2).upper() != target:
                continue
            yield bytes.fromhex(m.group(3))


def _first_matching_payload(can_id: int, dlc_min: int) -> bytes | None:
    """Find the first captured frame for ``can_id`` with payload ≥ ``dlc_min`` bytes."""
    for frames_log in _frame_files():
        for payload in _iter_frames(frames_log, can_id):
            if len(payload) >= dlc_min:
                return payload
    return None


def _confirmed_signals() -> list[Signal]:
    if not BINDINGS_PATH.exists():
        return []
    return [s for s in load_bindings(BINDINGS_PATH).signals if s.confidence == "confirmed"]


# ---------------------------------------------------------------------------
# Parametrized: one test per confirmed signal
# ---------------------------------------------------------------------------


_CONFIRMED = _confirmed_signals()


@pytest.mark.skipif(not _frame_files(), reason="no captured frames.log available")
@pytest.mark.skipif(not _CONFIRMED, reason="no confirmed bindings to validate")
@pytest.mark.parametrize("signal", _CONFIRMED, ids=lambda s: s.name)
def test_confirmed_binding_decodes_within_plausible_range(signal: Signal) -> None:
    dlc_min = signal.byte_start + signal.byte_length
    payload = _first_matching_payload(signal.can_id, dlc_min)
    if payload is None:
        pytest.skip(
            f"no captured frame for {signal.name} (0x{signal.can_id:X}, "
            f"≥{dlc_min} bytes) in {SESSIONS_ROOT}"
        )

    raw = decode_raw(signal, payload)
    assert raw is not None, f"{signal.name}: decode_raw returned None on a long-enough payload"

    decoded = apply_scaling(signal, raw)
    if signal.scale is None:
        # No scaling pinned yet — the binding shouldn't be "confirmed" then,
        # but if it ever happens, at least surface the contradiction.
        pytest.fail(
            f"{signal.name}: marked confirmed but has no scale; raw extraction returned {raw}"
        )

    assert decoded is not None
    plausible = is_plausible(signal, decoded)
    assert plausible is not False, (
        f"{signal.name}: decoded value {decoded:.3f} {signal.unit} is outside "
        f"plausible range [{signal.plausible_min}, {signal.plausible_max}] "
        f"on the first captured frame (raw=0x{raw:X}, payload=0x{payload.hex()})"
    )


# ---------------------------------------------------------------------------
# Sanity: at least one confirmed signal must be reachable from the corpus
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _frame_files(), reason="no captured frames.log available")
@pytest.mark.skipif(not _CONFIRMED, reason="no confirmed bindings to validate")
def test_at_least_one_confirmed_signal_observed() -> None:
    """Guard against a future state where every confirmed signal's CAN ID
    has fallen out of the captured corpus — the suite would silently report
    "all skipped" otherwise, and stale bindings could ride along undetected.
    """
    seen = 0
    for signal in _CONFIRMED:
        if _first_matching_payload(signal.can_id, signal.byte_start + signal.byte_length):
            seen += 1
    assert seen > 0, (
        "no confirmed binding was found in the captured corpus — the validation "
        "test will silently pass on staleness; check session retention / "
        "bindings.yaml confidence levels"
    )


def test_superseded_by_must_name_a_real_signal(tmp_path: Path) -> None:
    """A dangling supersede pointer is a rename that was half-done."""
    path = tmp_path / "bindings.yaml"
    path.write_text(
        "schema_version: 1\n"
        "vehicle: {make: Nissan}\n"
        "signals:\n"
        "  - name: OLD_NAME\n"
        "    module: ECM\n"
        "    can_id: 0x2D1\n"
        "    byte_start: 1\n"
        "    byte_length: 1\n"
        "    confidence: hypothesized\n"
        "    superseded_by: NAME_THAT_IS_NOT_HERE\n"
    )

    with pytest.raises(YamlModelError, match="superseded_by names no such signal") as refused:
        load_bindings(path)
    assert str(path) in str(refused.value)
