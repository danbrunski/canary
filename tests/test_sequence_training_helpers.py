"""Unit tests for the small training helpers in ``src.models.sequence_training``
that don't fit the trainer's larger end-to-end fixtures."""

from __future__ import annotations

from src.models.sequence_training import (
    dataloader_kwargs_for_device,
    default_training_device,
)


def test_dataloader_kwargs_for_device_cuda_opts_in_pin_memory_and_workers() -> None:
    """CUDA training overlaps host→device copies with kernel time; that needs
    pinned host memory and at least one prefetch worker, so the helper opts in."""
    kwargs = dataloader_kwargs_for_device("cuda")
    assert kwargs == {"num_workers": 2, "pin_memory": True}
    # ``cuda:0`` (explicit index) is still CUDA.
    assert dataloader_kwargs_for_device("cuda:0") == {"num_workers": 2, "pin_memory": True}


def test_dataloader_kwargs_for_device_cpu_stays_conservative() -> None:
    """CPU has no copy to overlap; the extra worker processes only add IPC
    noise. Stay on the conservative Pi-friendly defaults."""
    assert dataloader_kwargs_for_device("cpu") == {"num_workers": 0, "pin_memory": False}
    # Unknown device strings fall back to CPU defaults (safer than guessing).
    assert dataloader_kwargs_for_device("mps") == {"num_workers": 0, "pin_memory": False}


def test_default_training_device_returns_cpu_or_cuda() -> None:
    """The helper just asks torch; result depends on the test host's GPU. The
    invariant is that it returns one of the two strings, never raises, and
    never silently picks something unknown."""
    assert default_training_device() in {"cpu", "cuda"}
