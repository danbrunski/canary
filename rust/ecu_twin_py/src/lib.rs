//! `canary_ecu_twin`: a thin PyO3 binding of `ecu_twin_core` (E06.2, and
//! the surface E06.3 benchmarks on the Pi).
//!
//! Only conversion lives here. Python ints arrive as `i64` and are narrowed
//! to the contract's widths by the core's own boundary helpers; a value
//! outside them raises `ValueError("out_of_domain: ...")`. Every refusal is
//! `ValueError("<contract kind>: ...")`. Tables and curves are converted and
//! validated once, into owned immutable objects (`TargetPhiTable`,
//! `LoadCurves`), so a per-tuple call crosses the boundary with three
//! integers; `command_batch` crosses once per bounded batch.

use ecu_twin_core::fuel_comp_load::{reconstruct, LoadInputCurves};
use ecu_twin_core::lookup::{self, AxisHit, LookupResult, Table2d};
use ecu_twin_core::target_phi::{self, CommandConstants, CommandResult};
use ecu_twin_core::uprev::{self, UpRevCycle, UpRevState};
use ecu_twin_core::{byte, word, Refusal};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

/// Largest batch one call accepts (E04's bounded handoff, not a stream).
const MAX_BATCH: usize = 4096;

fn refused(r: Refusal) -> PyErr {
    PyValueError::new_err(format!("{}: refused by ecu_twin_core", r.kind()))
}

fn w(v: i64) -> PyResult<u16> {
    word(v).map_err(refused)
}

fn by(v: i64) -> PyResult<u8> {
    byte(v).map_err(refused)
}

fn l(v: i64) -> PyResult<u32> {
    u32::try_from(v).map_err(|_| refused(Refusal::OutOfDomain))
}

fn words(v: &[i64]) -> PyResult<Vec<u16>> {
    v.iter().map(|&x| w(x)).collect()
}

fn hit<'py>(py: Python<'py>, h: AxisHit) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("index", h.index)?;
    d.set_item("numerator", h.numerator)?;
    d.set_item("span", h.span)?;
    d.set_item("edge", h.edge.map(|e| e.name()))?;
    d.set_item("fraction_q16", h.fraction_q16())?;
    Ok(d)
}

fn lookup_dict<'py>(py: Python<'py>, r: &LookupResult) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("value", r.value)?;
    d.set_item("row", hit(py, r.row)?)?;
    d.set_item("column", hit(py, r.column)?)?;
    d.set_item("row_fraction", r.row_fraction)?;
    d.set_item("column_fraction", r.column_fraction)?;
    let cells: Vec<[usize; 2]> = r.cells().iter().map(|&(a, c)| [a, c]).collect();
    d.set_item("cells", cells)?;
    Ok(d)
}

fn command_dict<'py>(py: Python<'py>, r: &CommandResult) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("branch", r.branch.name())?;
    d.set_item("cell", r.cell)?;
    d.set_item("word", r.word)?;
    d.set_item("reason", r.reason)?;
    d.set_item("lookup", lookup_dict(py, &r.lookup)?)?;
    Ok(d)
}

fn table(cells: Vec<Vec<i64>>, rows: Vec<i64>, columns: Vec<i64>) -> PyResult<Table2d> {
    let cells: Vec<Vec<u16>> = cells.iter().map(|r| words(r)).collect::<PyResult<_>>()?;
    Table2d::new(&cells, &words(&rows)?, &words(&columns)?).map_err(refused)
}

/// `search_axis(breakpoints, value)` -> dict or None.
#[pyfunction]
fn search_axis(py: Python<'_>, breakpoints: Vec<i64>, value: i64) -> PyResult<Option<Py<PyDict>>> {
    let value = w(value)?;
    match lookup::search_axis(&words(&breakpoints)?, value) {
        None => Ok(None),
        Some(h) => Ok(Some(hit(py, h)?.unbind())),
    }
}

