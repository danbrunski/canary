//! The Fuel Comp load input `0xFFFF118A` (`src/decode/fuel_comp_load.py`;
//! contract operations `load_input_curves` and `fuel_comp_load`).

use alloc::vec::Vec;

use crate::lookup::{lookup_curve_q16, lookup_shift12_q16, SHIFT12_ENTRIES};
use crate::Refusal;

/// `0x03C582`: the area curve is read at four times the throttle counts.
pub const AREA_INPUT_MULTIPLIER: u32 = 4;
/// `0x03C466`/`0x03C468`: the area term's shift.
pub const AREA_TERM_SHIFT: u32 = 9;
/// `0x03C480`-`0x03C484`: the purge term's shift.
pub const PURGE_TERM_SHIFT: u32 = 11;
/// `0x03C48E`-`0x03C494`: the X at which the high fill curve takes over.
pub const X_SPLIT: u16 = 0x3C00;
/// `0x03C4A2`: below the split the low curve is read at `4 x X`.
pub const LOW_CURVE_MULTIPLIER: u16 = 4;
/// `0x03C4B8`: the byte is the curve's high byte.
pub const BYTE_SHIFT: u32 = 8;
/// Entries in the area curve and its axis.
pub const AREA_ENTRIES: usize = 32;
/// `w63FC` is a signed word.
pub const S16_MIN: i64 = -0x8000;

const WORD_MAX: u32 = 0xFFFF;

/// Every calibration value the chain reads, validated on construction.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoadInputCurves {
    area_breakpoints: Vec<u16>,
    area_values: Vec<u16>,
    fill_low: Vec<u16>,
    fill_high: Vec<u16>,
    rpm_multiplier: u8,
    reciprocal_numerator: u32,
    idle_air_cap: u16,
    limp_home_area: u16,
}

fn non_decreasing(values: &[u16]) -> bool {
    values.windows(2).all(|pair| pair[1] >= pair[0])
}

impl LoadInputCurves {
    /// The reference's checks, in its order: lengths (`table_length`), a
    /// non-zero numerator (`out_of_domain`), a strictly rising area axis
    /// (`axis_not_rising`), non-decreasing curves and the split's continuity
    /// (`non_monotone`). Word/byte widths hold by type.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        area_breakpoints: &[u16],
        area_values: &[u16],
        fill_low: &[u16],
        fill_high: &[u16],
        rpm_multiplier: u8,
        reciprocal_numerator: u32,
        idle_air_cap: u16,
        limp_home_area: u16,
    ) -> Result<Self, Refusal> {
        let lengths = [
            (area_breakpoints.len(), AREA_ENTRIES),
            (area_values.len(), AREA_ENTRIES),
            (fill_low.len(), SHIFT12_ENTRIES),
            (fill_high.len(), SHIFT12_ENTRIES),
        ];
        if lengths.iter().any(|(got, want)| got != want) {
            return Err(Refusal::TableLength);
        }
        if reciprocal_numerator == 0 {
            return Err(Refusal::OutOfDomain);
        }
        if area_breakpoints.windows(2).any(|pair| pair[1] <= pair[0]) {
            return Err(Refusal::AxisNotRising);
        }
        if !(non_decreasing(area_values) && non_decreasing(fill_low) && non_decreasing(fill_high)) {
            return Err(Refusal::NonMonotone);
        }
        let below_split =
            lookup_shift12_q16(fill_low, LOW_CURVE_MULTIPLIER * (X_SPLIT - 1)).expect("16 entries");
        if below_split > fill_high[0] {
            return Err(Refusal::NonMonotone);
        }
        Ok(LoadInputCurves {
            area_breakpoints: area_breakpoints.to_vec(),
            area_values: area_values.to_vec(),
            fill_low: fill_low.to_vec(),
            fill_high: fill_high.to_vec(),
            rpm_multiplier,
            reciprocal_numerator,
            idle_air_cap,
            limp_home_area,
        })
    }
}

