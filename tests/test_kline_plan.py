"""K01.5: a capture profile's ``kline:`` list resolves to one module's reads,
against the vehicle's K-line profile, or is refused by name."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from src.collection.kline.plan import KlinePlanError, resolve_module_plan, split_module
from src.collection.kline.profile import KlineProfile, load_kline_profile
from src.decode.dids import load_did_map

PROFILE_PATH = Path("data/nissan-xterra-2008/kline.yaml")
PROFILE = load_kline_profile(PROFILE_PATH)
DIDS = load_did_map(Path("data/dids.yaml"))


def _profile(edit: Any) -> KlineProfile:
    doc = yaml.safe_load(PROFILE_PATH.read_text())
    edit(doc)
    return KlineProfile.model_validate(doc)


def test_split_module_upper_cases_the_module_and_leaves_bare_tokens_alone() -> None:
    assert split_module("abs:1202") == ("ABS", "1202")
    assert split_module(" ABS : ENGINE_SPEED ") == ("ABS", "ENGINE_SPEED")
    assert split_module("0x1201") == (None, "0x1201")


def test_bare_entries_are_the_ecms_local_id_plan_capped_at_its_limit() -> None:
    plan = resolve_module_plan(("0x1201", "ECM_TARGET_EQUIV_RATIO"), PROFILE, DIDS)
    assert (plan.address, plan.label, plan.service) == (0x10, "ECM", "local_id")
    assert plan.local is not None
    assert [c.channel for c in plan.local.channels] == ["1201", "MEM_ECM_TARGET_EQUIV_RATIO"]
    assert plan.tester_present_seconds is None  # never seen answered by this ECM
    same = resolve_module_plan(("ECM:0x1201", "ECM:ECM_TARGET_EQUIV_RATIO"), PROFILE, DIDS)
    assert same == plan


def test_an_ecm_definition_past_the_modules_reply_limit_is_refused_before_sending() -> None:
    """UpRev's longer AC 84 drew ``7F AC 12``; the limit is profile data."""

    def cap(doc: dict[str, Any]) -> None:
        doc["modules"][0x10]["reader"]["max_local_id_reply_bytes"] = 3

    with pytest.raises(KlinePlanError, match=r"at most 3 .*7F AC 12"):
        resolve_module_plan(("0x1201", "0x1219"), _profile(cap), DIDS)


def test_an_abs_plan_reads_each_did_once_with_its_recorded_width() -> None:
    plan = resolve_module_plan(
        ("ABS:ENGINE_SPEED", "ABS:STOP_LAMP_SWITCH", "ABS:OFF_SWITCH", "abs:0x1102"),
        PROFILE,
        DIDS,
    )
    assert (plan.address, plan.label, plan.service) == (0x28, "ABS", "common_id")
    assert [(d.did, d.width) for d in plan.reads] == [(0x1202, 2), (0x1303, 2), (0x1102, 1)]
    assert plan.tester_present_seconds == 1.5
    assert plan.local is None


@pytest.mark.parametrize(
    ("entries", "named"),
    [
        (("ABS:1202", "0x1201"), "2 modules .*one wire"),
        (("TCM:1202",), "no K-line module named 'TCM'"),
        (("33:1202",), "module 33 has no reader"),
        (("ABS:1307",), "0x1307 has no recorded width"),
        (("ABS:YAW_RATE",), "no DID or binding named 'YAW_RATE'"),
        (("0x1117",), "0x1117"),
        ((), "at least one channel"),
    ],
)
def test_what_cannot_be_read_is_refused_by_name(entries: tuple[str, ...], named: str) -> None:
    with pytest.raises(KlinePlanError, match=named):
        resolve_module_plan(entries, PROFILE, DIDS)


def test_a_module_whose_reader_is_the_wrong_service_is_refused() -> None:
    def ecm_by_common_id(doc: dict[str, Any]) -> None:
        doc["modules"][0x10]["reader"] = {"read_service": "common_id", "evidence": "test"}

    with pytest.raises(KlinePlanError, match="only a local_id reader is built for the ECM"):
        resolve_module_plan(("0x1201",), _profile(ecm_by_common_id), DIDS)


def test_each_entry_names_the_channel_its_rows_are_stored_under() -> None:
    """H05.8: capture quality scores entries; rows are stored by channel."""
    ecm = resolve_module_plan((" 0x1201", "ECM:ECM_TARGET_EQUIV_RATIO"), PROFILE, DIDS)
    assert ecm.entry_channels == (
        ("0x1201", "ECM:1201"),
        ("ECM:ECM_TARGET_EQUIV_RATIO", "ECM:MEM_ECM_TARGET_EQUIV_RATIO"),
    )
    abs_plan = resolve_module_plan(
        ("ABS:STOP_LAMP_SWITCH", "ABS:OFF_SWITCH", "ABS:ENGINE_SPEED"), PROFILE, DIDS
    )
    # Two flags of one word are one read and one stored channel, two entries.
    assert abs_plan.entries_by_channel() == {
        "ABS:1303": ("ABS:STOP_LAMP_SWITCH", "ABS:OFF_SWITCH"),
        "ABS:1202": ("ABS:ENGINE_SPEED",),
    }


def test_the_collector_and_the_profile_store_share_one_resolver(tmp_path: Path) -> None:
    from src.collection.kline.plan import KlineCatalog
    from src.collection.kline.profile import KlineNotConfiguredError

    catalog = KlineCatalog(PROFILE_PATH, Path("data/dids.yaml"))
    profile, plan = catalog.resolve(("0x1201",))
    assert profile.ecm_address == plan.address == 0x10
    with pytest.raises(KlinePlanError, match="outlasts P3max"):
        KlineCatalog(PROFILE_PATH, Path("data/dids.yaml"), 6.0).resolve(("0x1201",))
    with pytest.raises(KlineNotConfiguredError):
        KlineCatalog(tmp_path / "absent.yaml", Path("data/dids.yaml")).resolve(("0x1201",))
