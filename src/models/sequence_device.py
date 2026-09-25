"""Torch and device helpers shared by the sequence trainers and calibration.

Split out of :mod:`src.models.sequence_training` (R04.11) so the calibration
module -- which serve imports for its threshold format -- can reach them
without importing the training module. Torch is imported only inside these
functions, never at module level.
"""

from __future__ import annotations

import os
import platform
from typing import Any

from src.models.sequence_dataset import MissingSequenceDependencyError


def torch_training_modules() -> tuple[Any, Any, Any, Any]:
    try:
        import torch
        from torch import nn, optim
        from torch.utils import data
    except ImportError as exc:
        raise MissingSequenceDependencyError(
            "torch is required to train sequence detectors; run scripts/install_deps.py "
            "or install the 'ml' optional dependency"
        ) from exc
    return torch, nn, optim, data


def torch_module() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise MissingSequenceDependencyError(
            "torch is required for sequence-detector checkpoints"
        ) from exc
    return torch


def default_training_device() -> str:
    """``"cuda"`` when a CUDA device is available, otherwise ``"cpu"``.

    The training CLIs (``train_lstm``, ``train_tcn``, ``retrain``,
    ``sequence_bakeoff``) use this as their ``--device`` default so the dev
    laptop's GPU is exercised automatically while the Pi (no CUDA) falls
    through to CPU. Explicit ``--device`` overrides win. Library entry points
    keep ``device="cpu"`` as their hard default — the serve path on the Pi
    must stay deterministic regardless of where the trainer ran.
    """
    try:
        import torch
    except ImportError:
        return "cpu"
    try:
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001 — driver/library issue → CPU is always safe
        return "cpu"


def dataloader_kwargs_for_device(device: str, *, num_workers: int | None = None) -> dict[str, Any]:
    """``num_workers`` + ``pin_memory`` tuned to the training device.

    CUDA uses page-locked host memory so host→device copies can overlap kernels.
    Worker count remains conservative because this dataset owns a full frame
    plus dense arrays; WSL defaults to zero and other CUDA hosts to one. CPU
    stays single-process. An explicit config value always wins.
    """
    if num_workers is None:
        # WSL process workers frequently duplicate this dataset's full pandas
        # frame and dense arrays. Zero is also the safe 16-GB default elsewhere.
        is_wsl = "microsoft" in platform.release().lower()
        num_workers = 0 if is_wsl else (1 if str(device).startswith("cuda") else 0)
    if str(device).startswith("cuda"):
        return {"num_workers": num_workers, "pin_memory": True}
    return {"num_workers": 0, "pin_memory": False}


def configure_torch_threads(thread_count: int | None = None) -> None:
    """Pin torch's intra-op thread pool to ``thread_count`` (default: all CPUs).

    Training CLIs call this at startup so CPU-bound code paths (the FP
    benchmark, threshold calibration, and the CPU fall-back of the trainers
    themselves) saturate every available core instead of the conservative
    half-cores torch picks by default. A no-op when torch is not installed.
    """
    try:
        import torch
    except ImportError:
        return
    if thread_count is None:
        thread_count = os.cpu_count() or 1
    try:
        torch.set_num_threads(int(thread_count))
    except Exception:  # noqa: BLE001 — exotic builds without the OpenMP backend
        return


__all__ = [
    "configure_torch_threads",
    "dataloader_kwargs_for_device",
    "default_training_device",
    "torch_module",
    "torch_training_modules",
]
