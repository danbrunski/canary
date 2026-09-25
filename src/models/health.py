"""Moved: daemon and host health lives in :mod:`src.daemon.health` (R04.8).

It is daemon/system health -- cgroup memory, CPU temperature, power, can0
counters, telemetry queues, collector handoff, capture quality -- and only its
drift functions are model concerns (:mod:`src.models.drift`). Living under
``src/models/`` made every collection or daemon change to it pay the
``ml_training`` test tier.

This name stays importable for the callers that have not moved: it *is* the
new module (``sys.modules`` alias below), so ``import src.models.health as
health`` followed by ``monkeypatch.setattr(health, ...)`` patches the code that
runs. Import from :mod:`src.daemon.health` in new code.
"""

from __future__ import annotations

import sys

from src.daemon import health as _health
from src.daemon.health import _MODE_BLIND_WINDOW as _MODE_BLIND_WINDOW
from src.daemon.health import DEFAULT_CAN0_STATS_PATH as DEFAULT_CAN0_STATS_PATH
from src.daemon.health import CaptureQualityInputs as CaptureQualityInputs
from src.daemon.health import FeatureDriftReport as FeatureDriftReport
from src.daemon.health import HealthMonitor as HealthMonitor
from src.daemon.health import can0_counters as can0_counters
from src.daemon.health import feature_drift as feature_drift
from src.daemon.health import memory_limit_status as memory_limit_status
from src.daemon.health import power_status as power_status
from src.daemon.health import reconstruction_error_drift as reconstruction_error_drift
from src.daemon.health import system_metrics as system_metrics

sys.modules[__name__] = _health
