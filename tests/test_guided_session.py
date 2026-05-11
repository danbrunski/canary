"""Tests for the guided protocol runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.guided_session import (
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


def test_run_step_writes_tag_with_step_label(tmp_path: Path):
    step = Step("idle_cold", "Just sit at idle.", 30)
    inp, out, wt = _FakeInput(), _FakeOutput(), _FakeWriteTag()
    run_step(step, 1, 1, tmp_path, input_fn=inp, output_fn=out, write_tag_fn=wt)
    assert len(wt.calls) == 1
    assert wt.calls[0]["label"] == "idle_cold"
    assert wt.calls[0]["session_dir"] == tmp_path


def test_run_step_uses_event_kind(tmp_path: Path):
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


def test_run_step_prompt_shows_description(tmp_path: Path):
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


def test_run_step_prompt_shows_advisory_duration_when_set(tmp_path: Path):
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


def test_run_step_prompt_omits_duration_for_instant_steps(tmp_path: Path):
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


def test_run_step_header_shows_step_number(tmp_path: Path):
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


def test_run_protocol_writes_all_steps_in_order(tmp_path: Path):
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


def test_run_protocol_returns_records(tmp_path: Path):
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


def test_run_protocol_propagates_write_tag_failure(tmp_path: Path):
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
