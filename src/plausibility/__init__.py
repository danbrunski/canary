"""Deterministic plausibility checks (A05).

Per-channel range, stuck, freshness and slew checks over *arrived* raw
samples, gated on operating mode, confirmed on repeat, and recorded as
evidence. Three modules, deliberately split by weight:

* :mod:`src.plausibility.limits` — the one resolver from channel to physical
  limits, slew limit, stuck rule, expected sample interval and enabled modes.
  Reads the catalogs (``bindings.yaml``, ``dids.yaml``, the J1979 registry)
  and the vehicle's ``plausibility.yaml``.
* :mod:`src.plausibility.evaluator` — the stdlib-only evaluator and the
  pending/confirmed state machine. This is what runs on the daemon's loop, so
  it imports nothing heavy.
* :mod:`src.plausibility.offline` — pandas: raw samples from a stored session,
  operating modes for them, and fault injection for evaluation.

Nothing is re-exported here, so ``import src.plausibility`` stays free.
"""
