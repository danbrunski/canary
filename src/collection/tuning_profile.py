"""Focused high-rate poll profile for tuning captures.

`engine-tuning-support.md` Phase 1.1. Normal operation polls ~42 requests
against an 8 req/s bus budget, which lands each channel at 0.06–0.5 Hz. That
is fine for anomaly detection over a whole drive and useless for tuning, where
you need enough samples in a single rpm × load cell to say anything: the
2026-07-26 post-flash drive produced 196 `TIMING_ADVANCE` samples over 57
minutes, or roughly 10 per cell.

Tuning mode narrows the rotation to a small set of channels that actually
inform fuelling and timing work, so each one gets a far larger slice of the
same budget. It also suspends the Mode-22 DID windows: the `10 C0` session
blackouts stop Mode 01 entirely for up to 10 s out of every 60, which is
exactly the kind of gap that makes a steady-state hold unusable.

Shaped like `tx_gate` deliberately — process-wide runtime state, no config
churn, visible in `/status`, reversible without a restart. Tuning mode is
something you switch on for a specific capture and off again, not a deployment
posture.

How much this buys depends on the bus budget, which is the real constraint and
is far more conservative than the ECU requires. Measured through `CanObdClient`
on 2026-07-27: 667 channel-reads/s single-PID, 1387 batched — both two orders
of magnitude above the 8 req/s the daemon actually uses.

Narrowing alone is not enough for everything, though. On the 2026-07-27 drive
this profile delivered ~0.33 Hz per channel, which caught **23** throttle>=70%
samples across 40 minutes; UpRev's synchronously-sampled K-line log caught
**537** over the same drive. For WOT work the budget has to rise too.

So CAN is not inherently slower than UpRev's K-line logger. The 8 req/s
default (``BUS_BUDGET_MULTIPLIER`` × ``poll_rate_hz``) is a conservative
heuristic, not a measured limit. Narrowing the channel set is one lever;
raising the budget for a bounded capture is the bigger one; multi-PID batching
multiplies whatever the budget is set to by ~2x.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

#: The Phase 1.1 tuning set. Channel names as they appear on `_PollRequest`.
#:
#: Deliberately vehicle-general rather than trimmed to what this Xterra
#: answers. `O2_WR_LAMBDA_S2` and the long-term trims are known not to produce
#: useful values on the dev vehicle — bank 2 lambda is unsupported and backs
#: off on its own, and LTFT reads a flat 0.0 because this ECU does not learn
#: long-term trim (confirmed independently over K-line). Both stay in the set
#: because that is a fact about one ECU, not about tuning.
DEFAULT_TUNING_CHANNELS: frozenset[str] = frozenset(
    {
        "RPM",
        "ENGINE_LOAD",
        "MAF",
        "TIMING_ADVANCE",
        "THROTTLE_POS",
        "INTAKE_TEMP",
        # Every trim analysis filters on coolant > 70 degC to exclude warm-up,
        # so omitting this makes a tuning capture unfilterable after the fact.
        # It also feeds `vehicle_state`, which reports coolant as null without
        # it -- caught mid-drive on 2026-07-27 with the capture already
        # running.
        "COOLANT_TEMP",
        "O2_WR_LAMBDA_S1",
        "O2_WR_LAMBDA_S2",
        "SHORT_FUEL_TRIM_1",
        "SHORT_FUEL_TRIM_2",
        "LONG_FUEL_TRIM_1",
        "LONG_FUEL_TRIM_2",
        "COMMANDED_EQUIV_RATIO",
        # Validity gates for every trim-based conclusion, not luxuries.
        #
        # EVAPORATIVE_PURGE: purge vapour is unmetered fuel, so the ECU backs
        # off injection and short-term trim reads low. Measured on session
        # 2026-07-26T225058: STFT averaged +5.49% with purge closed and
        # +1.34% with it open, a 4.15-point shift, and purge was active in 48
        # of 140 samples reaching 91% duty. A MAF correction derived without
        # separating those two populations is wrong by roughly half the
        # effect being corrected. UpRev's logger has no purge channel at all,
        # which is a concrete reason to prefer CANary for fuel work.
        #
        # FUEL_STATUS: open vs closed loop. Trim-based correction is only
        # meaningful in closed loop; open-loop (WOT enrichment, cold start)
        # samples must be excluded rather than averaged in.
        "EVAPORATIVE_PURGE",
        "FUEL_STATUS",
    }
)

_lock = threading.Lock()
_enabled = False
_channels: frozenset[str] = DEFAULT_TUNING_CHANNELS
_source = "default"


def is_active() -> bool:
    with _lock:
        return _enabled


def channels() -> frozenset[str]:
    with _lock:
        return _channels


def set_active(
    enabled: bool,
    *,
    channel_names: frozenset[str] | None = None,
    source: str = "control",
) -> bool:
    """Enable/disable tuning mode. Returns the *previous* state.

    ``channel_names`` overrides the default set for this activation only;
    disabling always restores the default so a narrow one-off cannot silently
    persist into the next capture.
    """
    global _enabled, _channels, _source
    with _lock:
        previous = _enabled
        _enabled = bool(enabled)
        _channels = (
            (channel_names or DEFAULT_TUNING_CHANNELS) if enabled else DEFAULT_TUNING_CHANNELS
        )
        _source = source
    if previous != bool(enabled):
        log.warning(
            "tuning mode %s",
            "ENABLED (focused poll set, DID windows suspended)" if enabled else "disabled",
            extra={"tuning_mode": bool(enabled), "source": source},
        )
    return previous


def request_eligible(index: int, request: Any) -> bool:
    """`RequestFilter` restricting the rotation to the tuning set.

    Only consulted while tuning mode is active. DTC / Mode 06 / freeze-frame
    requests are excluded along with everything else: they are slow-tier and
    each one costs a request slot plus its timeout, which is precisely the
    budget we are trying to concentrate.
    """
    del index
    return getattr(request, "channel", None) in channels()


def snapshot(achieved_hz: dict[str, float] | None = None) -> dict[str, Any]:
    """Status-payload view. ``achieved_hz`` records the measured per-channel
    rate, which Phase 1.1 asks to be captured alongside the session."""
    with _lock:
        payload: dict[str, Any] = {
            "active": _enabled,
            "source": _source,
            "channels": sorted(_channels),
        }
    if achieved_hz is not None:
        payload["achieved_hz"] = achieved_hz
    return payload


__all__ = [
    "DEFAULT_TUNING_CHANNELS",
    "channels",
    "is_active",
    "request_eligible",
    "set_active",
    "snapshot",
]
