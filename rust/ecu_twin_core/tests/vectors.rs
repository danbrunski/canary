//! Runs the contract's language-neutral vectors (`tests/fixtures/ecu_twin/`,
//! built and checked against the Python reference by
//! `tests/ecu_twin_vectors.py`) through the Rust core, plus Rust-side
//! generated edge cases. Run in both profiles:
//!
//! ```text
//! cargo test --manifest-path rust/Cargo.toml
//! cargo test --manifest-path rust/Cargo.toml --release
//! ```
//!
//! Every vector must reproduce the reference's `out` (or `refuse`) exactly;
//! every control must *not* reproduce the answer it names.

use std::path::PathBuf;

use ecu_twin_core::fuel_comp_load::{reconstruct, LoadInputCurves};
use ecu_twin_core::lookup::{
    interpolate_q16, lookup_2d, lookup_curve_q16, lookup_shift12_q16, search_axis, AxisHit,
    LookupResult, Table2d,
};
use ecu_twin_core::target_phi::{target_command, x_from_bfs, CommandConstants};
use ecu_twin_core::uprev::{loaded_slot, uprev_power_on, uprev_step, UpRevCycle, UpRevState};
use ecu_twin_core::{byte, word, Refusal, CONTRACT_VERSION, KERNEL_VERSION, STATE_MODEL_VERSION};
use serde_json::{json, Map, Value};

fn fixture(name: &str) -> Value {
    let path: PathBuf = [
        env!("CARGO_MANIFEST_DIR"),
        "..",
        "..",
        "tests",
        "fixtures",
        "ecu_twin",
        name,
    ]
    .iter()
    .collect();
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    serde_json::from_str(&text).expect("vector file is JSON")
}

// -- boundary conversion (JSON -> contract widths) ---------------------------------

fn int(value: &Value) -> Result<i64, Refusal> {
    if let Some(i) = value.as_i64() {
        return Ok(i);
    }
    if value.as_u64().is_some() {
        return Err(Refusal::OutOfDomain);
    }
    match value.as_f64() {
        Some(f) if f.fract() != 0.0 => Err(Refusal::NotWholeCount),
        _ => Err(Refusal::OutOfDomain),
    }
}

fn w(value: &Value) -> Result<u16, Refusal> {
    word(int(value)?)
}

fn b(value: &Value) -> Result<u8, Refusal> {
    byte(int(value)?)
}

fn u32_of(value: &Value) -> Result<u32, Refusal> {
    u32::try_from(int(value)?).map_err(|_| Refusal::OutOfDomain)
}

fn words(value: &Value) -> Result<Vec<u16>, Refusal> {
    value.as_array().expect("array").iter().map(w).collect()
}

fn opt<T>(value: &Value, f: impl Fn(&Value) -> Result<T, Refusal>) -> Result<Option<T>, Refusal> {
    if value.is_null() {
        Ok(None)
    } else {
        f(value).map(Some)
    }
}

// -- outputs, shaped as the reference writes them ------------------------------------

fn hit_json(hit: Option<AxisHit>) -> Value {
    match hit {
        None => Value::Null,
        Some(h) => json!({
            "index": h.index,
            "numerator": h.numerator,
            "span": h.span,
            "edge": h.edge.map(|e| e.name()),
            "fraction_q16": h.fraction_q16(),
        }),
    }
}

fn lookup_json(r: &LookupResult) -> Value {
    json!({
        "value": r.value,
        "row": hit_json(Some(r.row)),
        "column": hit_json(Some(r.column)),
        "row_fraction": r.row_fraction,
        "column_fraction": r.column_fraction,
        "cells": r.cells().iter().map(|(a, c)| json!([a, c])).collect::<Vec<_>>(),
    })
}

fn state_json(s: &UpRevState) -> Value {
    json!({
        "countdown": s.countdown,
        "debounce": s.debounce,
        "selector": s.selector,
        "magic": s.magic,
        "active_map": s.active_map,
        "history": s.history,
    })
}

fn cycle_json(step: Result<UpRevCycle, &'static str>) -> Value {
    match step {
        Ok(c) => {
            json!({"state": state_json(&c.state), "copied": c.copied, "indicator": c.indicator})
        }
        Err(reason) => json!({"reason": reason}),
    }
}

// -- inputs ---------------------------------------------------------------------------

fn table(named: &Value, name: &str) -> Result<Table2d, Refusal> {
    let t = &named["tables"][name];
    let rows: Vec<Vec<u16>> = t["cells"]
        .as_array()
        .expect("rows")
        .iter()
        .map(words)
        .collect::<Result<_, _>>()?;
    Table2d::new(
        &rows,
        &words(&t["row_breakpoints"])?,
        &words(&t["column_breakpoints"])?,
    )
}

