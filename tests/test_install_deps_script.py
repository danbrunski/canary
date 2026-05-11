"""Tests for the CUDA-aware dependency installer."""

from __future__ import annotations

from scripts.install_deps import (
    editable_project_spec,
    parse_cuda_version,
    parse_extras,
    project_extras_for_install,
    select_torch_wheel,
)


def test_parse_cuda_version_from_nvidia_smi_output() -> None:
    text = """
    +-----------------------------------------------------------------------------------------+
    | NVIDIA-SMI 565.65                 Driver Version: 566.07         CUDA Version: 12.7     |
    +-----------------------------------------------------------------------------------------+
    """

    assert parse_cuda_version(text) == (12, 7)


def test_parse_cuda_version_returns_none_when_absent() -> None:
    assert parse_cuda_version("Failed to initialize NVML") is None


def test_select_torch_wheel_uses_highest_supported_cuda_not_newer_than_driver() -> None:
    assert select_torch_wheel("auto", (12, 7)).wheel.name == "cu126"
    assert select_torch_wheel("auto", (12, 8)).wheel.name == "cu128"
    assert select_torch_wheel("auto", (13, 0)).wheel.name == "cu130"


def test_select_torch_wheel_falls_back_to_cpu_without_cuda_detection() -> None:
    selection = select_torch_wheel("auto", None)

    assert selection.wheel.name == "cpu"
    assert "No usable CUDA version" in selection.reason


def test_select_torch_wheel_supports_explicit_override() -> None:
    selection = select_torch_wheel("cu126", None)

    assert selection.wheel.name == "cu126"
    assert "forced" in selection.reason


def test_select_torch_wheel_can_skip_torch() -> None:
    selection = select_torch_wheel("none", (12, 8))

    assert selection.wheel is None
    assert "skipped" in selection.reason


def test_parse_extras_accepts_comma_list_and_none() -> None:
    assert parse_extras("dev,docs") == ["dev", "docs"]
    assert parse_extras("none") == []
    assert parse_extras("") == []


def test_editable_project_spec_formats_extras() -> None:
    assert editable_project_spec(["dev"]) == ".[dev]"
    assert editable_project_spec(["dev", "docs"]) == ".[dev,docs]"
    assert editable_project_spec([]) == "."


def test_project_extras_for_install_skips_ml_extra() -> None:
    assert project_extras_for_install(["dev", "ml"]) == ["dev"]
    assert project_extras_for_install(["ml"]) == []