/// `interpolate_q16(a, b, fraction)`.
#[pyfunction]
fn interpolate_q16(a: i64, b: i64, fraction: i64) -> PyResult<u16> {
    Ok(lookup::interpolate_q16(w(a)?, w(b)?, w(fraction)?))
}

/// `lookup_2d(cells, row_breakpoints, column_breakpoints, row_value, column_value)`.
#[pyfunction]
fn lookup_2d(
    py: Python<'_>,
    cells: Vec<Vec<i64>>,
    row_breakpoints: Vec<i64>,
    column_breakpoints: Vec<i64>,
    row_value: i64,
    column_value: i64,
) -> PyResult<Py<PyDict>> {
    let (row, column) = (w(row_value)?, w(column_value)?);
    let t = table(cells, row_breakpoints, column_breakpoints)?;
    Ok(lookup_dict(py, &lookup::lookup_2d(&t, row, column))?.unbind())
}

/// `lookup_curve_q16(values, breakpoints, value)`.
#[pyfunction]
fn lookup_curve_q16(values: Vec<i64>, breakpoints: Vec<i64>, value: i64) -> PyResult<u16> {
    let value = w(value)?;
    lookup::lookup_curve_q16(&words(&values)?, &words(&breakpoints)?, value).map_err(refused)
}

/// `lookup_shift12_q16(table, value)`.
#[pyfunction]
fn lookup_shift12_q16(table: Vec<i64>, value: i64) -> PyResult<u16> {
    let value = w(value)?;
    lookup::lookup_shift12_q16(&words(&table)?, value).map_err(refused)
}

/// `x_from_bfs(bfs, scaler, shift, cap)`.
#[pyfunction]
fn x_from_bfs(bfs: i64, scaler: i64, shift: i64, cap: i64) -> PyResult<u16> {
    target_phi::x_from_bfs(w(bfs)?, w(scaler)?, by(shift)?, w(cap)?).map_err(refused)
}

/// A resolved Target AFR table and its gate constants, validated once.
#[pyclass(frozen, module = "canary_ecu_twin")]
struct TargetPhiTable {
    table: Table2d,
    constants: CommandConstants,
}

#[pymethods]
impl TargetPhiTable {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        cells: Vec<Vec<i64>>,
        row_breakpoints: Vec<i64>,
        column_breakpoints: Vec<i64>,
        flag_bit: i64,
        load_bits: i64,
        floor_cell: i64,
        threshold_cell: i64,
        hysteresis_cells: i64,
        cell_to_word_shift: i64,
    ) -> PyResult<Self> {
        let constants = CommandConstants::new(
            by(flag_bit)?,
            by(load_bits)?,
            w(floor_cell)?,
            w(threshold_cell)?,
            w(hysteresis_cells)?,
            by(cell_to_word_shift)?,
        )
        .map_err(refused)?;
        let table = table(cells, row_breakpoints, column_breakpoints)?;
        Ok(TargetPhiTable { table, constants })
    }

    /// One tuple -> the contract's `target_command` output.
    fn command(
        &self,
        py: Python<'_>,
        rpm_raw: i64,
        load_raw: i64,
        flag_raw: i64,
    ) -> PyResult<Py<PyDict>> {
        let r = target_phi::target_command(
            &self.table,
            &self.constants,
            w(rpm_raw)?,
            w(load_raw)?,
            l(flag_raw)?,
        );
        Ok(command_dict(py, &r)?.unbind())
    }

    /// The gated word only (`None` in the band): the smallest return.
    fn word(&self, rpm_raw: i64, load_raw: i64, flag_raw: i64) -> PyResult<Option<u32>> {
        Ok(target_phi::target_command(
            &self.table,
            &self.constants,
            w(rpm_raw)?,
            w(load_raw)?,
            l(flag_raw)?,
        )
        .word)
    }

    /// A bounded batch of `(rpm_raw, load_raw, flag_raw)` -> words, in one
    /// crossing; the arithmetic runs with the interpreter detached.
    fn word_batch(
        &self,
        py: Python<'_>,
        tuples: Vec<(i64, i64, i64)>,
    ) -> PyResult<Vec<Option<u32>>> {
        if tuples.len() > MAX_BATCH {
            return Err(PyValueError::new_err(format!(
                "batch of {} exceeds {MAX_BATCH}",
                tuples.len()
            )));
        }
        let inputs: Vec<(u16, u16, u32)> = tuples
            .iter()
            .map(|&(r, ld, f)| Ok((w(r)?, w(ld)?, l(f)?)))
            .collect::<PyResult<_>>()?;
        Ok(py.detach(|| {
            inputs
                .iter()
                .map(|&(r, ld, f)| {
                    target_phi::target_command(&self.table, &self.constants, r, ld, f).word
                })
                .collect()
        }))
    }

    /// Bytes of table state held (the E04.3 <= 1 MiB budget).
    #[getter]
    fn footprint_bytes(&self) -> usize {
        self.table.footprint_bytes()
    }
}

