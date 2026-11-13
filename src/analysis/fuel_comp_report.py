"""The N02.4-N02.6 result as ``fit.json`` and ``report.md`` (N02.6).

:func:`build_result` runs droop separation, the per-cell fit both ways, the
held-out validation with the MAF-bin diagnostic, and the knock-on prediction
over one samples frame; :func:`render_markdown` writes the report a person
reads; :func:`write_outputs` writes them.

**A proposal cannot be mistaken for a measured outcome**: the report opens
with that statement and every proposed value is labelled a proposal. **No
code path writes or exports a ROM**: :func:`write_outputs` writes only JSON,
Markdown and Parquet (:data:`OUTPUT_SUFFIXES`) and refuses any other name,
and nothing in the N02 modules holds image bytes for writing
(``tests/test_fuel_comp_fit.py`` checks both).
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.fuel_comp_droop import (
    DROOP_VERSION,
    PressureSession,
    apply_droop,
    droop_evidence,
    droop_known_answer,
)
from src.analysis.fuel_comp_fit import (
    FIT_VERSION,
    SIDE,
    TableFit,
    design_vs_firmware,
    fit_both_ways,
    prepare_fit_samples,
    validate,
)
from src.analysis.fuel_comp_inputs import FuelCompDeclaration
from src.analysis.fuel_comp_knock_on import KnockOnTables, knock_on, summarise_predictions
from src.analysis.fuel_comp_rom import FuelCompCalibration
from src.analysis.fuel_comp_samples import SAMPLES_VERSION, eligible_summary, route_counts
from src.jsonable import jsonable

#: The only files N02's tool writes. A ROM image has none of these suffixes.
OUTPUT_SUFFIXES = (".json", ".md", ".parquet")

PROPOSAL_HEADER = (
    "**THIS IS A PROPOSAL, NOT A MEASURED OUTCOME.** Every factor below is "
    "fitted from logged fuelling error on earlier revisions and predicts what "
    "the target table would need; no drive has tested it. **Nothing here "
    "writes or exports a ROM.** Any edit is the operator's decision, made and "
    "flashed in UpRev under the AGENTS.md rules (explicit permission per "
    "write; key on, engine off, stationary). Per N02, read it only after the "
    "first drive on the target revision has tested the restore prediction "
    "(`data/analyses/2026-10-04-fuel-comp-restore-prediction.md`); if that "
    "prediction fails, the model of how Fuel Comp acts is wrong and these "
    "factors must not be used."
)


class OutputRefusedError(ValueError):
    """A write N02's tool may not make (anything but JSON, Markdown, Parquet)."""


def write_outputs(out_dir: Path, outputs: Mapping[str, Any]) -> list[Path]:
    """Write each named output; refuse any name whose suffix is not in
    :data:`OUTPUT_SUFFIXES` before writing anything."""
    for name in outputs:
        if Path(name).suffix not in OUTPUT_SUFFIXES or Path(name).name != name:
            raise OutputRefusedError(
                f"{name!r}: N02 writes only {', '.join(OUTPUT_SUFFIXES)} files directly in "
                "--out; it never writes or exports a ROM image"
            )
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, payload in outputs.items():
        path = out_dir / name
        suffix = path.suffix
        if suffix == ".parquet":
            if not isinstance(payload, pd.DataFrame):
                raise OutputRefusedError(f"{name}: a .parquet output must be a DataFrame")
            frame = payload.astype(
                {c: "string" for c in payload.columns if payload[c].dtype == object}
            )
            frame.to_parquet(path, index=False)
        elif suffix == ".md":
            if not isinstance(payload, str):
                raise OutputRefusedError(f"{name}: a .md output must be text")
            path.write_text(payload)
        else:
            path.write_text(json.dumps(jsonable(payload), indent=2))
        written.append(path)
    return written


