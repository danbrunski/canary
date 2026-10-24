"""Tests for the guided protocol runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.guided_session import (
    PROTOCOL_BINDING,
    PROTOCOL_DRIVE,
    PROTOCOL_NT510,
    PROTOCOL_REV,
    PROTOCOLS,
    Step,
    run_protocol,
    run_step,
)

# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------


def test_protocols_registry_has_rev():
    assert "rev" in PROTOCOLS
    assert PROTOCOLS["rev"] is PROTOCOL_REV


def test_rev_protocol_labels_are_unique():
    """Tag labels need to be unique so build_windows gives clean windows."""
    labels = [s.label for s in PROTOCOL_REV]
    assert len(labels) == len(set(labels))


def test_rev_protocol_has_start_end_pairs_for_blips():
    """Every blip should have both a _start and _end tag."""
    labels = [s.label for s in PROTOCOL_REV]
    for prefix in ("blip_2k", "blip_3k", "blip_4k", "sustain_2500"):
        assert f"{prefix}_start" in labels, f"missing {prefix}_start"
        assert f"{prefix}_end" in labels, f"missing {prefix}_end"


def test_rev_protocol_brackets_engine_state():
    """Should start with engine-off, transition through start, end with off."""
    labels = [s.label for s in PROTOCOL_REV]
    assert labels[0] == "key_on_engine_off"
    assert "engine_start" in labels
    assert labels[-1] == "engine_off_key_on"


def test_rev_protocol_has_idle_recovery_between_blips():
    """Each blip should be followed by an idle_recover_* tag for clean window."""
    labels = [s.label for s in PROTOCOL_REV]
    for i, label in enumerate(labels):
        if label.startswith("blip_") and label.endswith("_end"):
            next_label = labels[i + 1] if i + 1 < len(labels) else ""
            assert (
                next_label.startswith("idle_recover_")
                or next_label.startswith("blip_")
                or next_label.startswith("sustain_")
            ), f"blip end {label} not followed by recovery or another rev"


# ---------------------------------------------------------------------------
# run_step
# ---------------------------------------------------------------------------


class _FakeInput:
    """Records the prompts shown to the user; returns empty string each call."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return ""


class _FakeOutput:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)


class _FakeWriteTag:
    """Stand-in for write_tag that records calls without touching disk."""

    def __init__(self, fixed_t: float = 12345.6) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fixed_t = fixed_t

    def __call__(
        self, session_dir: Path, label: str, *, kind: str = "event", **fields: Any
    ) -> dict[str, Any]:
        record = {"t": self.fixed_t, "kind": kind, "label": label, **fields}
        self.calls.append({"session_dir": session_dir, **record})
        return record


def test_run_step_writes_tag_with_step_label(tmp_path: Path) -> None:
    step = Step("idle_cold", "Just sit at idle.", 30)
    inp, out, wt = _FakeInput(), _FakeOutput(), _FakeWriteTag()
    run_step(step, 1, 1, tmp_path, input_fn=inp, output_fn=out, write_tag_fn=wt)
    assert len(wt.calls) == 1
    assert wt.calls[0]["label"] == "idle_cold"
    assert wt.calls[0]["session_dir"] == tmp_path


def test_run_step_uses_event_kind(tmp_path: Path) -> None:
    step = Step("blip_2k_start", "Blip.", None)
    wt = _FakeWriteTag()
    run_step(
        step,
        1,
        1,
        tmp_path,
        input_fn=_FakeInput(),
        output_fn=_FakeOutput(),
        write_tag_fn=wt,
    )
    assert wt.calls[0]["kind"] == "event"


def test_run_step_prompt_shows_description(tmp_path: Path) -> None:
    step = Step("engine_start", "Crank now.", None)
    inp, out = _FakeInput(), _FakeOutput()
    run_step(
        step,
        5,
        17,
        tmp_path,
        input_fn=inp,
        output_fn=out,
        write_tag_fn=_FakeWriteTag(),
    )
    all_prompts = "\n".join(inp.prompts)
    assert "Crank now." in all_prompts


def test_run_step_prompt_shows_advisory_duration_when_set(tmp_path: Path) -> None:
    step = Step("idle_cold", "Idle.", 60)
    inp = _FakeInput()
    run_step(
        step,
        1,
        1,
        tmp_path,
        input_fn=inp,
        output_fn=_FakeOutput(),
        write_tag_fn=_FakeWriteTag(),
    )
    assert any("60s" in p for p in inp.prompts)


