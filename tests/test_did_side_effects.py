"""A DID whose read changes ECU state is never polled (2026-10-10).

DID 0x120B answers with the first flagged diagnostic item's DTC, and the CAN
handler then clears status bit 0 on every item (firmware FUN_0002f748). It was
catalogued as a read. ``DidEntry.side_effect`` marks it, and every path that
turns a DID into a repeated request refuses or skips it: capture profiles,
``collection.obd_dids``, the request plan, the ELM327 list, the sweep and the
logger.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from src.analysis.did_scan import DidSnapshot, Mode22Transport, sweep_dids, sweep_dids_batched
from src.collection.capture_profiles import (
    CaptureProfile,
    CaptureProfileSources,
    CaptureProfileStore,
)
from src.collection.obd_scheduler import (
    CanObdSpec,
    build_poll_requests,
    profile_did_union,
)
from src.config import CanaryConfig, CanBusConfig
from src.decode.dids import (
    known_did_names,
    load_repo_did_map,
    pollable_did_names,
    side_effect_dids,
)
from tools import did_log

REPO_ROOT = Path(__file__).resolve().parents[1]
FIRST_FLAGGED = 0x120B
CAN = CanBusConfig(interface="virtual", channel="test", bitrate=500000)


@pytest.fixture
def config() -> CanaryConfig:
    return CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")


def test_the_catalog_marks_0x120b_as_changing_ecu_state() -> None:
    did_map = load_repo_did_map()
    assert did_map is not None
    entry = next(e for e in did_map.dids if e.did == FIRST_FLAGGED)
    assert entry.side_effect and "bit 0" in entry.side_effect
    assert FIRST_FLAGGED in side_effect_dids()


def test_the_profile_inventory_leaves_it_out_but_the_name_still_resolves() -> None:
    assert "ECM_FIRST_FLAGGED_DTC" in known_did_names()
    assert "ECM_FIRST_FLAGGED_DTC" not in pollable_did_names()


@pytest.mark.parametrize("token", ["ECM_FIRST_FLAGGED_DTC", "120B", "120b"])
def test_a_profile_naming_it_is_refused_at_plan_build(config: CanaryConfig, token: str) -> None:
    profile = CaptureProfile(
        name="dtc-watch",
        mode22=(token,),
        corpus_eligible=False,
        storage_prefix="_capture-dtc-watch",
    )
    with pytest.raises(ValueError, match=r"capture profile 'dtc-watch'.*0x120B"):
        profile_did_union(config.collection, (profile,))


def test_config_obd_dids_naming_it_is_refused(config: CanaryConfig) -> None:
    collection = config.collection.model_copy(update={"obd_dids": ["ECM_FIRST_FLAGGED_DTC"]})
    with pytest.raises(ValueError, match="collection.obd_dids"):
        profile_did_union(collection, ())


def test_the_default_plan_never_carries_it(config: CanaryConfig) -> None:
    """The inventory union used to add every catalogued DID to the plan."""
    spec = CanObdSpec.from_config(config.collection, CAN, catalog=())
    requests = build_poll_requests(None, spec)
    dids = {r.identifier for r in requests if r.kind == "did"}
    assert dids, "the union should still carry the rest of the inventory"
    assert FIRST_FLAGGED not in dids


def test_the_request_builder_refuses_it_from_a_bare_spec() -> None:
    """The recorder's ``dids=`` override bypasses the union."""
    spec = CanObdSpec(can=CAN, poll_rate_hz=1.0, dids=("1201", "120B"))
    with pytest.raises(ValueError, match="Mode 22 poll plan.*0x120B"):
        build_poll_requests(None, spec)


def test_the_profile_store_refuses_to_save_it(tmp_path: Path, config: CanaryConfig) -> None:
    store = CaptureProfileStore(
        CaptureProfileSources(
            tmp_path / "vehicle" / "capture_profiles.yaml",
            tmp_path / "state" / "capture_profile_overrides.yaml",
        ),
        config.collection,
        tmp_path / "missing-behavior-models.yaml",
    )
    profile = CaptureProfile(
        name="dtc-watch",
        mode22=("ECM_AF_SENSOR_V_B1", "ECM_FIRST_FLAGGED_DTC"),
        corpus_eligible=False,
        storage_prefix="_capture-dtc-watch",
    )
    with pytest.raises(ValueError, match="changes ECU state"):
        store.save(profile)
    assert store.load().by_name("dtc-watch") is None


class _Transport(Mode22Transport):
    def __init__(self) -> None:
        self.reads: list[int] = []

    def read_did(self, did: int) -> bytes | None:
        self.reads.append(did)
        return b"\x00\x00"


@pytest.mark.parametrize("batched", [False, True])
def test_sweeps_skip_it_and_say_so(batched: bool) -> None:
    transport = _Transport()
    span = range(0x1208, 0x120E)
    if batched:
        snap = sweep_dids_batched(transport, span, lambda _did: 2)
    else:
        snap = sweep_dids(transport, span)
    assert FIRST_FLAGGED not in transport.reads
    assert FIRST_FLAGGED not in snap.values
    assert "ECM_FIRST_FLAGGED_DTC" in snap.skipped[FIRST_FLAGGED]
    restored = DidSnapshot.from_dict(json.loads(json.dumps(snap.to_dict())))
    assert restored.skipped == snap.skipped


def test_a_snapshot_written_before_skipped_existed_loads_with_none() -> None:
    snap = DidSnapshot.from_dict({"captured_at": 1.0, "values": {"0x1201": "0a"}})
    assert snap.skipped == {}


def test_did_log_never_logs_it_even_from_supplied_widths() -> None:
    transport = _Transport()
    buf = io.StringIO()
    ticks = iter(range(10_000))
    did_log.run(
        transport,
        {0x1201: 2, FIRST_FLAGGED: 2},
        buf,
        duration_seconds=1.0,
        batch=1,
        monotonic=lambda: next(ticks) * 0.1,
        wall_clock=lambda: 1_800_000_000.0,
    )
    assert FIRST_FLAGGED not in transport.reads
    assert 0x1201 in transport.reads
    header = json.loads(buf.getvalue().splitlines()[0])
    assert "0x120B" in header["skipped"]
    assert "0x120B" not in header["widths"]
