//! The ECM's table arithmetic (`src/decode/ecm_lookup.py`; contract
//! operations `search_axis`, `interpolate_q16`, `lookup_2d`,
//! `lookup_curve_q16`, `lookup_shift12_q16`).

use alloc::vec::Vec;

use crate::Refusal;

/// Fixed-point weight resolution of the interpolation (`0x026F10`).
pub const FRACTION_BITS: u32 = 16;
/// `0x026C28`: entries in its table.
pub const SHIFT12_ENTRIES: usize = 16;
/// `0x026C28`: the shift that makes the index.
pub const SHIFT12_INDEX_SHIFT: u32 = 12;
/// `0x026C28`: from this input the last entry is read outright.
pub const SHIFT12_LAST_ENTRY_FROM: u16 = 0xF000;

/// Which end of an axis a coordinate fell past (and was clamped at).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Edge {
    /// Below the first breakpoint.
    Below,
    /// Above the last breakpoint.
    Above,
}

impl Edge {
    /// The contract's name.
    pub const fn name(self) -> &'static str {
        match self {
            Edge::Below => "below",
            Edge::Above => "above",
        }
    }
}

/// Where one coordinate landed on one axis.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AxisHit {
    /// The lower breakpoint the lookup reads from.
    pub index: usize,
    /// `value - axis[index]` (0 on a breakpoint and at a clamp).
    pub numerator: u16,
    /// `axis[index + 1] - axis[index]` (1 on a breakpoint and at a clamp).
    pub span: u16,
    /// The edge clamped at, if any.
    pub edge: Option<Edge>,
}

impl AxisHit {
    const fn exact(index: usize, edge: Option<Edge>) -> Self {
        AxisHit {
            index,
            numerator: 0,
            span: 1,
            edge,
        }
    }

    /// The unsigned Q16 fraction (`0x027448`): `(numerator << 16) / span`,
    /// truncated. The search guarantees `numerator < span`, so it is below
    /// `1 << 16`.
    pub fn fraction_q16(&self) -> u16 {
        if self.numerator == 0 {
            return 0;
        }
        // numerator <= 0xFFFE, so the shifted value fits u32.
        let shifted = u32::from(self.numerator) << FRACTION_BITS;
        let quotient = shifted
            .checked_div(u32::from(self.span))
            .expect("invariant: a non-zero numerator has a non-zero span");
        u16::try_from(quotient).expect("invariant: numerator < span keeps the fraction below 1")
    }
}

/// The axis search (`0x027114`, `0x027194`, `0x027154`): from the top
/// breakpoint down, the largest index with `axis[i] <= value`. `None` only
/// for an empty axis. Total for any breakpoint order: when a non-top index
/// is returned with a fraction, the scan has already found
/// `value < axis[i + 1]`, so `0 < numerator < span`.
pub fn search_axis(breakpoints: &[u16], value: u16) -> Option<AxisHit> {
    let top = breakpoints.len().checked_sub(1)?;
    for index in (0..=top).rev() {
        let point = breakpoints[index];
        if value == point {
            return Some(AxisHit::exact(index, None));
        }
        if value > point {
            if index == top {
                return Some(AxisHit::exact(top, Some(Edge::Above)));
            }
            let numerator = value.checked_sub(point).expect("invariant: value > point");
            let span = breakpoints[index + 1]
                .checked_sub(point)
                .expect("invariant: the scan saw value < axis[index + 1]");
            return Some(AxisHit {
                index,
                numerator,
                span,
                edge: None,
            });
        }
    }
    Some(AxisHit::exact(0, Some(Edge::Below)))
}

/// `0x026F10`: from `a` toward `b` by `fraction / 65536`, truncated toward
/// `a` in both directions. The product `|b - a| * fraction` is at most
/// `0xFFFF * 0xFFFF`, which fits u32; the step is below `|b - a|`, so the
/// result stays between `a` and `b`.
pub fn interpolate_q16(a: u16, b: u16, fraction: u16) -> u16 {
    if b >= a {
        let step = (u32::from(b - a) * u32::from(fraction)) >> FRACTION_BITS;
        let step = u16::try_from(step).expect("invariant: step < b - a");
        a.checked_add(step).expect("invariant: a + step <= b")
    } else {
        let step = (u32::from(a - b) * u32::from(fraction)) >> FRACTION_BITS;
        let step = u16::try_from(step).expect("invariant: step < a - b");
        a.checked_sub(step).expect("invariant: a - step >= b")
    }
}

/// A 2-D table and its axes, validated once (row-major cells; breakpoints in
/// each input's comparison domain).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Table2d {
    cells: Vec<u16>,
    row_breakpoints: Vec<u16>,
    column_breakpoints: Vec<u16>,
}

impl Table2d {
    /// Refuses an empty axis (`empty_axis`) before a shape that does not match
    /// the axes (`shape_mismatch`), the reference's order.
    pub fn new(
        rows: &[Vec<u16>],
        row_breakpoints: &[u16],
        column_breakpoints: &[u16],
    ) -> Result<Self, Refusal> {
        if row_breakpoints.is_empty() || column_breakpoints.is_empty() {
            return Err(Refusal::EmptyAxis);
        }
        if rows.len() != row_breakpoints.len()
            || rows.iter().any(|row| row.len() != column_breakpoints.len())
        {
            return Err(Refusal::ShapeMismatch);
        }
        Ok(Table2d {
            cells: rows.iter().flat_map(|row| row.iter().copied()).collect(),
            row_breakpoints: row_breakpoints.to_vec(),
            column_breakpoints: column_breakpoints.to_vec(),
        })
    }