def test_run_step_prompt_omits_duration_for_instant_steps(tmp_path: Path) -> None:
    step = Step("engine_start", "Crank now.", None)
    inp = _FakeInput()
    run_step(
        step,
        1,
        1,
        tmp_path,
        input_fn=inp,
        output_fn=_FakeOutput(),
        write_tag_fn=_FakeWriteTag(),
    )
    # No "Suggested ~Xs" hint should appear when duration is None
    assert not any("Suggested" in p for p in inp.prompts)


def test_run_step_header_shows_step_number(tmp_path: Path) -> None:
    out = _FakeOutput()
    run_step(
        Step("foo", "bar"),
        3,
        17,
        tmp_path,
        input_fn=_FakeInput(),
        output_fn=out,
        write_tag_fn=_FakeWriteTag(),
    )
    all_output = "\n".join(out.lines)
    assert "3/17" in all_output


# ---------------------------------------------------------------------------
# run_protocol
# ---------------------------------------------------------------------------


def test_run_protocol_writes_all_steps_in_order(tmp_path: Path) -> None:
    steps = [
        Step("a", "first"),
        Step("b", "second"),
        Step("c", "third"),
    ]
    wt = _FakeWriteTag()
    records = run_protocol(
        steps,
        tmp_path,
        input_fn=_FakeInput(),
        output_fn=_FakeOutput(),
        write_tag_fn=wt,
    )
    assert [r["label"] for r in records] == ["a", "b", "c"]
    assert [c["label"] for c in wt.calls] == ["a", "b", "c"]


def test_run_protocol_returns_records(tmp_path: Path) -> None:
    wt = _FakeWriteTag(fixed_t=999.5)
    records = run_protocol(
        [Step("x", "y")],
        tmp_path,
        input_fn=_FakeInput(),
        output_fn=_FakeOutput(),
        write_tag_fn=wt,
    )
    assert records[0]["t"] == 999.5
    assert records[0]["label"] == "x"


def test_run_protocol_propagates_write_tag_failure(tmp_path: Path) -> None:
    """If write_tag raises (e.g. session went away), the runner should bubble."""

    def angry_write(*a: Any, **k: Any) -> dict[str, Any]:
        raise FileNotFoundError("tags.jsonl gone")

    with pytest.raises(FileNotFoundError, match="tags.jsonl gone"):
        run_protocol(
            [Step("x", "y")],
            tmp_path,
            input_fn=_FakeInput(),
            output_fn=_FakeOutput(),
            write_tag_fn=angry_write,
        )


# ---------------------------------------------------------------------------
# nt510 protocol + skip support
# ---------------------------------------------------------------------------


def test_protocols_registry_has_nt510():
    assert "nt510" in PROTOCOLS
    assert PROTOCOLS["nt510"] is PROTOCOL_NT510


def test_nt510_protocol_labels_are_unique():
    labels = [s.label for s in PROTOCOL_NT510]
    assert len(labels) == len(set(labels))


def test_nt510_protocol_has_start_end_pairs():
    labels = set(s.label for s in PROTOCOL_NT510)
    for start, end in [
        ("nt510_module_scan_start", "nt510_module_scan_end"),
        ("nt510_abs_livedata_start", "nt510_abs_livedata_end"),
        ("nt510_fan_test_on", "nt510_fan_test_off"),
        ("nt510_idle_air_up", "nt510_idle_air_restore"),
        ("nt510_evap_purge_on", "nt510_evap_purge_off"),
        ("nt510_power_balance_start", "nt510_power_balance_end"),
        ("nt510_fuel_pump_cut", "nt510_fuel_pump_restore"),
        ("nt510_abs_actuator_start", "nt510_abs_actuator_end"),
    ]:
        assert start in labels, f"missing {start}"
        assert end in labels, f"missing {end}"