fn constants(named: &Value, name: &str) -> Result<CommandConstants, Refusal> {
    let k = &named["constants"][name];
    CommandConstants::new(
        b(&k["flag_bit"])?,
        b(&k["load_bits"])?,
        w(&k["floor_cell"])?,
        w(&k["threshold_cell"])?,
        w(&k["hysteresis_cells"])?,
        b(&k["cell_to_word_shift"])?,
    )
}

fn load_curves(named: &Value, name: &str) -> Result<LoadInputCurves, Refusal> {
    let c = &named["load_curves"][name];
    LoadInputCurves::new(
        &words(&c["area_breakpoints"])?,
        &words(&c["area_values"])?,
        &words(&c["fill_low"])?,
        &words(&c["fill_high"])?,
        b(&c["rpm_multiplier"])?,
        u32_of(&c["reciprocal_numerator"])?,
        w(&c["idle_air_cap"])?,
        w(&c["limp_home_area"])?,
    )
}

fn uprev_state(value: &Value) -> Result<UpRevState, Refusal> {
    let f = value.as_array().expect("six fields");
    assert_eq!(f.len(), 6);
    Ok(UpRevState {
        countdown: opt(&f[0], w)?,
        debounce: opt(&f[1], b)?,
        selector: opt(&f[2], b)?,
        magic: opt(&f[3], u32_of)?,
        active_map: opt(&f[4], b)?,
        history: opt(&f[5], u32_of)?,
    })
}

/// One vector through the core: `Ok(out)` or `Err(refusal)`.
fn run(op: &str, i: &Value, named: &Value) -> Result<Value, Refusal> {
    Ok(match op {
        "search_axis" => {
            let axis = match i.get("axis") {
                Some(name) => words(&named["axes"][name.as_str().expect("name")])?,
                None => words(&i["breakpoints"])?,
            };
            json!({"hit": hit_json(search_axis(&axis, w(&i["value"])?))})
        }
        "interpolate_q16" => {
            json!({"value": interpolate_q16(w(&i["a"])?, w(&i["b"])?, w(&i["fraction"])?)})
        }
        "lookup_2d" => {
            let (row, column) = (w(&i["row_value"])?, w(&i["column_value"])?);
            let t = table(named, i["table"].as_str().expect("name"))?;
            lookup_json(&lookup_2d(&t, row, column))
        }
        "lookup_curve_q16" => {
            let value = w(&i["value"])?;
            let c = &named["curves"][i["curve"].as_str().expect("name")];
            json!({"value": lookup_curve_q16(&words(&c["values"])?, &words(&c["breakpoints"])?, value)?})
        }
        "lookup_shift12_q16" => {
            let value = w(&i["value"])?;
            let t = words(&named["tables16"][i["table16"].as_str().expect("name")])?;
            json!({"value": lookup_shift12_q16(&t, value)?})
        }
        "target_command" => {
            let (rpm, load, flag) = (
                w(&i["rpm_raw"])?,
                w(&i["load_raw"])?,
                u32_of(&i["flag_raw"])?,
            );
            let k = constants(named, i["constants"].as_str().expect("name"))?;
            let t = table(named, i["table"].as_str().expect("name"))?;
            let r = target_command(&t, &k, rpm, load, flag);
            json!({
                "branch": r.branch.name(),
                "cell": r.cell,
                "word": r.word,
                "reason": r.reason,
                "lookup": lookup_json(&r.lookup),
            })
        }
        "x_from_bfs" => json!({"value": x_from_bfs(
            w(&i["bfs"])?, w(&i["scaler"])?, b(&i["shift"])?, w(&i["cap"])?
        )?}),
        "load_input_curves" => {
            load_curves(named, i["curves"].as_str().expect("name"))?;
            json!({"valid": true})
        }
        "fuel_comp_load" => {
            let (throttle, rpm, purge) = (
                w(&i["throttle_counts"])?,
                w(&i["rpm_word"])?,
                w(&i["purge_area"])?,
            );
            let idle = int(&i["idle_air"])?;
            let curves = load_curves(named, i["curves"].as_str().expect("name"))?;
            let limp = i["limp_home"].as_bool().expect("bool");
            let t = reconstruct(throttle, rpm, idle, purge, &curves, limp)?;
            json!({"byte": t.byte, "x": t.x, "area": t.area, "w1124": t.w1124, "w116a": t.w116a})
        }
        "uprev_power_on" => {
            let (state, copied) = uprev_power_on(uprev_state(&i["retained"])?)?;
            json!({"state": state_json(&state), "copied": copied})
        }
        "uprev_step" => {
            let state = uprev_state(&i["state"])?;
            cycle_json(uprev_step(state, b(&i["inputs_2ee0"])?))
        }
        "uprev_sequence" => {
            let mut state = uprev_state(&i["state"])?;
            let mut steps = Vec::new();
            for input in i["inputs"].as_array().expect("inputs") {
                let step = uprev_step(state, b(input)?);
                steps.push(cycle_json(step));
                match step {
                    Ok(cycle) => state = cycle.state,
                    Err(_) => break,
                }
            }
            json!({"steps": steps})
        }
        "loaded_slot" => match loaded_slot(opt(&i["magic"], u32_of)?, opt(&i["active_map"], b)?) {
            Ok((slot, source)) => json!({"slot": slot, "source": source.name()}),
            Err(reason) => json!({"reason": reason}),
        },
        other => panic!("vector op {other} is not implemented by the Rust core"),
    })
}

