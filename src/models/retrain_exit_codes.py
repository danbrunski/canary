"""Exit codes ``scripts/retrain.py`` uses that need to stay distinguishable
from daemon-side code (the online-training scheduler, Task 3.2,
online-training-strategy.md) without that code importing the whole retrain
script — ``retrain.py`` pulls in the LSTM/TCN trainer modules (torch) at
import time, which the daemon shouldn't have to eat just to read a constant.

Kept as its own tiny module rather than folded into ``src/models/manifest.py``
or ``training_ledger.py`` — this is process-exit-code vocabulary, not manifest
or ledger schema.
"""

from __future__ import annotations

#: The selected `--since` window had too few sessions to reserve a
#: calibration holdout (Task 3.1) — distinct from a genuine trainer crash
#: (exit 1) so a caller (the scheduler) can log a routine skip instead of an
#: error, and never silently falls back to in-sample calibration for a
#: scheduled run.
WINDOW_TOO_SMALL_FOR_HOLDOUT = 3

#: A06.5 — an alert-role artifact did not show the alert budget (at most 1
#: alert per engine-hour, upper bound, on the evaluation drives) and was set
#: aside instead of published (``src.models.alert_budget``). Distinct from a
#: trainer crash (1): the artifact trained fine and is kept for inspection.
ALERT_BUDGET_REFUSED = 4

__all__ = ["ALERT_BUDGET_REFUSED", "WINDOW_TOO_SMALL_FOR_HOLDOUT"]
