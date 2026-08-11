"""Whether a capture actually delivered what its profile promised.

A capture profile makes claims — this rate, these channels, this tuple
co-observed. Nothing checked them. The 2026-08-10 fuel-map capture ran for 24
minutes describing itself as "High-rate fueling and timing capture" and
produced **two** usable delivery-error sample-instants; every individual number
along the way looked unremarkable, and the shortfall was only visible by
reconstructing the whole chain afterwards from the Parquet.

So the promise becomes a contract with a verdict, evaluated in one place and
consumed twice:

* **at arm time**, ~20 s in, while the operator can still fix it — the point of
  the exercise is to fail *before* the drive, not after;
* **at capture stop**, persisted beside the data as ``capture_quality.json``,
  because whether the capture was sound is a property of the capture and has to
  survive it. Nothing in the 2026-08-10 session directory recorded the achieved
  rates, the gaps, or even whether request batching was on, so the post-mortem
  could not distinguish "batching was off" from "the budget was binding".

**Per-channel rates are not the contract.** They were all that was ever
reported, and they are exactly what hid this failure: every channel in that
capture answered, none looked dead, and the analysis still had nothing to work
with, because delivery error needs five channels in the *same* poll round and
the scarcest of them set the yield. ``required_tuple`` makes the joint
requirement first-class, and :func:`tuple_rate_bound_hz` reports it.

The bound is a bound, named as one. The exact count of complete tuples needs
the written rows, which the scheduler does not have and which is why the
digest — where the session is already being read — computes the real figure.
Reporting an upper bound as if it were the yield would repeat the original
mistake in a new place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Fraction of the requested rate a channel may fall to before it counts as a
#: shortfall. Loose enough to absorb round scheduling jitter, tight enough that
#: the 0.28-vs-4 Hz case is never a pass.
RATE_TOLERANCE = 0.9

#: A gap longer than this many requested periods is a coverage failure, not a
#: slow rate. They were one number until 2026-08-06, when idle time inside a
#: capture made an on-target 14.93 Hz poll report 11.25 Hz and read as a fault.
STALL_PERIODS = 5.0

#: Floor under the stall threshold. Without it a 20 Hz channel was called
#: stalled after 0.25 s, which is ordinary scheduling jitter — the first live
#: run of this contract flagged every DID in the capture on that basis. A fast
#: channel silent for two seconds is a real coverage gap; a quarter-second is
#: not, at any rate.
MIN_STALL_SECONDS = 2.0

#: Poll tiers that are not part of a capture's channel selection. DTC reads are
#: always eligible (`request_eligible` returns True for them regardless of
#: profile) and are polled on their own slow schedule, so scoring them against
#: the capture's rate reported DTC_STORED/PENDING/PERMANENT as unanswered on
#: the contract's first live run. They are diagnostics, not capture channels.
EXCLUDED_KINDS = frozenset({"dtc"})
EXCLUDED_TIERS = frozenset({"dtc", "on_demand"})

#: Minimum verification window. Below this the sample is too short to separate
#: a slow channel from an unlucky one, and a "pass" would be noise. Returned as
#: ``unverified`` rather than as a pass, because those are different claims.
MIN_VERIFY_SECONDS = 5.0

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ChannelQuality:
    """One channel's side of the contract."""

    channel: str
    kind: str | None
    requested_hz: float | None
    achieved_hz: float | None
    samples: int
    max_gap_seconds: float | None
    #: ``ok`` | ``missing`` | ``shortfall`` | ``stalled``. ``missing`` and
    #: ``shortfall`` demand opposite responses — a channel the ECU never
    #: answers wants removing from the profile, a throttled one wants budget —
    #: so they are never collapsed into a single "bad".
    status: str
    in_required_tuple: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "kind": self.kind,
            "requested_hz": self.requested_hz,
            "achieved_hz": self.achieved_hz,
            "samples": self.samples,
            "max_gap_seconds": self.max_gap_seconds,
            "status": self.status,
            "in_required_tuple": self.in_required_tuple,
        }


@dataclass(frozen=True)
class CaptureQuality:
    """The contract's verdict, and every number behind it."""

    verdict: str
    profile: str
    observed_seconds: float | None
    channels: list[ChannelQuality]
    #: Each entry names the clause that failed and what to do about it.
    failures: list[dict[str, Any]] = field(default_factory=list)
    required_tuple: list[str] = field(default_factory=list)
    #: Upper bound on the rate at which the required tuple can complete: the
    #: slowest member. Exact only when the profile stamps a round's readings
    #: with one timestamp (``round_timestamps``); otherwise genuinely a bound,
    #: since members polled in different rounds may never share an instant.
    tuple_rate_bound_hz: float | None = None
    tuple_bound_is_exact: bool = False
    storage: dict[str, Any] = field(default_factory=dict)
    batching: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "verdict": self.verdict,
            "profile": self.profile,
            "observed_seconds": self.observed_seconds,
            "required_tuple": list(self.required_tuple),
            "tuple_rate_bound_hz": self.tuple_rate_bound_hz,
            "tuple_bound_is_exact": self.tuple_bound_is_exact,
            "failures": list(self.failures),
            "channels": [row.to_dict() for row in self.channels],
            "storage": dict(self.storage),
            "batching": self.batching,
            "notes": list(self.notes),
        }