#[derive(Default)]
struct Tally {
    vectors: usize,
    refusals: usize,
    controls: usize,
    independent: usize,
}

fn run_file(name: &str, kind: &str) -> Tally {
    let doc = fixture(name);
    assert_eq!(doc["format"], "canary-ecu-twin-vectors");
    assert_eq!(doc["kind"], kind);
    assert_eq!(
        doc["contract_version"],
        json!(CONTRACT_VERSION),
        "contract version"
    );
    assert_eq!(
        doc["kernel_version"],
        json!(KERNEL_VERSION),
        "kernel version"
    );
    assert_eq!(
        doc["state_model_version"],
        json!(STATE_MODEL_VERSION),
        "state model version"
    );
    let named = if kind == "lookup" {
        doc["named"].clone()
    } else {
        Value::Object(Map::new())
    };
    let mut tally = Tally::default();
    let mut failures = Vec::new();
    for v in doc["vectors"].as_array().expect("vectors") {
        let id = v["id"].as_str().expect("id");
        let op = v["op"].as_str().expect("op");
        let got = run(op, &v["in"], &named);
        let source = v["source"].as_str().expect("source");
        if !source.starts_with("generated:") {
            tally.independent += 1;
        }
        if v.get("kind").and_then(Value::as_str) == Some("control") {
            tally.controls += 1;
            let out = got
                .as_ref()
                .unwrap_or_else(|r| panic!("{id}: control refused ({r})"));
            for (key, wrong) in v["must_differ"].as_object().expect("must_differ") {
                if &out[key] == wrong {
                    failures.push(format!("{id}: control {source} reproduced {key} = {wrong}"));
                }
            }
        }
        match (&got, v.get("refuse"), v.get("out")) {
            (Ok(out), None, Some(expected)) if out == expected => {}
            (Err(r), Some(kind), None) if kind == r.kind() => tally.refusals += 1,
            _ => failures.push(format!(
                "{id} ({op}, {source}): got {got:?}, expected out={:?} refuse={:?}",
                v.get("out"),
                v.get("refuse")
            )),
        }
        tally.vectors += 1;
    }
    assert!(
        failures.is_empty(),
        "{} of {} vectors failed:\n{}",
        failures.len(),
        tally.vectors,
        failures.join("\n")
    );
    tally
}

#[test]
fn every_lookup_vector_is_reproduced_bit_for_bit() {
    let t = run_file("lookup_vectors.json", "lookup");
    eprintln!(
        "lookup vectors: {} ({} refusals, {} controls, {} hand/capture)",
        t.vectors, t.refusals, t.controls, t.independent
    );
    assert!(t.vectors > 1000 && t.controls >= 8 && t.independent >= 90);
}

#[test]
fn every_state_vector_is_reproduced_bit_for_bit() {
    let t = run_file("state_vectors.json", "state");
    eprintln!(
        "state vectors: {} ({} refusals, {} hand)",
        t.vectors, t.refusals, t.independent
    );
    assert!(t.vectors > 1000 && t.independent >= 10);
}

// -- Rust-side generated edge cases ---------------------------------------------------

/// An independent statement of `0x026F10` in i128 (floor of the exact step
/// toward `a`), against the core's u32 arithmetic over every fraction.
#[test]
fn interpolation_matches_an_independent_wide_formula_for_every_fraction() {
    let operands = [
        0u16, 1, 2, 0x7F, 0x80, 0x7FFF, 0x8000, 0x8001, 0xFFFE, 0xFFFF,
    ];
    for &a in &operands {
        for &b in &operands {
            for fraction in 0..=u16::MAX {
                let (ai, bi, fi) = (i128::from(a), i128::from(b), i128::from(fraction));
                let step = ((bi - ai).abs() * fi) >> 16;
                let expected = if bi >= ai { ai + step } else { ai - step };
                assert_eq!(
                    i128::from(interpolate_q16(a, b, fraction)),
                    expected,
                    "{a} {b} {fraction}"
                );
            }
        }
    }
}