def _with_droop(
    samples: pd.DataFrame,
    pressure: Mapping[str, PressureSession],
    declaration: FuelCompDeclaration,
    drives: Mapping[str, str],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Eligible samples with their drive and :data:`DROOP_COLUMNS`, and the
    droop evidence (one reference per drive)."""
    rules = declaration.droop
    evidence = droop_evidence(pressure, rules, drives)
    eligible = samples[samples["state"].astype(str) == "eligible"].copy()
    eligible["drive"] = [drives.get(str(n), str(n)) for n in eligible["session"]]
    droop = apply_droop(eligible, pressure, evidence.references, evidence.model, rules)
    joined = pd.concat([eligible, droop], axis=1)
    pressure_drives = {r.session for r in evidence.references.values()}
    on_pressure_drive = joined["drive"].isin(pressure_drives)
    block = {
        **evidence.as_dict(),
        "known_answer": droop_known_answer(pressure, evidence, rules),
        "kinds_among_eligible": {
            str(k): int(v) for k, v in droop["droop_kind"].value_counts().items()
        },
        "basis_among_eligible": Counter(str(b) for b in droop["droop_basis"]).most_common(12),
        "limitations": list(rules.limitations),
        "flow_law": rules.flow_law,
        "window_half_width_s": rules.window_half_width_s,
        "min_window_reads": rules.min_window_reads,
        "reference_method": rules.reference.model_dump(),
        # The model is fitted on the pressure drives' own MAF reads, so a
        # sample on one of those drives that falls back to the model is
        # predicted by a fit that saw its drive (not refitted per fold).
        "model_leak": {
            "modelled_samples_on_pressure_drives": int(
                (on_pressure_drive & (droop["droop_kind"] == "modelled")).sum()
            ),
            "modelled_samples_elsewhere": int(
                (~on_pressure_drive & (droop["droop_kind"] == "modelled")).sum()
            ),
            "note": (
                "The droop model is fitted once on every pressure drive, not per held-out "
                "fold. Samples on other drives never informed it; the count above on "
                "pressure drives is the only leak into the held-out validation."
            ),
        },
    }
    return joined, block


def _drive_groups(drives: Mapping[str, str]) -> dict[str, list[str]]:
    """Drive id -> its sessions, for drives of more than one session."""
    groups: dict[str, list[str]] = {}
    for session, drive in drives.items():
        groups.setdefault(drive, []).append(session)
    return {d: sorted(v) for d, v in sorted(groups.items()) if len(v) > 1}


def _open_loop_target_routes(
    samples: pd.DataFrame, prepared: pd.DataFrame, declaration: FuelCompDeclaration
) -> dict[str, dict[str, Any]]:
    """Open-loop eligible and fit-usable samples per commanded-target source,
    with the declared caveat of any source that is not the phi word itself."""
    rules = declaration.samples.open_loop
    out: dict[str, dict[str, Any]] = {}
    if samples.empty:
        return out
    eligible = samples[
        (samples["route"].astype(str) == "open_loop") & (samples["state"].astype(str) == "eligible")
    ]
    usable = (
        prepared[(prepared["route"].astype(str) == "open_loop") & prepared["fit_refusal"].isna()]
        if len(prepared)
        else prepared
    )
    open_rows = (
        prepared[prepared["route"].astype(str) == "open_loop"] if len(prepared) else prepared
    )
    for name in rules.commanded_preference:
        mine = (
            open_rows[open_rows["commanded_source"].astype(str) == name]
            if len(open_rows)
            else open_rows
        )
        refused = (
            mine["fit_refusal"].dropna().astype(str).value_counts().to_dict() if len(mine) else {}
        )
        out[name] = {
            "fit_refusals": {str(k): int(v) for k, v in refused.items()},
            "eligible": int((eligible["commanded_source"].astype(str) == name).sum()),
            "usable_for_fit": int((usable["commanded_source"].astype(str) == name).sum())
            if len(usable)
            else 0,
            "caveat": rules.source_caveats.get(name),
        }
    return out


def build_result(
    samples: pd.DataFrame,
    *,
    declaration: FuelCompDeclaration,
    pressure: Mapping[str, PressureSession],
    calibrations: Mapping[str, FuelCompCalibration],
    target: FuelCompCalibration,
    tables: KnockOnTables | None,
    tables_refusal: str | None = None,
    load_byte_known_answer: Mapping[str, Any] | None = None,
    sessions: Sequence[str] = (),
    notes: Sequence[str] = (),
    drives: Mapping[str, str] | None = None,
    profile_files: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Everything ``fit.json``, ``report.md`` and the Parquet outputs carry.

    ``samples`` is N02.3's frame (every candidate, every state); ``pressure``
    the sessions read for rail pressure; ``calibrations`` every revision the
    samples were logged on; ``target`` the revision proposed on; ``tables``
    its Target AFR and timing tables (``None`` with ``tables_refusal`` when
    they could not be read, and the knock-on says so).
    """
    rules = declaration.fit
    drive_of = dict(drives or {})
    joined, droop_block = _with_droop(samples, pressure, declaration, drive_of)
    prepared = prepare_fit_samples(joined, calibrations, target, declaration)
    fits = fit_both_ways(prepared, target, rules)
    validations = {
        name: validate(prepared, target, rules, include_unvalidated=fit.include_unvalidated)
        for name, fit in fits.items()
    }
    primary_name = (
        "did_route_included" if rules.include_unvalidated_throttle_route else "did_route_excluded"
    )
    if tables is not None:
        knock = knock_on(prepared, fits[primary_name], tables, declaration)
    else:
        knock = {
            "empty": f"knock-on not computed: {tables_refusal or 'no tables'}",
            "cells": [],
            "predictions": pd.DataFrame(),
            "bfs_model": None,
            "limitations": list(declaration.knock_on.limitations),
        }
    predictions = knock.pop("predictions")
    knock["summary"] = summarise_predictions(predictions)
    revisions = {
        name: {**cal.provenance(), "k": cal.k} for name, cal in sorted(calibrations.items())
    }
    target_routes = _open_loop_target_routes(samples, prepared, declaration)
    fit_json = {
        "header": PROPOSAL_HEADER,
        "generated_at": datetime.now(UTC).isoformat(),
        "versions": {
            "samples": SAMPLES_VERSION,
            "droop": DROOP_VERSION,
            "fit": FIT_VERSION,
        },
        "target_revision": {"name": target.name, **target.provenance()},
        "revisions": revisions,
        "sessions": list(sessions),
        "drive_unit": rules.drive_unit.model_dump(),
        "drives": _drive_groups(drive_of),
        "profile_files": {k: dict(v) for k, v in (profile_files or {}).items()},
        "design_vs_firmware": design_vs_firmware(prepared, target),
        "notes": list(notes),
        "declaration": {
            "pairing": declaration.pairing.model_dump(),
            "gates": declaration.samples.gates.model_dump(),
            "closed_loop": declaration.samples.closed_loop.model_dump(),
            "open_loop": declaration.samples.open_loop.model_dump(),
            "fit": rules.model_dump(),
        },
        "sample_counts": route_counts(samples) if len(samples) else {},
        "eligible_summary": eligible_summary(samples) if len(samples) else {},
        "load_byte_known_answer": dict(load_byte_known_answer or {}),
        "droop": droop_block,
        "open_loop_target_routes": target_routes,
        "fit_refusals": (
            {k: int(v) for k, v in prepared["fit_refusal"].fillna("usable").value_counts().items()}
            if len(prepared)
            else {}
        ),
        "primary_configuration": primary_name,
        "fits": {name: fit.as_dict() for name, fit in fits.items()},
        "validation": {name: v.as_dict() for name, v in validations.items()},
        "knock_on": knock,
    }
    return {
        "fit_json": jsonable(fit_json),
        "fits": fits,
        "prepared": prepared,
        "predictions": predictions,
    }


# -- markdown -------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return "-" if not math.isfinite(value) else f"{value:.{digits}f}"
    return str(value)


def _pct(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:+.{digits}f} %"


def _grid(
    fit: Mapping[str, Any], target: Mapping[str, Any], cell_text: Any, title: str
) -> list[str]:
    rpm_axis = [int(v) * 50 for v in target["rpm_axis_raw"]]
    load_axis = [int(v) for v in target["load_axis_raw"]]
    lines = [
        f"{title} (rows: rpm, axis `0xCACD` x 50; columns: load byte, axis `0xCB29`)",
        "",
        "| rpm \\ byte | " + " | ".join(str(b) for b in load_axis) + " |",
        "|---" * (len(load_axis) + 1) + "|",
    ]
    cells = fit["cells"]
    for r in range(SIDE):
        row = [cell_text(cells[r * SIDE + c]) for c in range(SIDE)]
        lines.append(f"| {rpm_axis[r]} | " + " | ".join(row) + " |")
    lines.append("")
    return lines


def _coverage_text(cell: Mapping[str, Any]) -> str:
    if cell["status"] == "not_covered":
        return "."
    return f"{cell['n']}/{len(cell['drives'])}"


def _proposal_text(cell: Mapping[str, Any]) -> str:
    if cell["status"] == "not_covered":
        return "."
    if cell["status"] == "insufficient":
        return "ins"
    ci = cell["ci_factor"]
    change = cell["change_pct"]
    flag = "*" if cell["unvalidated_route"] else ""
    if ci is None:
        return f"{change:+.1f}{flag}"
    lo = 100.0 * (ci[0] * 128.0 / cell["current_raw"] - 1.0)
    hi = 100.0 * (ci[1] * 128.0 / cell["current_raw"] - 1.0)
    return f"{change:+.1f} [{lo:+.1f},{hi:+.1f}]{flag}"


def _validation_lines(name: str, v: Mapping[str, Any]) -> list[str]:
    lines = [
        f"**{name}**: {v['verdict']}. Diagnostic: *{v['diagnostic']}*.",
        "",
        f"- folds (drives held out in turn): {v['folds']}, with a proposal in "
        f"{v['folds_with_a_proposal']}; held-out samples compared: {v['n_compared']} "
        f"(not compared, no paired MAF read: {v['n_held_out_without_maf']})",
        "",
        "| model | held-out RMS | mean signed residual | drives better / worse / same vs "
        "current | parameters per fold (min / median / max) |",
        "|---|---|---|---|---|",
    ]
    for model, m in (v.get("models") or {}).items():
        size = m["parameters_per_fold"]
        size_text = "-" if size is None else f"{size['min']} / {size['median']:g} / {size['max']}"
        lines.append(
            f"| {model} | {_fmt(m['rms_pct'], 3)} % | {_fmt(m['mean_signed_residual_pct'], 2)} % "
            f"| {m['drives_better_than_current']} / {m['drives_worse_than_current']} / "
            f"{m['drives_same_as_current']} | {size_text} ({m['parameter_kind']}) |"
        )
    comparisons = v.get("comparisons") or {}
    if comparisons:
        lines += [
            "",
            "Paired drive bootstrap, share of resamples in which the first model's pooled MSE "
            "is lower (a win needs the declared confidence; anything less is a tie):",
            "",
        ]
        for pair in (
            "global_factor<current_table",
            "maf_bins<global_factor",
            "fitted_cells<global_factor",
            "fitted_cells<maf_bins",
            "maf_bins<fitted_cells",
            "fitted_cells<current_table",
        ):
            if pair in comparisons:
                lines.append(f"- {pair.replace('<', ' < ')}: {comparisons[pair]:.3f}")
    lines.append("")
    return lines


def render_markdown(result: Mapping[str, Any]) -> str:
    """``report.md`` from :func:`build_result`'s ``fit_json``."""
    r = result
    target = r["target_revision"]
    lines: list[str] = [
        "# N02 Fuel Comp proposal: fit, validation and knock-on",
        "",
        r["header"],
        "",
        f"Generated {r['generated_at']}. Samples v{r['versions']['samples']}, droop "
        f"v{r['versions']['droop']}, fit v{r['versions']['fit']}. Proposed on "
        f"**{target['name']}** (image sha256 `{target['image_sha256']}`).",
        "",
    ]
    for note in r.get("notes") or []:
        lines.append(f"- {note}")
    lines += ["", "## Inputs", ""]
    lines += [
        "| Revision | Image sha256 | K | Load axis `0xCB29` |",
        "|---|---|---|---|",
    ]
    for name, rev in r["revisions"].items():
        lines.append(
            f"| {name} | `{rev['image_sha256']}` | {rev['k']} | "
            f"{rev['load_axis_raw'][0]}..{rev['load_axis_raw'][-1]} |"
        )
    lines.append(
        f"| {target['name']} (target) | `{target['image_sha256']}` | {target['k']} | "
        f"{target['load_axis_raw'][0]}..{target['load_axis_raw'][-1]} |"
    )
    lines += ["", f"Sessions read: {len(r['sessions'])}.", ""]
    decl = r["declaration"]
    pairing = decl["pairing"]
    gates = decl["gates"]
    lines += [
        "## Drives and pinned inputs",
        "",
        f"Drive unit: {r['drive_unit']['method']}, sessions closer than "
        f"{r['drive_unit']['max_gap_minutes']:g} min are one drive. Merged drives: "
        f"{r['drives'] or 'none'}.",
        "",
        "Profile files read (sha256; `dirty` = differs from HEAD when read):",
        "",
        *[
            f"- `{name}` {info.get('sha256')} {'(dirty)' if info.get('dirty') else ''}"
            for name, info in r["profile_files"].items()
        ],
        "",
        f"Real-valued design vs the firmware's truncating lookup on the current table, "
        f"raw counts: {r['design_vs_firmware']}.",
        "",
        "## Gates and pairing",
        "",
        f"- Pairing: closed loop {pairing['closed_loop']['max_separation_s']} s, open loop "
        f"{pairing['open_loop']['max_separation_s']} s (acquisition time, one-to-one).",
        f"- Closed loop steadiness: {decl['closed_loop']['steady']}.",
        f"- Open loop: no steadiness gate; rpm and throttle as intervals over the B06 "
        f"delay bracket of `{decl['open_loop']['delay_bracket']['delay_source']}`.",
        f"- Coolant >= {gates['coolant']['min_c']} C; baro {gates['baro']['min_kpa']}-"
        f"{gates['baro']['max_kpa']} kPa; post-fuel-cut holdoff from lambda_delivery.yaml.",
        f"- Fit refusals per cell: >= {decl['fit']['min_weighted_pairs']} weighted pairs from "
        f">= {decl['fit']['min_drives']} drives, each with >= "
        f"{decl['fit']['min_drive_weight']}; prior {decl['fit']['prior_pairs']} pair toward "
        f"the current value; bootstrap {decl['fit']['bootstrap']['resamples']} drive "
        f"resamples, {decl['fit']['bootstrap']['interval_pct']} percentiles.",
        "",
        "## Sample accounting",
        "",
    ]
    for route, counts in r["sample_counts"].items():
        top = sorted(((k, v) for k, v in counts.items() if ":" in k), key=lambda kv: -kv[1])
        lines.append(
            f"- **{route}**: {counts['total']} candidates; eligible {counts['eligible']}, "
            f"gated {counts['gated']}, unpaired {counts['unpaired']}, input missing "
            f"{counts['input_missing']}."
        )
        lines += [f"  - {k}: {v}" for k, v in top]
    lines += ["", "Eligible by revision, route and throttle route:", ""]
    for rev, routes in r["eligible_summary"].items():
        for route, block in routes.items():
            split = ", ".join(
                f"{name} ({'validated' if b['validated'] else 'UNVALIDATED'}) {b['eligible']}"
                for name, b in block["by_throttle_route"].items()
            )
            lines.append(
                f"- {rev} {route}: eligible {block['eligible']} "
                f"(unambiguous cell {block['eligible_cell_unambiguous']}); {split}"
            )
    lines += ["", "Open-loop samples by commanded-target route:", ""]
    for name, block in r.get("open_loop_target_routes", {}).items():
        caveat = f" **Caveat:** {block['caveat']}" if block["caveat"] else ""
        lines.append(
            f"- {name}: eligible {block['eligible']}, usable for the fit "
            f"{block['usable_for_fit']}; refused: {block.get('fit_refusals') or {}}.{caveat}"
        )
    lines += ["", "Fit refusals among eligible samples:", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(r["fit_refusals"].items(), key=lambda kv: -kv[1])]
    lines += ["", "## Known answers", ""]
    ka = r["load_byte_known_answer"]
    if ka:
        lines.append(f"Load byte (N02.2): {json.dumps(ka.get('summary', ka), default=str)}")
    else:
        lines.append("Load byte (N02.2): not run.")
    dka = r["droop"]["known_answer"]
    lines += [
        "",
        f"Droop (N02.4): expected {dka['expected_psi']} psi at "
        f"{dka['maf_band_gs'][0]:g}-{dka['maf_band_gs'][1]:g} g/s ({dka['source']}).",
        f"Measured path: median {_fmt(dka.get('median_droop_psi'), 3)} psi at median "
        f"{_fmt(dka.get('median_maf_gs'), 1)} g/s, n = {dka['n']}, passed: "
        f"{dka.get('passed')} {dka.get('reason') or ''}. Model at that MAF: "
        f"{_fmt(dka.get('model_at_median_maf_psi'), 3)} psi.",
        "",
        "## Fuel-supply droop (N02.4)",
        "",
    ]
    model = r["droop"]["model"]
    lines += [
        f"- Flow law: injector flow ~ sqrt(rail gauge pressure), relative to each drive's own "
        f"low-flow reference ({r['droop']['reference_method']}).",
        f"- Model ({model['form']}): a = {_fmt(model['a_psi_per_gs'], 5)} psi/(g/s), b = "
        f"{_fmt(model['b_psi_per_gs2'], 7)} psi/(g/s)^2, n = {model['n_pairs']} pairs from "
        f"{model['drives']}, fitted on MAF {model['fitted_maf_range_gs']} and applied on "
        f"{model['applied_maf_range_gs']} (zero at zero flow by construction), pooled reference "
        f"{_fmt(model['reference_psi'], 2)} psi, residual RMS "
        f"{_fmt(model['residual_rms_psi'], 3)} psi. {model['refusal'] or ''}",
        f"- Droop kind among eligible samples: {r['droop']['kinds_among_eligible']}.",
        f"- Model leak: {r['droop']['model_leak']}.",
        "- References:",
    ]
    for name, ref in r["droop"]["references"].items():
        lines.append(f"  - {name}: {_fmt(ref['psi'], 2)} psi (n {ref['n']}) {ref['reason'] or ''}")
    lines += ["- **Limitations:**"]
    lines += [f"  - {text}" for text in r["droop"]["limitations"]]
    lines += ["", "## Per-cell proposal (N02.5)", ""]
    primary = r["primary_configuration"]
    for name, fit in r["fits"].items():
        tag = " (declared configuration)" if name == primary else ""
        lines += [
            f"### {name}{tag}",
            "",
            f"Samples fitted: {fit['n_samples']} from {len(fit['drives'])} drives; "
            f"unvalidated DID-route samples excluded: "
            f"{fit['excluded_unvalidated_route_samples']}. Cells: {fit['status_counts']}.",
            "",
        ]
        lines += _grid(fit, target, _coverage_text, "Coverage, n samples / drives counted")
        lines += _grid(
            fit,
            target,
            _proposal_text,
            "Proposal: change % [bootstrap CI] for proposed cells, `ins` insufficient, "
            "`.` not covered, `*` touched by unvalidated DID-route samples",
        )
        proposed = [c for c in fit["cells"] if c["status"] == "proposed"]
        if proposed:
            lines += [
                "The CI is a drive-level bootstrap: a cell resting on 2 drives has only 3 "
                "distinct resamples, so its interval understates the uncertainty; read the "
                "drives column with it.",
                "",
                "| cell (rpm, byte) | current | proposed factor | change | CI (factor) | n | "
                "drives | CI excludes current | DID-route weight |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            for c in sorted(proposed, key=lambda c: -abs(c["change_pct"] or 0.0)):
                rpm = int(target["rpm_axis_raw"][c["row"]]) * 50
                byte = int(target["load_axis_raw"][c["col"]])
                ci = c["ci_factor"]
                lines.append(
                    f"| {c['row']},{c['col']} ({rpm}, {byte}) | {c['current_raw']} | "
                    f"{_fmt(c['factor'], 4)} (raw {c['proposed_raw']}) | "
                    f"{_pct(c['change_pct'], 2)} | "
                    f"{'-' if ci is None else f'{ci[0]:.4f}-{ci[1]:.4f}'} | {c['n']} | "
                    f"{len(c['drives'])} | {c['change_resolved']} | "
                    f"{c['unvalidated_route_weight_share']:.2f} |"
                )
            lines.append("")
        insufficient = [c for c in fit["cells"] if c["status"] == "insufficient"]
        if insufficient:
            lines += ["Insufficient cells:", ""]
            for c in insufficient:
                lines.append(
                    f"- {c['row']},{c['col']}: n {c['n']}, weighted pairs "
                    f"{c['weighted_pairs']}, drives {len(c['drives'])}; " + "; ".join(c["refusals"])
                )
            lines.append("")
    lines += ["## Held-out validation and the MAF-shaped diagnostic", ""]
    for name, v in r["validation"].items():
        lines += _validation_lines(name, v)
    knock = r["knock_on"]
    lines += [
        "## Knock-on (N02.6), predicted at the logged samples",
        "",
        f"Configuration: {knock.get('configuration', primary)}. BFS model: "
        f"{knock.get('bfs_model')}. Summary: {knock.get('summary')}.",
        "",
    ]
    model = knock.get("bfs_model") or {}
    if model.get("subsets"):
        lines += [
            "Where c comes from (median per-sample ratio, n): "
            + "; ".join(
                f"{name} {_fmt(block['median'], 2)} (n {block['n']})"
                for name, block in model["subsets"].items()
            )
            + ". The pairs are mostly idle DID-route samples (DID 0x1208 is read often only "
            "on all-DID captures); the off-idle subset is the check that c holds where the "
            "knock-on is predicted.",
            "",
        ]
    tables = knock.get("tables")
    if tables:
        afr = tables["target_afr"]
        lines += [
            f"Read from the target image: Target AFR `{afr['table']}` slot {afr['slot']} "
            f"(image `{afr['image_sha256']}`, source {afr['table_source']}), Load Point "
            f"Scaler {tables['load_point_scaler']}, timing-trim BFS-X axis "
            f"{tables['timing_load_axis']}. The phi twin's standing blockers "
            f"{tables['target_afr_blockers']}: a predicted phi is the gated Target AFR "
            "lookup's word, not yet the final commanded word.",
            "",
        ]
    if knock.get("empty"):
        lines += [f"Empty: {knock['empty']}.", ""]
    if knock.get("cells"):
        lines += [
            "| cell | current -> proposed | samples | BFS meas/model/none | BFS ratio | X shift | "
            "crosses Target AFR breakpoint | commanded phi change | crosses timing column |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for c in knock["cells"]:
            lines.append(
                f"| {c['cell'][0]},{c['cell'][1]} | {c['current_raw']} -> {c['proposed_raw']} | "
                f"{c['samples_touched']} | {c['bfs_measured']}/{c['bfs_modelled']}/"
                f"{c['bfs_not_predicted']} | {c['bfs_ratio']} | {c['x_shift']} | "
                f"{c['crosses_target_afr_breakpoint']} | {c['phi_change']} | "
                f"{c['crosses_timing_breakpoint']} |"
            )
        lines.append("")
    lines += ["Knock-on limitations:", ""]
    lines += [f"- {text}" for text in knock.get("limitations") or []]
    lines += [
        "",
        "---",
        "",
        "A proposal, not a measured outcome. Nothing in this report or the code that "
        "wrote it writes or exports a ROM image.",
        "",
    ]
    return "\n".join(lines)


def fit_table_frame(fit: TableFit) -> pd.DataFrame:
    """One row per cell, for the Parquet output."""
    return pd.DataFrame([c.as_dict() for c in fit.cells])


__all__ = [
    "OUTPUT_SUFFIXES",
    "PROPOSAL_HEADER",
    "OutputRefusedError",
    "build_result",
    "fit_table_frame",
    "render_markdown",
    "write_outputs",
]