    /// Row breakpoints.
    pub fn row_breakpoints(&self) -> &[u16] {
        &self.row_breakpoints
    }

    /// Column breakpoints.
    pub fn column_breakpoints(&self) -> &[u16] {
        &self.column_breakpoints
    }

    /// The cell at `(row, column)`.
    pub fn cell(&self, row: usize, column: usize) -> u16 {
        self.cells[row * self.column_breakpoints.len() + column]
    }

    /// Bytes the table holds (cells and axes), for the E04.3 budget.
    pub fn footprint_bytes(&self) -> usize {
        2 * (self.cells.len() + self.row_breakpoints.len() + self.column_breakpoints.len())
    }
}

/// One 2-D lookup: the value and every input that decided it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LookupResult {
    /// The value read.
    pub value: u16,
    /// The row hit.
    pub row: AxisHit,
    /// The column hit.
    pub column: AxisHit,
    /// Row Q16 fraction.
    pub row_fraction: u16,
    /// Column Q16 fraction.
    pub column_fraction: u16,
    cells: [(usize, usize); 4],
    n_cells: usize,
}

impl LookupResult {
    /// `(row, column)` of each cell read, in read order (the branch trace).
    pub fn cells(&self) -> &[(usize, usize)] {
        &self.cells[..self.n_cells]
    }
}

/// `0x026F40` at two hits (the reference's `bilinear_q16`).
pub fn bilinear_q16(table: &Table2d, row: AxisHit, column: AxisHit) -> LookupResult {
    let (r, c) = (row.index, column.index);
    let (row_fraction, column_fraction) = (row.fraction_q16(), column.fraction_q16());
    let result = |value, cells: &[(usize, usize)], rf, cf| {
        let mut trace = [(0, 0); 4];
        trace[..cells.len()].copy_from_slice(cells);
        LookupResult {
            value,
            row,
            column,
            row_fraction: rf,
            column_fraction: cf,
            cells: trace,
            n_cells: cells.len(),
        }
    };
    if column_fraction != 0 {
        let upper = interpolate_q16(table.cell(r, c), table.cell(r, c + 1), column_fraction);
        if row_fraction == 0 {
            return result(upper, &[(r, c), (r, c + 1)], 0, column_fraction);
        }
        let lower = interpolate_q16(
            table.cell(r + 1, c),
            table.cell(r + 1, c + 1),
            column_fraction,
        );
        return result(
            interpolate_q16(upper, lower, row_fraction),
            &[(r, c), (r, c + 1), (r + 1, c), (r + 1, c + 1)],
            row_fraction,
            column_fraction,
        );
    }
    if row_fraction == 0 {
        return result(table.cell(r, c), &[(r, c)], 0, 0);
    }
    result(
        interpolate_q16(table.cell(r, c), table.cell(r + 1, c), row_fraction),
        &[(r, c), (r + 1, c)],
        row_fraction,
        0,
    )
}

/// Search both axes and interpolate. Coordinates are words by type; the
/// table was validated by [`Table2d::new`].
pub fn lookup_2d(table: &Table2d, row_value: u16, column_value: u16) -> LookupResult {
    let row = search_axis(&table.row_breakpoints, row_value).expect("validated: non-empty axis");
    let column =
        search_axis(&table.column_breakpoints, column_value).expect("validated: non-empty axis");
    bilinear_q16(table, row, column)
}

/// `0x026D58` with `0x027154`: a word curve on its own word axis.
pub fn lookup_curve_q16(values: &[u16], breakpoints: &[u16], value: u16) -> Result<u16, Refusal> {
    if values.len() != breakpoints.len() || values.is_empty() {
        return Err(Refusal::CurveShape);
    }
    let hit = search_axis(breakpoints, value).expect("non-empty");
    let fraction = hit.fraction_q16();
    if fraction == 0 {
        return Ok(values[hit.index]);
    }
    Ok(interpolate_q16(
        values[hit.index],
        values[hit.index + 1],
        fraction,
    ))
}

/// `0x026C28`: a 16-entry word table indexed by `value >> 12`; the fraction
/// is the low 12 bits moved up to Q16; from `0xF000` the last entry.
pub fn lookup_shift12_q16(table: &[u16], value: u16) -> Result<u16, Refusal> {
    if table.len() != SHIFT12_ENTRIES {
        return Err(Refusal::TableLength);
    }
    if value >= SHIFT12_LAST_ENTRY_FROM {
        return Ok(table[SHIFT12_ENTRIES - 1]);
    }
    let index = usize::from(value >> SHIFT12_INDEX_SHIFT);
    // (value & 0xFFF) << 4 is at most 0xFFF0.
    let fraction =
        (value & ((1 << SHIFT12_INDEX_SHIFT) - 1)) << (FRACTION_BITS - SHIFT12_INDEX_SHIFT);
    Ok(interpolate_q16(table[index], table[index + 1], fraction))
}