/// The `0xFFFF118A` chain's calibration, validated once.
#[pyclass(frozen, module = "canary_ecu_twin")]
struct LoadCurves {
    curves: LoadInputCurves,
}

#[pymethods]
impl LoadCurves {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        area_breakpoints: Vec<i64>,
        area_values: Vec<i64>,
        fill_low: Vec<i64>,
        fill_high: Vec<i64>,
        rpm_multiplier: i64,
        reciprocal_numerator: i64,
        idle_air_cap: i64,
        limp_home_area: i64,
    ) -> PyResult<Self> {
        let curves = LoadInputCurves::new(
            &words(&area_breakpoints)?,
            &words(&area_values)?,
            &words(&fill_low)?,
            &words(&fill_high)?,
            by(rpm_multiplier)?,
            l(reciprocal_numerator)?,
            w(idle_air_cap)?,
            w(limp_home_area)?,
        )
        .map_err(refused)?;
        Ok(LoadCurves { curves })
    }

    /// The contract's `fuel_comp_load` output.
    #[pyo3(signature = (throttle_counts, rpm_word, idle_air, purge_area, limp_home=false))]
    fn reconstruct(
        &self,
        py: Python<'_>,
        throttle_counts: i64,
        rpm_word: i64,
        idle_air: i64,
        purge_area: i64,
        limp_home: bool,
    ) -> PyResult<Py<PyDict>> {
        let (t, r, p) = (w(throttle_counts)?, w(rpm_word)?, w(purge_area)?);
        let trace = reconstruct(t, r, idle_air, p, &self.curves, limp_home).map_err(refused)?;
        let d = PyDict::new(py);
        d.set_item("byte", trace.byte)?;
        d.set_item("x", trace.x)?;
        d.set_item("area", trace.area)?;
        d.set_item("w1124", trace.w1124)?;
        d.set_item("w116a", trace.w116a)?;
        Ok(d.unbind())
    }
}

type PyState = (
    Option<i64>,
    Option<i64>,
    Option<i64>,
    Option<i64>,
    Option<i64>,
    Option<i64>,
);

fn state_in(s: PyState) -> PyResult<UpRevState> {
    Ok(UpRevState {
        countdown: s.0.map(w).transpose()?,
        debounce: s.1.map(by).transpose()?,
        selector: s.2.map(by).transpose()?,
        magic: s.3.map(l).transpose()?,
        active_map: s.4.map(by).transpose()?,
        history: s.5.map(l).transpose()?,
    })
}

fn state_dict<'py>(py: Python<'py>, s: &UpRevState) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("countdown", s.countdown)?;
    d.set_item("debounce", s.debounce)?;
    d.set_item("selector", s.selector)?;
    d.set_item("magic", s.magic)?;
    d.set_item("active_map", s.active_map)?;
    d.set_item("history", s.history)?;
    Ok(d)
}