def tuple_rate_bound_hz(
    rates: dict[str, float | None],
    required_tuple: list[str] | tuple[str, ...],
) -> float | None:
    """Upper bound on how often the whole tuple can be observed together.

    The slowest member, because a tuple cannot complete more often than its
    scarcest channel is sampled. ``None`` when any member never answered — an
    absent member means the tuple's real rate is zero, but "zero because the
    channel is missing" is a different finding from "slow", and the caller
    reports it as a missing-channel failure rather than as a rate.
    """
    if not required_tuple:
        return None
    values: list[float] = []
    for name in required_tuple:
        rate = rates.get(name.upper())
        if rate is None:
            return None
        values.append(float(rate))
    return min(values) if values else None


def _channel_status(
    achieved: float | None,
    requested: float | None,
    max_gap: float | None,
    samples: int,
) -> str:
    if achieved is None or samples == 0:
        return "missing"
    if requested is not None and float(achieved) < float(requested) * RATE_TOLERANCE:
        return "shortfall"
    if max_gap is not None and requested is not None and requested > 0:
        if float(max_gap) > max(STALL_PERIODS / float(requested), MIN_STALL_SECONDS):
            return "stalled"
    return "ok"


def evaluate_capture_quality(  # noqa: PLR0912
    *,
    profile: dict[str, Any],
    scheduler: dict[str, Any],
    storage: dict[str, Any] | None = None,
    requested_rate_hz: float | None = None,
) -> CaptureQuality:
    """Score a running or finished capture against its profile's promises.

    ``profile`` is a ``tuning_profile.snapshot()`` payload and ``scheduler`` a
    ``/status`` ``health.obd_scheduler`` payload, so the same function serves
    the MCP arm-time check (which has only HTTP) and the daemon's stop-time
    sidecar (which has the objects). Plain dicts on purpose: a shared
    evaluator that only one caller could construct arguments for would drift
    back into two implementations.
    """
    name = str(profile.get("name") or "unknown")
    weights = {str(k).upper(): float(v) for k, v in (profile.get("mode01") or {}).items()}
    target = profile.get("target_rate_hz")
    required = [str(item).upper() for item in (profile.get("required_tuple") or [])]
    floor = profile.get("min_achieved_rate_hz")
    round_timestamps = bool(profile.get("round_timestamps", False))

    rates = scheduler.get("achieved_hz_capture") or {}
    samples_by_channel = scheduler.get("capture_samples") or {}
    gaps = scheduler.get("capture_max_gap_seconds") or {}
    observed_seconds = scheduler.get("capture_observed_seconds")

    effective_target = requested_rate_hz if requested_rate_hz is not None else target

    channels: list[ChannelQuality] = []
    achieved_by_channel: dict[str, float | None] = {}
    seen: set[str] = set()
    for request in scheduler.get("requests") or []:
        if not request.get("capture_selected", False):
            continue
        if request.get("kind") in EXCLUDED_KINDS or request.get("poll_tier") in EXCLUDED_TIERS:
            continue
        channel = str(request.get("channel") or "")
        # Two requests can resolve to one DID (a catalogue alias and the
        # profile's own name both map to 0x1209), which listed it twice.
        if channel.upper() in seen:
            continue
        seen.add(channel.upper())
        # The profile's own name for this request, resolved by the scheduler.
        # Falling back to the raw channel keeps older payloads working.
        profile_channel = str(request.get("profile_channel") or channel)
        upper = profile_channel.upper()
        # The weight is part of the promise: under a target rate a 0.25-weight
        # context channel is *asked* for a quarter of it, so holding it to the
        # full target would report a shortfall the profile never claimed.
        if effective_target is not None and upper in weights:
            requested_hz: float | None = float(effective_target) * weights[upper]
        elif effective_target is not None and request.get("kind") == "did":
            requested_hz = float(effective_target)
        else:
            requested_hz = request.get("configured_rate_hz")

        achieved = rates.get(channel)
        samples = int(samples_by_channel.get(channel, 0) or 0)
        max_gap = gaps.get(channel)
        achieved_by_channel[upper] = None if achieved is None else float(achieved)
        channels.append(
            ChannelQuality(
                # Reported under the profile's name, not the transport's:
                # "ECM_INJECTOR_PW_B1" is what the operator selected,
                # "DID_1208" is what they then have to look up.
                channel=profile_channel,
                kind=request.get("kind"),
                requested_hz=requested_hz,
                achieved_hz=achieved,
                samples=samples,
                max_gap_seconds=max_gap,
                status=_channel_status(achieved, requested_hz, max_gap, samples),
                in_required_tuple=upper in required,
            )
        )

    failures: list[dict[str, Any]] = []
    notes: list[str] = []

    unanswered = [row.channel for row in channels if row.status == "missing"]
    if unanswered:
        failures.append(
            {
                "clause": "channel_unanswered",
                "channels": unanswered,
                "detail": (
                    "requested but never answered — these consume a schedule slot every "
                    "round and return nothing. Remove them from this vehicle's profile, or "
                    "confirm the PID/DID is supported before re-arming."
                ),
            }
        )

    shortfalls = [row.channel for row in channels if row.status == "shortfall"]
    if shortfalls:
        failures.append(
            {
                "clause": "rate_shortfall",
                "channels": shortfalls,
                "detail": (
                    "below the profile's requested rate. Demand and budget are separate "
                    "knobs and both bind: raise bus_budget_override, or lower target_rate_hz."
                ),
            }
        )

    stalled = [row.channel for row in channels if row.status == "stalled"]
    if stalled:
        failures.append(
            {
                "clause": "channel_stalled",
                "channels": stalled,
                "detail": (
                    "on-rate overall but silent for a long stretch — a coverage problem "
                    "(window closed, engine off, ECU stopped answering), not a slow rate."
                ),
            }
        )

    missing_tuple = [item for item in required if achieved_by_channel.get(item) is None]
    bound = tuple_rate_bound_hz(achieved_by_channel, required)
    if required and missing_tuple:
        failures.append(
            {
                "clause": "required_tuple_incomplete",
                "channels": missing_tuple,
                "detail": (
                    "this profile's headline analysis needs every required_tuple channel "
                    "observed together; these never answered, so the tuple can never "
                    "complete and the capture cannot answer its own question."
                ),
            }
        )
    elif required and bound is not None and floor is not None and bound < float(floor):
        failures.append(
            {
                "clause": "required_tuple_rate",
                "channels": list(required),
                "observed_hz": bound,
                "required_hz": float(floor),
                "detail": (
                    f"the required tuple can complete at most {bound:.2f} Hz (its slowest "
                    f"member), below the profile's {float(floor):.2f} Hz floor. Per-channel "
                    "rates can pass while this fails — that is what it is here to catch."
                ),
            }
        )

    if floor is not None:
        below = [
            row.channel
            for row in channels
            if row.achieved_hz is not None
            and row.requested_hz is not None
            and row.requested_hz >= float(floor)
            and float(row.achieved_hz) < float(floor)
        ]
        if below:
            failures.append(
                {
                    "clause": "below_rate_floor",
                    "channels": below,
                    "required_hz": float(floor),
                    "detail": "channels asked for at or above the floor did not reach it.",
                }
            )

    storage_payload = dict(storage or {})
    dropped = storage_payload.get("dropped_records") or storage_payload.get("records_dropped")
    if dropped:
        failures.append(
            {
                "clause": "storage_dropped_records",
                "dropped_records": dropped,
                "detail": (
                    "the storage sink dropped rows during this capture — telemetry is missing "
                    "from disk regardless of what the bus delivered. Lower the request rate; "
                    "25 req/s dropped 2491 records on 2026-07-29 with the cause never found."
                ),
            }
        )

    batching = scheduler.get("batching")
    if batching is None:
        notes.append(
            "request batching was off or unreported. Unbatched, one request carries one PID, "
            "so the bus budget binds far sooner than the ECM does."
        )

    if not required:
        notes.append(
            "this profile declares no required_tuple, so only per-channel rates were checked "
            "— which cannot tell whether any joint analysis has usable samples."
        )
    if required and not round_timestamps:
        notes.append(
            "round_timestamps is off, so tuple_rate_bound_hz is a strict upper bound: members "
            "polled in different rounds need not share an instant."
        )

    if observed_seconds is not None and float(observed_seconds) < MIN_VERIFY_SECONDS:
        verdict = "unverified"
        notes.append(
            f"observed for {float(observed_seconds):.1f}s, under the {MIN_VERIFY_SECONDS:g}s "
            "minimum — too short to tell a slow channel from an unlucky one."
        )
    elif not channels:
        verdict = "unverified"
        notes.append("no capture-selected requests were reported, so nothing could be checked.")
    else:
        verdict = "fail" if failures else "pass"

    return CaptureQuality(
        verdict=verdict,
        profile=name,
        observed_seconds=observed_seconds,
        channels=channels,
        failures=failures,
        required_tuple=required,
        tuple_rate_bound_hz=bound,
        tuple_bound_is_exact=bool(required) and round_timestamps,
        storage=storage_payload,
        batching=batching,
        notes=notes,
    )


__all__ = [
    "MIN_VERIFY_SECONDS",
    "RATE_TOLERANCE",
    "SCHEMA_VERSION",
    "STALL_PERIODS",
    "CaptureQuality",
    "ChannelQuality",
    "evaluate_capture_quality",
    "tuple_rate_bound_hz",
]
