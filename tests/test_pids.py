"""Tests for the OBD-II PID registry."""

from src.collection.pids import PIDS_BY_NAME, REQUIRED_PIDS, PidSpec

# PIDs called out by name in FR-01.
EXPECTED_NAMES = {
    "rpm",
    "speed",
    "engine_load",
    "coolant_temp",
    "intake_temp",
    "maf",
    "throttle_pos",
    "short_fuel_trim_1",
    "long_fuel_trim_1",
    "barometric_pressure",
    "run_time",
}

# 0x14-0x1B = 8 O2 sensor voltage PIDs (4 per bank, 2 banks).
EXPECTED_O2_NAMES = {f"o2_b{bank}s{sensor}" for bank in (1, 2) for sensor in (1, 2, 3, 4)}


def test_all_required_pids_present():
    names = {spec.name for spec in REQUIRED_PIDS}
    assert EXPECTED_NAMES <= names
    assert EXPECTED_O2_NAMES <= names


def test_pid_names_unique():
    names = [spec.name for spec in REQUIRED_PIDS]
    assert len(names) == len(set(names))


def test_lookup_dict_matches_tuple():
    assert len(PIDS_BY_NAME) == len(REQUIRED_PIDS)
    for spec in REQUIRED_PIDS:
        assert PIDS_BY_NAME[spec.name] is spec


def test_specs_are_immutable():
    spec = REQUIRED_PIDS[0]
    try:
        spec.unit = "different"  # type: ignore[misc]
    except (AttributeError, Exception):
        pass
    else:
        raise AssertionError("PidSpec should be frozen")


def test_each_spec_has_real_obd_command():
    for spec in REQUIRED_PIDS:
        assert spec.command is not None
        assert spec.command.mode == 1, f"{spec.name} is not a Mode 01 command"
        assert spec.command.pid is not None


def test_known_pid_bytes():
    # Spot-check a few from FR-01's table to catch any wiring mistake.
    cases = {
        "rpm": 0x0C,
        "speed": 0x0D,
        "engine_load": 0x04,
        "coolant_temp": 0x05,
        "intake_temp": 0x0F,
        "maf": 0x10,
        "throttle_pos": 0x11,
        "short_fuel_trim_1": 0x06,
        "long_fuel_trim_1": 0x07,
        "barometric_pressure": 0x33,
        "run_time": 0x1F,
    }
    for name, expected_pid in cases.items():
        assert PIDS_BY_NAME[name].command.pid == expected_pid, name


def test_o2_sensors_cover_0x14_to_0x1b():
    o2_pids = {spec.command.pid for spec in REQUIRED_PIDS if spec.name.startswith("o2_")}
    assert o2_pids == set(range(0x14, 0x1C))


def test_units_populated():
    for spec in REQUIRED_PIDS:
        assert spec.unit, f"{spec.name} has empty unit"


def test_pid_spec_type():
    assert all(isinstance(spec, PidSpec) for spec in REQUIRED_PIDS)