/// Every coordinate on several axes: the search never panics, keeps
/// `0 < numerator < span`, and the fraction never decreases inside a segment.
#[test]
fn the_search_holds_its_invariants_over_the_whole_word_range() {
    let axes: [&[u16]; 5] = [
        &[0x34, 0x44, 0x5D, 0x64, 0x6C, 0x6E, 0x7F, 0x80],
        &[96, 128, 160, 224, 288, 320, 352, 416],
        &[10, 10, 20, 20, 30],
        &[300, 200, 100],
        &[0, 0xFFFF],
    ];
    for axis in axes {
        let mut previous: Option<AxisHit> = None;
        for value in 0..=u16::MAX {
            let hit = search_axis(axis, value).expect("non-empty");
            if hit.numerator != 0 {
                assert!(hit.numerator < hit.span, "{axis:?} {value}");
                assert!(hit.index + 1 < axis.len());
            }
            if let Some(p) = previous {
                if p.index == hit.index && p.edge.is_none() && hit.edge.is_none() {
                    assert!(hit.fraction_q16() >= p.fraction_q16(), "{axis:?} {value}");
                }
            }
            previous = Some(hit);
        }
    }
}

/// Every rpm word and every load byte on Map 1 (plus word extremes): the
/// value always lies within the cells the trace says were read.
#[test]
fn a_lookup_stays_within_the_cells_it_read_across_the_whole_input_range() {
    let rows: Vec<Vec<u16>> = vec![
        vec![0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x92],
        vec![0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x95],
        vec![0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x95],
        vec![0x80, 0x80, 0x80, 0x80, 0x8E, 0x90, 0x90, 0x95],
        vec![0x80, 0x80, 0x80, 0x93, 0x96, 0x95, 0x95, 0x95],
        vec![0x80, 0x80, 0x90, 0x93, 0x96, 0x99, 0x99, 0x99],
        vec![0x80, 0x8B, 0x97, 0x99, 0x9D, 0x9B, 0x9B, 0x9B],
        vec![0x93, 0x98, 0x9D, 0xA0, 0xA3, 0x9F, 0x9F, 0x9F],
    ];
    let rpm: Vec<u16> = [0x18u16, 0x20, 0x28, 0x38, 0x48, 0x50, 0x58, 0x68]
        .iter()
        .map(|b| b * 4)
        .collect();
    let load = [0x34u16, 0x44, 0x5D, 0x64, 0x6C, 0x6E, 0x7F, 0x80];
    let table = Table2d::new(&rows, &rpm, &load).expect("valid");
    let rpms = (0..=1200u16).chain([0x7FFF, 0x8000, 0xFFFE, 0xFFFF]);
    for r in rpms {
        for l in (0..=0xFFu16).chain([0x100, 0xFFFF]) {
            let result = lookup_2d(&table, r, l);
            let read: Vec<u16> = result
                .cells()
                .iter()
                .map(|&(a, c)| table.cell(a, c))
                .collect();
            let (lo, hi) = (*read.iter().min().unwrap(), *read.iter().max().unwrap());
            assert!(lo <= result.value && result.value <= hi, "{r} {l}");
            assert_eq!(
                result.cells().len(),
                match (result.row_fraction, result.column_fraction) {
                    (0, 0) => 1,
                    (0, _) | (_, 0) => 2,
                    _ => 4,
                }
            );
        }
    }
}

/// The UpRev step over every input byte from every fully-known small state:
/// never panics, the debounce always takes the masked input, and the
/// countdown only falls (or is set by a commit to `selector << 6`).
#[test]
fn the_uprev_step_is_total_over_known_states_and_every_input_byte() {
    for countdown in [0u16, 1, 0x20, 0x21, 0x3FC0, 0xFFFF] {
        for debounce in [0u8, 2, 8, 10] {
            for selector in [0u8, 1, 5, 6, 0x3F, 0xFF] {
                for inputs in 0..=u8::MAX {
                    let state = UpRevState {
                        countdown: Some(countdown),
                        debounce: Some(debounce),
                        selector: Some(selector),
                        magic: Some(0xAA55_55AA),
                        active_map: Some(3),
                        history: Some(0xFFFF_FFFF),
                    };
                    let cycle = uprev_step(state, inputs).expect("all known");
                    assert_eq!(cycle.state.debounce, Some(inputs & 0x0A));
                    let after = cycle.state.countdown.expect("known");
                    let decremented = countdown.saturating_sub(1);
                    let committed = u16::from(cycle.state.selector.unwrap_or(0)) << 6;
                    assert!(after == decremented || after == committed);
                }
            }
        }
    }
}
