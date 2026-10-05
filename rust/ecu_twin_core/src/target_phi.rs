//! The target-equivalence-ratio command (`src/decode/target_phi_command.py`;
//! contract operations `target_command` and `x_from_bfs`).

use crate::lookup::{lookup_2d, LookupResult, Table2d};
use crate::Refusal;

/// The reason a lookup inside the gate's hysteresis band carries.
pub const GATE_STATE_UNKNOWN: &str = "enrichment_gate_state_unknown";
/// Largest flag bit (the flag word is at most 32 bits).
pub const MAX_FLAG_BIT: u8 = 31;
/// Largest cell-to-word shift (a word shifted 16 still fits u32).
pub const MAX_CELL_TO_WORD_SHIFT: u8 = 16;

/// Which way the load input was chosen.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Branch {
    /// The load input as read.
    Ordinary,
    /// The forced-column flag replaced it with all ones.
    Override,
}

impl Branch {
    /// The contract's name.
    pub const fn name(self) -> &'static str {
        match self {
            Branch::Ordinary => "ordinary",
            Branch::Override => "override",
        }
    }
}

/// The decoded constants between the inputs and the command word, validated
/// once.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CommandConstants {
    flag_bit: u8,
    forced_load: u16,
    floor_cell: u16,
    threshold_cell: u16,
    hysteresis_cells: u16,
    cell_to_word_shift: u8,
}

impl CommandConstants {
    /// Refuses (`out_of_domain`) a flag bit past 31, a load width other than 8
    /// or 16 bits, and a shift past 16 -- the reference's order.
    pub fn new(
        flag_bit: u8,
        load_bits: u8,
        floor_cell: u16,
        threshold_cell: u16,
        hysteresis_cells: u16,
        cell_to_word_shift: u8,
    ) -> Result<Self, Refusal> {
        if flag_bit > MAX_FLAG_BIT {
            return Err(Refusal::OutOfDomain);
        }
        let forced_load = match load_bits {
            8 => 0xFF,
            16 => 0xFFFF,
            _ => return Err(Refusal::OutOfDomain),
        };
        if cell_to_word_shift > MAX_CELL_TO_WORD_SHIFT {
            return Err(Refusal::OutOfDomain);
        }
        Ok(CommandConstants {
            flag_bit,
            forced_load,
            floor_cell,
            threshold_cell,
            hysteresis_cells,
            cell_to_word_shift,
        })
    }
}

/// One evaluated tuple.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CommandResult {
    /// The branch taken.
    pub branch: Branch,
    /// The lookup and its trace.
    pub lookup: LookupResult,
    /// The lookup value.
    pub cell: u16,
    /// The gated command word; `None` inside the hysteresis band.
    pub word: Option<u32>,
    /// Why there is no word.
    pub reason: Option<&'static str>,
}

/// One tuple of raw inputs to the gated command word.
pub fn target_command(
    table: &Table2d,
    constants: &CommandConstants,
    rpm_raw: u16,
    load_raw: u16,
    flag_raw: u32,
) -> CommandResult {
    // flag_bit <= 31 (validated), so the shift is in range.
    let forced = (flag_raw >> constants.flag_bit) & 1 == 1;
    let load = if forced {
        constants.forced_load
    } else {
        load_raw
    };
    let lookup = lookup_2d(table, rpm_raw, load);
    let branch = if forced {
        Branch::Override
    } else {
        Branch::Ordinary
    };
    let cell = lookup.value;
    // The band's lower edge may be below zero: compare in i32.
    let low = i32::from(constants.threshold_cell) - i32::from(constants.hysteresis_cells);
    if low <= i32::from(cell) && cell < constants.threshold_cell {
        return CommandResult {
            branch,
            lookup,
            cell,
            word: None,
            reason: Some(GATE_STATE_UNKNOWN),
        };
    }
    let gated = if cell >= constants.threshold_cell {
        cell
    } else {
        constants.floor_cell
    };
    // A u16 shifted at most 16 fits u32.
    let word = u32::from(gated) << constants.cell_to_word_shift;
    CommandResult {
        branch,
        lookup,
        cell,
        word: Some(word),
        reason: None,
    }
}

/// `min(cap, (bfs << shift) / scaler)`, an unsigned truncating divide.
pub fn x_from_bfs(bfs: u16, scaler: u16, shift: u8, cap: u16) -> Result<u16, Refusal> {
    if u32::from(shift) > u32::from(MAX_CELL_TO_WORD_SHIFT) {
        return Err(Refusal::OutOfDomain);
    }
    let shifted = u32::from(bfs) << shift;
    let quotient = shifted
        .checked_div(u32::from(scaler))
        .ok_or(Refusal::DivisionByZero)?;
    // The minimum with a u16 cap is a u16.
    Ok(u16::try_from(quotient.min(u32::from(cap))).expect("bounded by cap"))
}
