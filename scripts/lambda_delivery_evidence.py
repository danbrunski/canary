#!/usr/bin/env python3
"""Known-answer evidence for the lambda-delivery declaration (B06.1/B06.2).

Reads stored sessions on the laptop and reports, per session, the checks the
declaration in ``data/<profile>/lambda_delivery.yaml`` rests on. Nothing is
fitted, published or written except the ``--out`` JSON.

* RAM final target vs Mode 01 PID 0x44, per-sample on the acquisition clock
  (``target_source_agreement``), split by target regime.
* Fuel-cut coincidence: every RAM ``_SENTINEL`` read against the nearest
  PID 0x44 read (0.0 = cut) and PID 0x24 read (lean rail).
* Target convention judged away from stoich only (``check_target_convention``)
  against the verified lambda sensor, plus a negative control that must fail.
* Post-fuel-cut recovery durations (what the declared holdoff rests on).
* Acquisition separation between the target and measured reads.
* The per-sample state counts of ``delivery_samples`` for each measured source.

Usage::

    .venv/bin/python scripts/lambda_delivery_evidence.py \\
        --declaration data/nissan-xterra-2008/lambda_delivery.yaml \\
        --out /tmp/lambda-evidence.json \\
        ~/canary-data/sessions-0921c/_capture-tuning-2026-09-21T232153
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.afr import AfrCalibration, load_afr_calibration  # noqa: E402
from src.analysis.channels import read_channels  # noqa: E402
from src.analysis.derived import FUEL_PROPERTIES  # noqa: E402
from src.analysis.lambda_delivery import (  # noqa: E402
    DriveInputs,
    LambdaDeliveryDeclaration,
    check_target_convention,
    delay_bracket,
    delivery_samples,
    estimate_transport_delay,
    load_lambda_delivery,
    per_pair_bracket,
    per_pair_delay_support,
    steady_neighbours,
    target_source_agreement,
)
from src.analysis.noise_floor import load_noise_floor  # noqa: E402
from src.analysis.pairing import PairingPolicy, match_ordered, to_seconds  # noqa: E402
from src.paths import DIDS_YAML  # noqa: E402
from src.preprocessing.loader import read_session  # noqa: E402


def _long(session: Path | pd.DataFrame, spec: str, **kw: Any) -> pd.DataFrame:
    try:
        return read_channels(session, [spec], acquired_time=True, **kw)
    except Exception:  # an unresolvable spec is an absent channel here
        return pd.DataFrame(columns=["timestamp", "spec", "value", "unit"])


def _times_values(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    if frame.empty:
        return np.array([]), np.array([])
    ordered = frame.sort_values("timestamp", kind="stable")
    return (
        np.asarray(to_seconds(pd.to_datetime(ordered["timestamp"], utc=True)), dtype=float),
        pd.to_numeric(ordered["value"], errors="coerce").to_numpy(dtype=float),
    )


def fuel_cut_coincidence(
    session: Path | pd.DataFrame,
    declaration: LambdaDeliveryDeclaration,
    *,
    max_separation_s: float,
) -> dict[str, Any]:
    """Each sentinel read vs the nearest PID 0x44 and PID 0x24 read."""
    ram = declaration.commanded("ram_final_target")
    pid = declaration.commanded("mode01_pid_0x44")
    sensor = declaration.measured("ecm_lambda_pid_0x24")
    assert ram.sentinel_spec is not None
    s_t, _ = _times_values(_long(session, ram.sentinel_spec))
    out: dict[str, Any] = {"sentinel_reads": int(len(s_t))}
    policy = PairingPolicy.within(pd.Timedelta(seconds=max_separation_s), reference="a")
    for label, spec, test in (
        ("pid_0x44_zero", pid.spec, lambda v: v == 0.0),
        ("lambda_at_rail", sensor.spec, lambda v: v >= (sensor.rail_high or np.inf)),
    ):
        o_t, o_v = _times_values(_long(session, spec))
        if not len(s_t) or not len(o_t):
            out[label] = {"paired": 0, "agree": 0}
            continue
        m = match_ordered(s_t, o_t, policy)
        agree = int(np.sum(test(o_v[m.index_b])))
        out[label] = {"paired": m.n_pairs, "agree": agree}
    # The reverse: PID 0x44 cut reads that the RAM word did NOT mark as cut.
    p_t, p_v = _times_values(_long(session, pid.spec))
    r_t, r_v = _times_values(_long(session, ram.spec))
    zero = p_t[p_v == 0.0]
    if len(zero) and len(r_t):
        m = match_ordered(zero, r_t, policy)
        out["pid_zero_paired_with_ram_number"] = {
            "pid_zero_reads": int(len(zero)),
            "paired_with_numeric_target": m.n_pairs,
        }
    return out


def post_cut_recovery(
    session: Path | pd.DataFrame,
    declaration: LambdaDeliveryDeclaration,
    *,
    within_pct: float = 3.0,
    settle_s: float = 2.0,
    episode_gap_s: float = 3.0,
    horizon_s: float = 30.0,
) -> dict[str, Any]:
    """Seconds from each cut episode's last sentinel until measured lambda
    *settles* within ``within_pct`` of the RAM target's lambda.

    Settled means every sensor read for the next ``settle_s`` stays inside
    the band: the sensor swings from its lean rail through stoich on the way
    to the post-cut rich recovery, so the first in-band read is not recovery.
    """
    ram = declaration.commanded("ram_final_target")
    sensor = declaration.measured("ecm_lambda_pid_0x24")
    assert ram.sentinel_spec is not None
    s_t, _ = _times_values(_long(session, ram.sentinel_spec))
    r_t, r_v = _times_values(_long(session, ram.spec))
    m_t, m_v = _times_values(_long(session, sensor.spec))
    if not len(s_t) or not len(m_t) or not len(r_t):
        return {"episodes": 0}
    ends = [
        s_t[i] for i in range(len(s_t)) if i == len(s_t) - 1 or s_t[i + 1] - s_t[i] > episode_gap_s
    ]
    durations: list[float] = []
    unresolved = 0
    for end in ends:
        stop = end + horizon_s
        next_cut = s_t[s_t > end]
        if len(next_cut):
            stop = min(stop, float(next_cut[0]))
        window = (m_t > end) & (m_t < stop)
        times = m_t[window]
        values = m_v[window]
        prior = np.searchsorted(r_t, times, side="right") - 1
        valid = (prior >= 0) & (r_t[np.maximum(prior, 0)] > end)
        target = np.where(valid, 1.0 / r_v[np.maximum(prior, 0)], np.nan)
        inside = np.abs(values / target - 1.0) * 100.0 <= within_pct
        found = None
        for i in range(len(times)):
            if not inside[i]:
                continue
            ahead = (times >= times[i]) & (times <= times[i] + settle_s)
            if times[i] + settle_s < stop and bool(np.all(inside[ahead])):
                found = float(times[i] - end)
                break
        if found is None:
            unresolved += 1
        else:
            durations.append(found)
    arr = np.asarray(durations)
    return {
        "episodes": len(ends),
        "settled": len(durations),
        "unsettled_before_horizon_or_next_cut": unresolved,
        "within_pct": within_pct,
        "settle_s": settle_s,
        "median_s": float(np.median(arr)) if len(arr) else None,
        "p90_s": float(np.quantile(arr, 0.9)) if len(arr) else None,
        "max_s": float(arr.max()) if len(arr) else None,
    }


def convention_evidence(
    session: Path | pd.DataFrame,
    declaration: LambdaDeliveryDeclaration,
    *,
    target_spec: str,
    measured_spec: str,
    max_separation_s: float,
    steady_window_s: float,
    holdoff_s: float,
) -> dict[str, Any]:
    """Pair steady, non-post-cut target reads with the lambda sensor and judge."""
    ram = declaration.commanded("ram_final_target")
    t_t, t_v = _times_values(_long(session, target_spec))
    m_t, m_v = _times_values(_long(session, measured_spec))
    s_t, _ = _times_values(_long(session, ram.sentinel_spec or ""))
    if not len(t_t) or not len(m_t):
        return {"verdict": "unresolvable", "reason": "channel absent"}
    steady = steady_neighbours(t_t, t_v, window_s=steady_window_s)
    if len(s_t):
        last = np.searchsorted(s_t, t_t, side="right") - 1
        since = np.where(last >= 0, t_t - s_t[np.maximum(last, 0)], np.inf)
        steady &= since >= holdoff_s
    policy = PairingPolicy.within(pd.Timedelta(seconds=max_separation_s), reference="a")
    m = match_ordered(t_t[steady], m_t, policy)
    targets = t_v[steady][m.index_a]
    measured = m_v[m.index_b]
    out: dict[str, Any] = {"target": target_spec, "measured": measured_spec}
    for side in ("above", "below"):
        check = check_target_convention(
            targets, measured, off_stoich_band=0.03, tolerance_pct=3.0, min_pairs=10, side=side
        )
        out[f"{side}_1"] = check.as_dict()
    return out


def acquisition_separation(
    session: Path | pd.DataFrame, a_spec: str, b_spec: str, *, max_separation_s: float
) -> dict[str, Any]:
    a_t, _ = _times_values(_long(session, a_spec))
    b_t, _ = _times_values(_long(session, b_spec))
    if not len(a_t) or not len(b_t):
        return {"pairs": 0}
    m = match_ordered(a_t, b_t, PairingPolicy.within(pd.Timedelta(seconds=5.0), reference="a"))
    sep = m.separation_s
    return {
        "a_reads": int(len(a_t)),
        "pairs_within_5s": m.n_pairs,
        "median_s": float(np.median(sep)) if len(sep) else None,
        "p95_s": float(np.quantile(sep, 0.95)) if len(sep) else None,
        "max_s": float(sep.max()) if len(sep) else None,
        "within_declared": int(np.sum(sep <= max_separation_s)),
    }


def state_counts(
    session: Path | pd.DataFrame,
    declaration: LambdaDeliveryDeclaration,
    *,
    calibration: AfrCalibration | None,
    noise_floor_path: Path | None,
    afr_stoich: float,
) -> dict[str, Any]:
    floor = load_noise_floor(noise_floor_path) if noise_floor_path else None
    out: dict[str, Any] = {}
    for target in declaration.commanded_sources:
        target_long = _long(session, target.spec)
        sentinel = _long(session, target.sentinel_spec) if target.sentinel_spec else None
        if target_long.empty:
            out[target.name] = {"available": False, "reason": f"{target.spec} not captured"}
            continue
        per: dict[str, Any] = {}
        for instrument in declaration.measured_sources:
            measured = _long(session, instrument.spec)
            if measured.empty:
                per[instrument.name] = {
                    "available": False,
                    "reason": f"{instrument.spec} not captured",
                }
                continue
            samples = delivery_samples(
                target=target_long,
                measured=measured,
                sentinel=sentinel,
                declaration=declaration,
                target_source=target.name,
                measured_source=instrument.name,
                calibration=calibration,
                afr_stoich=afr_stoich,
            )
            per[instrument.name] = samples.summary(floor, declaration)
        out[target.name] = per
    return out


#: Candidate delays for the enrichment scan: 0-0.6 s in 50 ms steps.
DELAY_CANDIDATES = tuple(round(0.05 * i, 2) for i in range(13))


def enrichment_delay_scan(
    sessions: Sequence[Path],
    declaration: LambdaDeliveryDeclaration,
    *,
    calibration: AfrCalibration | None,
    afr_stoich: float,
) -> dict[str, Any]:
    """The enrichment transport-delay scan each route's declared delay rests on.

    Per commanded source, on exactly the drives its declaration names — the
    frozen delay's ``fitted_on``, or the rule's ``informed_by`` for a source
    whose delay is not estimated — refusing when ``sessions`` lacks one: the
    mean-|residual| scan (:func:`estimate_transport_delay`, enrichment rows),
    its 5 % bracket, and the per-read sign test of every candidate against
    the arg-min and against 0 s (:func:`per_pair_delay_support`), with the
    per-pair bracket. A difference of means alone can be carried by a few
    large reads.
    """
    rule = declaration.eligibility.regimes.enrichment
    by_name = {path.name: path for path in sessions}
    sensor = declaration.measured("ecm_lambda_pid_0x24")
    out: dict[str, Any] = {}
    for source in declaration.commanded_sources:
        declared = rule.transport_delays[source.name]
        names = declared.fitted_on if declared.status == "frozen" else rule.informed_by
        missing = sorted(set(names) - set(by_name))
        if missing:
            raise SystemExit(
                f"--fit-enrichment-delay: {source.name} is declared on {sorted(names)}; "
                f"missing {missing}"
            )
        drives: dict[str, DriveInputs] = {}
        for name in names:
            rows = read_session(by_name[name], filters=[("source", "==", "obd")])
            target = _long(rows, source.spec)
            if target.empty:
                continue
            drives[name] = DriveInputs(
                target=target,
                measured=_long(rows, sensor.spec),
                sentinel=_long(rows, source.sentinel_spec) if source.sentinel_spec else None,
            )
        common: dict[str, Any] = {
            "declaration": declaration,
            "target_source": source.name,
            "measured_source": sensor.name,
            "calibration": calibration,
            "afr_stoich": afr_stoich,
        }
        delay = estimate_transport_delay(
            drives, candidates=DELAY_CANDIDATES, regimes=["enrichment"], **common
        )
        entry: dict[str, Any] = {
            "drives": sorted(drives),
            **delay.as_dict(),
            "mean_bracket_5pct_s": list(delay_bracket(delay, margin=0.05)),
        }
        for label, reference in (("vs_best", delay.seconds), ("vs_zero", 0.0)):
            supports = per_pair_delay_support(
                drives,
                candidates=DELAY_CANDIDATES,
                reference_s=reference,
                regimes=["enrichment"],
                **common,
            )
            entry[f"per_pair_{label}"] = [item.as_dict() for item in supports]
            entry[f"per_pair_bracket_{label}_s"] = list(per_pair_bracket(supports))
        out[source.name] = entry
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("sessions", nargs="+", type=Path)
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument("--dids", type=Path, default=DIDS_YAML)
    parser.add_argument("--providers", type=Path, default=None)
    parser.add_argument("--calibration", type=Path, default=None)
    parser.add_argument("--noise-floor", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--fit-enrichment-delay",
        action="store_true",
        help="Also run the enrichment transport-delay scan per commanded source on the "
        "drives its declaration names (fitted_on, else informed_by): mean-residual scan, "
        "per-read sign tests against the arg-min and 0 s, and both brackets (R05.16).",
    )
    parser.add_argument(
        "--fuel-type",
        choices=sorted(FUEL_PROPERTIES),
        default="gasoline",
        help="Fuel the vehicle runs; sets stoichiometry for AFR-quantity instruments.",
    )
    args = parser.parse_args(argv)
    stoich = FUEL_PROPERTIES[args.fuel_type].afr_stoich
    profile_dir = args.declaration.parent
    providers = args.providers or profile_dir / "tuning_providers.yaml"
    calibration_path = args.calibration or profile_dir / "afr_calibration.yaml"
    noise_path = args.noise_floor or profile_dir / "noise_floor.yaml"
    declaration = load_lambda_delivery(
        args.declaration, dids_path=args.dids, providers_path=providers
    )
    calibration = load_afr_calibration(calibration_path) if calibration_path.exists() else None
    ram = declaration.commanded("ram_final_target")
    pid = declaration.commanded("mode01_pid_0x44")
    sensor = declaration.measured("ecm_lambda_pid_0x24")
    elig = declaration.eligibility
    tol = declaration.acquisition.max_separation_s
    report: dict[str, Any] = {"declaration": str(args.declaration), "sessions": {}}
    for session_dir in args.sessions:
        # Read once, polled rows only: every check below re-reads channels,
        # and a whole-session read carries ~100k raw CAN frames per minute.
        session = read_session(session_dir, filters=[("source", "==", "obd")])
        entry: dict[str, Any] = {}
        if _long(session, ram.spec).empty:
            # A Mode 01-only drive (it can still inform the PID 0x44 route's
            # delay scan): the RAM-target checks have nothing to read.
            entry["available"] = False
            entry["reason"] = f"{ram.spec} not captured; RAM-target checks skipped"
            report["sessions"][session_dir.name] = entry
            continue
        policy = PairingPolicy.within(pd.Timedelta(seconds=tol))
        entry["ram_vs_pid_0x44"] = target_source_agreement(
            session,
            ram.spec,
            pid.spec,
            policy=policy,
            closed_loop_band=elig.closed_loop_band,
            steady_window_s=1.5,
        )
        # Negative control: a neighbouring word of the same RAM window must
        # NOT agree with the target, or the agreement test proves nothing.
        entry["negative_control_ram_vs_engine_speed_ram"] = _negative_control(
            session, ram.spec, "mem:ECM_ENGINE_SPEED_RAM", tol
        )
        entry["fuel_cut"] = fuel_cut_coincidence(session, declaration, max_separation_s=tol)
        entry["convention_ram_vs_lambda_sensor"] = convention_evidence(
            session,
            declaration,
            target_spec=ram.spec,
            measured_spec=sensor.spec,
            max_separation_s=tol,
            steady_window_s=1.0,
            holdoff_s=elig.regimes.closed_loop.post_fuel_cut_holdoff_s,
        )
        # The 78592f4 known answer reproduced on these drives: PID 0x44 is
        # polled ~4x faster, so it holds steady through enrichment often
        # enough to judge.
        entry["convention_pid_0x44_vs_lambda_sensor"] = convention_evidence(
            session,
            declaration,
            target_spec=pid.spec,
            measured_spec=sensor.spec,
            max_separation_s=tol,
            steady_window_s=1.0,
            holdoff_s=elig.regimes.closed_loop.post_fuel_cut_holdoff_s,
        )
        entry["convention_negative_control_timing_ram"] = convention_evidence(
            session,
            declaration,
            target_spec="mem:ECM_IGNITION_TIMING_RAM",
            measured_spec=sensor.spec,
            max_separation_s=tol,
            steady_window_s=1.0,
            holdoff_s=elig.regimes.closed_loop.post_fuel_cut_holdoff_s,
        )
        entry["post_cut_recovery"] = post_cut_recovery(session, declaration)
        entry["acquisition_ram_vs_lambda_sensor"] = acquisition_separation(
            session, ram.spec, sensor.spec, max_separation_s=tol
        )
        entry["acquisition_ram_vs_pid_0x44"] = acquisition_separation(
            session, ram.spec, pid.spec, max_separation_s=tol
        )
        entry["delivery_samples"] = state_counts(
            session,
            declaration,
            calibration=calibration,
            noise_floor_path=noise_path,
            afr_stoich=stoich,
        )
        report["sessions"][session_dir.name] = entry
    if args.fit_enrichment_delay:
        report["enrichment_transport_delay"] = enrichment_delay_scan(
            args.sessions, declaration, calibration=calibration, afr_stoich=stoich
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=_json_default) + "\n")
    print(json.dumps(report, indent=1, default=_json_default)[:20000])
    return 0


def _negative_control(session: Path | pd.DataFrame, a: str, b: str, tol: float) -> dict[str, Any]:
    a_t, a_v = _times_values(_long(session, a))
    b_t, b_v = _times_values(_long(session, b))
    if not len(a_t) or not len(b_t):
        return {"pairs": 0}
    m = match_ordered(a_t, b_t, PairingPolicy.within(pd.Timedelta(seconds=tol), reference="a"))
    ratio = a_v[m.index_a] / b_v[m.index_b]
    return {
        "pairs": m.n_pairs,
        "exact_fraction": float(np.mean(ratio == 1.0)) if m.n_pairs else None,
        "within_0p5pct_fraction": float(np.mean(np.abs(ratio - 1.0) <= 0.005))
        if m.n_pairs
        else None,
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, np.bool_):
        return bool(value)
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