/// One evaluation of the chain with every intermediate word.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LoadTrace {
    /// `0xFFFF118A`.
    pub byte: u8,
    /// `0xFFFF2136`.
    pub x: u16,
    /// `w4136`.
    pub area: u16,
    /// `w1124`.
    pub w1124: u16,
    /// `w116A`.
    pub w116a: u16,
}

/// `w116A`: `min(0xFFFF, numerator / min(0xFFFF, k x rpm))`, 0 for a zero
/// product (never a divide by zero).
pub fn rpm_reciprocal(rpm_word: u16, curves: &LoadInputCurves) -> u16 {
    // u8 x u16 fits u32.
    let product = (u32::from(curves.rpm_multiplier) * u32::from(rpm_word)).min(WORD_MAX);
    if product == 0 {
        return 0;
    }
    let quotient = curves.reciprocal_numerator / product;
    u16::try_from(quotient.min(WORD_MAX)).expect("saturated to a word")
}

/// `w4136`: the area curve at `min(0xFFFF, 4 x counts)`.
pub fn throttle_area(throttle_counts: u16, curves: &LoadInputCurves) -> u16 {
    let x = u16::try_from((AREA_INPUT_MULTIPLIER * u32::from(throttle_counts)).min(WORD_MAX))
        .expect("saturated to a word");
    lookup_curve_q16(&curves.area_values, &curves.area_breakpoints, x).expect("validated shape")
}

/// `w2C32 = clamp(area - w63FC, 0, 0xFFFF)` with `w63FC` signed and at most
/// the calibration cap.
pub fn effective_area(area: u16, idle_air: i64, curves: &LoadInputCurves) -> Result<u16, Refusal> {
    if idle_air < S16_MIN || idle_air > i64::from(curves.idle_air_cap) {
        return Err(Refusal::IdleAirOutOfRange);
    }
    let difference = i64::from(area) - idle_air;
    Ok(u16::try_from(difference.clamp(0, i64::from(WORD_MAX))).expect("clamped to a word"))
}

/// `FUN_0003c444` up to `0xFFFF2136`.
pub fn load_x(w1124: u16, purge_area: u16, w116a: u16) -> u16 {
    // Two words multiply into u32 without overflow.
    let first = (u32::from(w1124) * u32::from(w116a)) >> AREA_TERM_SHIFT;
    if first >= WORD_MAX {
        return 0xFFFF;
    }
    let second = (u32::from(purge_area) * u32::from(w116a)) >> PURGE_TERM_SHIFT;
    // first < 0xFFFF and second < 2^21: the sum fits u32.
    u16::try_from((first + second).min(WORD_MAX)).expect("saturated to a word")
}

/// `0xFFFF118A` from X: the split, the fill curve, the high byte.
pub fn load_byte(x: u16, curves: &LoadInputCurves) -> u8 {
    let word = if x < X_SPLIT {
        // x <= 0x3BFF, so 4x <= 0xEFFC.
        lookup_shift12_q16(&curves.fill_low, LOW_CURVE_MULTIPLIER * x)
    } else {
        lookup_shift12_q16(&curves.fill_high, x - X_SPLIT)
    }
    .expect("validated length");
    u8::try_from(word >> BYTE_SHIFT).expect("a word's high byte")
}

/// The byte the ECM writes for one set of raw inputs. `idle_air` is read
/// (and refused out of range) only when not in limp-home, as in the
/// reference.
pub fn reconstruct(
    throttle_counts: u16,
    rpm_word: u16,
    idle_air: i64,
    purge_area: u16,
    curves: &LoadInputCurves,
    limp_home: bool,
) -> Result<LoadTrace, Refusal> {
    let w116a = rpm_reciprocal(rpm_word, curves);
    let area = throttle_area(throttle_counts, curves);
    let w1124 = if limp_home {
        curves.limp_home_area
    } else {
        effective_area(area, idle_air, curves)?
    };
    let x = load_x(w1124, purge_area, w116a);
    Ok(LoadTrace {
        byte: load_byte(x, curves),
        x,
        area,
        w1124,
        w116a,
    })
}
