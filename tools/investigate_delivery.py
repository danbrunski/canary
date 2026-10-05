#!/usr/bin/env python3
"""Run the fuel-delivery investigation loop on one stored session (E03.1).

Mints B06 delivery error and the fuel-pressure residuals (closed-loop
high-airflow droop; flow loss at B06-eligible enrichment rows) as evidence in
the vehicle's memory ledger, opens the delivery investigation for a qualified
discrepancy or retrieves the open one and folds this occurrence in, and prints
the investigation card: hypotheses from the vehicle's
``fault_hypotheses.yaml`` with supporting/refuting evidence, the pairs no
observation has separated, and the measurement that could.

With ``--predict`` it also records the two predictions for the next drive
(the discrepancy recurs; under delivery sag, pressure droop at enrichment
loses that much injector flow). With ``--verify PREDICTION_ID`` it evaluates
that prediction on this session's evidence, which must be a later drive than
the one the prediction was made from. Nothing here writes the ECU.

Reads a whole session: run it on the laptop, or on the Pi only while nothing
is recording (AGENTS.md, "No analysis on the Pi while it collects").

Usage:
  tools/investigate_delivery.py --config canary.dev.example.yaml \\
      --profile nissan-xterra-2008 --session _capture-tuning-2026-09-23T164007
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="investigate_delivery",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, required=True, help="canary.yaml to scope by.")
    parser.add_argument("--profile", default=None, help="Vehicle profile slug.")
    parser.add_argument("--session", required=True, help="Session id under the storage path.")
    parser.add_argument(
        "--program-id", default=None, help="ECU program identity, when known (else unknown)."
    )
    parser.add_argument("--predict", action="store_true", help="Record the next-drive predictions.")
    parser.add_argument(
        "--verify",
        action="append",
        default=[],
        metavar="PREDICTION_ID",
        help="Evaluate this prediction on the session's evidence (repeatable).",
    )
    parser.add_argument("--json", action="store_true", help="Print the card as JSON.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    sys.path.insert(0, str(REPO_ROOT))
    from src.analysis.delivery_evidence import (
        DELIVERY_QUANTITY,
        mint_delivery_error,
        mint_fuel_pressure_droop,
    )
    from src.analysis.delivery_investigation import (
        evaluate_outcome,
        investigate,
        predict_pressure_flow_loss,
        predict_recurrence,
        retrieve,
    )
    from src.analysis.fault_model import FAULT_MODEL_FILE, load_fault_model
    from src.analysis.tuning_context import TuningContext
    from src.config import CanaryConfig
    from src.evidence import Prediction, investigation_card, memory_ledger_for, render_text
    from src.storage.sessions import resolve_session
    from src.vehicle.profiles import profile_dir, resolve_profile_for_tooling, scope_config

    base = CanaryConfig.from_yaml(args.config)
    config = scope_config(base, resolve_profile_for_tooling(base, args.profile))
    session = resolve_session(config.collection.storage_path, args.session)
    ledger = memory_ledger_for(config)
    context = TuningContext.from_config(config)
    data = profile_dir(config)
    model, model_sha = load_fault_model(data / FAULT_MODEL_FILE)
    vin = config.vehicle.vin or None

    delivery = mint_delivery_error(
        ledger, session, context, model, model_sha, vin=vin, program_id=args.program_id
    )
    regions: tuple[Literal["high_airflow", "enrichment"], ...] = ("high_airflow", "enrichment")
    pressure = tuple(
        mint_fuel_pressure_droop(
            ledger,
            session,
            context,
            model,
            model_sha,
            region=region,
            vin=vin,
            program_id=args.program_id,
        )
        for region in regions
    )
    earlier = retrieve(ledger, delivery)
    print(f"delivery evidence {delivery}; fuel-pressure evidence {', '.join(pressure)}")
    print(f"  occurrences on record: {', '.join(earlier.occurrences)}")
    print(f"  investigations on record: {', '.join(earlier.investigations) or 'none'}")
    for prediction_id in args.verify:
        prediction = ledger.get(prediction_id)
        if not isinstance(prediction, Prediction):
            print(f"{prediction_id} is not a prediction", file=sys.stderr)
            return 2
        on = delivery if prediction.quantity == DELIVERY_QUANTITY else pressure[1]
        outcome = evaluate_outcome(ledger, prediction_id, on)
        verdict = ledger.assess(outcome)
        print(f"outcome {outcome}: {verdict.verdict} ({verdict.reason})")
    step = investigate(ledger, model, delivery_evidence=delivery, other_evidence=pressure)
    print(f"discrepancy: {step.discrepancy.status} ({step.discrepancy.detail})")
    if step.investigation is None:
        print("no investigation opened: the discrepancy is not qualified and none is open")
        return 0
    floor = (
        context.lambda_delivery.noise_floors.get("enrichment")
        if context.lambda_delivery is not None
        else None
    )
    if args.predict and floor is not None and not step.retrieved:
        for name, predict in (
            ("recurrence", predict_recurrence),
            ("pressure flow loss", predict_pressure_flow_loss),
        ):
            if name != "recurrence" and step.discrepancy.direction != "lean":
                continue
            made = predict(
                ledger,
                step.investigation,
                floor_path=data / "noise_floor.yaml",
                floor_conditions=floor.conditions,
            )
            print(f"prediction ({name}): {made}")
    card = investigation_card(ledger, step.investigation)
    if args.json:
        print(json.dumps(card.model_dump(mode="json"), indent=2, sort_keys=True))
    else:
        print(render_text(card))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