fn cycle_dict<'py>(
    py: Python<'py>,
    step: Result<UpRevCycle, &'static str>,
) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    match step {
        Ok(c) => {
            d.set_item("state", state_dict(py, &c.state)?)?;
            d.set_item("copied", c.copied)?;
            d.set_item("indicator", c.indicator)?;
        }
        Err(reason) => d.set_item("reason", reason)?,
    }
    Ok(d)
}

/// `uprev_power_on(retained)` with the state as a six-tuple (None unknown).
#[pyfunction]
fn uprev_power_on(py: Python<'_>, retained: PyState) -> PyResult<Py<PyDict>> {
    let (state, copied) = uprev::uprev_power_on(state_in(retained)?).map_err(refused)?;
    let d = PyDict::new(py);
    d.set_item("state", state_dict(py, &state)?)?;
    d.set_item("copied", copied)?;
    Ok(d.unbind())
}

/// `uprev_step(state, inputs_2ee0)`.
#[pyfunction]
fn uprev_step(py: Python<'_>, state: PyState, inputs_2ee0: i64) -> PyResult<Py<PyDict>> {
    let state = state_in(state)?;
    Ok(cycle_dict(py, uprev::uprev_step(state, by(inputs_2ee0)?))?.unbind())
}

/// `uprev_sequence(state, inputs)`: cycles in order, ending at the first
/// unevaluable one (the vectors' harness composition).
#[pyfunction]
fn uprev_sequence(py: Python<'_>, state: PyState, inputs: Vec<i64>) -> PyResult<Py<PyList>> {
    let mut state = state_in(state)?;
    let steps = PyList::empty(py);
    for value in inputs {
        let step = uprev::uprev_step(state, by(value)?);
        steps.append(cycle_dict(py, step)?)?;
        match step {
            Ok(cycle) => state = cycle.state,
            Err(_) => break,
        }
    }
    Ok(steps.unbind())
}

/// `loaded_slot(magic, active_map)`.
#[pyfunction]
fn loaded_slot(
    py: Python<'_>,
    magic: Option<i64>,
    active_map: Option<i64>,
) -> PyResult<Py<PyDict>> {
    let d = PyDict::new(py);
    match uprev::loaded_slot(magic.map(l).transpose()?, active_map.map(by).transpose()?) {
        Ok((slot, source)) => {
            d.set_item("slot", slot)?;
            d.set_item("source", source.name())?;
        }
        Err(reason) => d.set_item("reason", reason)?,
    }
    Ok(d.unbind())
}

#[pymodule]
fn canary_ecu_twin(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("CONTRACT_VERSION", ecu_twin_core::CONTRACT_VERSION)?;
    m.add("KERNEL_VERSION", ecu_twin_core::KERNEL_VERSION)?;
    m.add("STATE_MODEL_VERSION", ecu_twin_core::STATE_MODEL_VERSION)?;
    m.add("BACKEND", "rust")?;
    m.add("CRATE_VERSION", env!("CARGO_PKG_VERSION"))?;
    m.add(
        "BUILD_PROFILE",
        if cfg!(debug_assertions) {
            "debug"
        } else {
            "release"
        },
    )?;
    m.add("MAX_BATCH", MAX_BATCH)?;
    m.add_function(wrap_pyfunction!(search_axis, m)?)?;
    m.add_function(wrap_pyfunction!(interpolate_q16, m)?)?;
    m.add_function(wrap_pyfunction!(lookup_2d, m)?)?;
    m.add_function(wrap_pyfunction!(lookup_curve_q16, m)?)?;
    m.add_function(wrap_pyfunction!(lookup_shift12_q16, m)?)?;
    m.add_function(wrap_pyfunction!(x_from_bfs, m)?)?;
    m.add_function(wrap_pyfunction!(uprev_power_on, m)?)?;
    m.add_function(wrap_pyfunction!(uprev_step, m)?)?;
    m.add_function(wrap_pyfunction!(uprev_sequence, m)?)?;
    m.add_function(wrap_pyfunction!(loaded_slot, m)?)?;
    m.add_class::<TargetPhiTable>()?;
    m.add_class::<LoadCurves>()?;
    Ok(())
}