class _ScriptedInput:
    """Returns queued responses in order; falls back to '' once exhausted."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._responses.pop(0) if self._responses else ""


def test_run_step_skips_on_s_input(tmp_path: Path) -> None:
    wt = _FakeWriteTag()
    record = run_step(
        Step("nt510_fan_test_on", "Fan on."),
        1,
        1,
        tmp_path,
        input_fn=_ScriptedInput(["s"]),
        output_fn=_FakeOutput(),
        write_tag_fn=wt,
    )
    assert record is None
    assert wt.calls == []


def test_run_step_skips_on_skip_word_case_insensitive(tmp_path: Path) -> None:
    wt = _FakeWriteTag()
    record = run_step(
        Step("nt510_evap_purge_on", "Purge on."),
        1,
        1,
        tmp_path,
        input_fn=_ScriptedInput(["  SKIP  "]),
        output_fn=_FakeOutput(),
        write_tag_fn=wt,
    )
    assert record is None
    assert wt.calls == []


def test_run_step_tags_normally_on_empty_input(tmp_path: Path) -> None:
    wt = _FakeWriteTag()
    record = run_step(
        Step("idle_settle", "Idle."),
        1,
        1,
        tmp_path,
        input_fn=_ScriptedInput([""]),
        output_fn=_FakeOutput(),
        write_tag_fn=wt,
    )
    assert record is not None
    assert len(wt.calls) == 1


def test_run_protocol_skips_steps(tmp_path: Path) -> None:
    steps = [Step("a", "first"), Step("b", "second"), Step("c", "third")]
    wt = _FakeWriteTag()
    records = run_protocol(
        steps,
        tmp_path,
        input_fn=_ScriptedInput(["", "s", ""]),
        output_fn=_FakeOutput(),
        write_tag_fn=wt,
    )
    assert [r["label"] for r in records] == ["a", "c"]
    assert [c["label"] for c in wt.calls] == ["a", "c"]


def test_protocols_registry_has_drive():
    assert "drive" in PROTOCOLS
    assert PROTOCOLS["drive"] is PROTOCOL_DRIVE


def test_drive_protocol_labels_are_unique():
    labels = [s.label for s in PROTOCOL_DRIVE]
    assert len(labels) == len(set(labels))


def test_drive_protocol_covers_key_phases():
    labels = set(s.label for s in PROTOCOL_DRIVE)
    for expected in (
        "cold_start",
        "idle_warm",
        "highway_cruise",
        "coasting_dfco",
        "engine_off_key_on",
    ):
        assert expected in labels, f"missing {expected}"


# ---------------------------------------------------------------------------
# binding protocol
# ---------------------------------------------------------------------------


def test_protocols_registry_has_binding():
    assert "binding" in PROTOCOLS
    assert PROTOCOLS["binding"] is PROTOCOL_BINDING


def test_binding_protocol_labels_are_unique():
    labels = [s.label for s in PROTOCOL_BINDING]
    assert len(labels) == len(set(labels))


def test_binding_protocol_brackets_engine_state():
    """Starts key-on/engine-off, ends with engine-off so build_windows has clean anchors."""
    labels = [s.label for s in PROTOCOL_BINDING]
    assert labels[0] == "key_on_engine_off"
    assert "engine_start" in labels
    assert labels[-1] == "engine_off_key_on"


def test_binding_protocol_isolates_brake_before_motion():
    """Brake steps must come before any motion step so the brake byte sees a
    stationary baseline with no vehicle-speed-correlated noise."""
    labels = [s.label for s in PROTOCOL_BINDING]
    brake_release_idx = labels.index("brake_release")
    pulling_away_idx = labels.index("pulling_away")
    assert brake_release_idx < pulling_away_idx


def test_binding_protocol_isolates_hvac_before_motion():
    """HVAC sweep must come before motion so 0x551:1 (HVAC candidate) gets a
    stationary differential window — otherwise speed-correlated bytes confound it."""
    labels = [s.label for s in PROTOCOL_BINDING]
    hvac_baseline_idx = labels.index("hvac_off_baseline")
    pulling_away_idx = labels.index("pulling_away")
    assert hvac_baseline_idx < pulling_away_idx


def test_binding_protocol_covers_targeted_actions():
    """Each unbound-byte candidate from the corpus report needs a tagged action."""
    labels = set(s.label for s in PROTOCOL_BINDING)
    for expected in (
        # brake (0x285:7 candidate)
        "idle_park_neutral_brake",
        "brake_release",
        "brake_press_pump",
        # HVAC (0x551:1 candidate)
        "hvac_off_baseline",
        "hvac_fan_low",
        "hvac_fan_high",
        "hvac_ac_on",
        # motion / speed (0x2DE, 0x2A5, 0x794 candidates)
        "steady_25_flat",
        "steady_40",
        "coast_dfco",
        # steering / lateral (any byte that tracks yaw)
        "right_turn_sharp",
        "left_turn_sharp",
    ):
        assert expected in labels, f"missing {expected}"
