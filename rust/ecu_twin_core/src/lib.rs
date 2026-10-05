//! The ECU twin's computation core (E06.2), implementing the language-neutral
//! contract `docs/ecu-twin-computation-contract.md` /
//! `data/ecu_twin_contract.yaml`.
//!
//! The Python modules named there (`src/decode/ecm_lookup.py`,
//! `src/decode/target_phi_command.py`, `src/decode/fuel_comp_load.py`,
//! `src/analysis/ecu_state_models.py`) are the executable reference. This
//! crate is a second implementation, checked against the same vectors
//! (`tests/fixtures/ecu_twin/*.json`), never a replacement for them.
//!
//! Rules the code follows:
//!
//! * `no_std` (plus `alloc` for owned tables): no bus, disk, network or clock,
//!   and no dependency on Python objects.
//! * Every value has the width the contract declares (`u8`, `u16`, `u32`,
//!   `i64` for the signed idle-air input). Arithmetic that could leave a width
//!   is written as an explicit widening (`u32::from`), a saturating `min`, a
//!   `checked_*` whose failure is a proven-impossible invariant, or a named
//!   truncation (`truncate_u32`). Nothing depends on the build profile's
//!   overflow behaviour; the vectors run in debug and release.
//! * An input outside the contract's domain is a [`Refusal`]; an input the
//!   contract can evaluate but whose answer depends on unknown state is a
//!   named reason (`&'static str`), exactly as the reference names it.

#![no_std]
#![forbid(unsafe_code)]
#![warn(missing_docs)]

extern crate alloc;

pub mod fuel_comp_load;
pub mod lookup;
pub mod target_phi;
pub mod uprev;

/// The contract version these kernels implement (`contract_version` in
/// `data/ecu_twin_contract.yaml` and in every vector file).
pub const CONTRACT_VERSION: u32 = 1;
/// `src.decode.ecm_lookup.KERNEL_VERSION`: the lookup arithmetic.
pub const KERNEL_VERSION: u32 = 1;
/// `src.analysis.ecu_state_models.MODEL_VERSION`: the state models.
pub const STATE_MODEL_VERSION: u32 = 1;

/// Why the contract refuses an input. One kind per contract `refusals` entry;
/// [`Refusal::kind`] is the name the vectors and the Python mapping use.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refusal {
    /// A value outside its declared width or range.
    OutOfDomain,
    /// A fractional value where a whole raw count is required.
    NotWholeCount,
    /// An axis with no breakpoints.
    EmptyAxis,
    /// A 2-D table whose rows/columns do not match its axes.
    ShapeMismatch,
    /// A 1-D curve whose entries do not match its breakpoints.
    CurveShape,
    /// A fixed-size table of the wrong length.
    TableLength,
    /// An axis the routine divides by the gap of, not strictly rising.
    AxisNotRising,
    /// Curves that would make the load byte non-monotone in an input.
    NonMonotone,
    /// A signed idle-air word outside what the ECM can hold.
    IdleAirOutOfRange,
    /// A zero divisor the program never holds.
    DivisionByZero,
    /// Power-on with an unknown retained magic (unknown RAM is not a cold boot).
    RetainedMagicUnknown,
}

impl Refusal {
    /// The contract's name for this refusal.
    pub const fn kind(self) -> &'static str {
        match self {
            Refusal::OutOfDomain => "out_of_domain",
            Refusal::NotWholeCount => "not_whole_count",
            Refusal::EmptyAxis => "empty_axis",
            Refusal::ShapeMismatch => "shape_mismatch",
            Refusal::CurveShape => "curve_shape",
            Refusal::TableLength => "table_length",
            Refusal::AxisNotRising => "axis_not_rising",
            Refusal::NonMonotone => "non_monotone",
            Refusal::IdleAirOutOfRange => "idle_air_out_of_range",
            Refusal::DivisionByZero => "division_by_zero",
            Refusal::RetainedMagicUnknown => "retained_magic_unknown",
        }
    }
}

impl core::fmt::Display for Refusal {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str(self.kind())
    }
}

/// A whole 16-bit count from a wider signed integer (the binding's and the
/// vector runner's boundary conversion).
pub fn word(value: i64) -> Result<u16, Refusal> {
    u16::try_from(value).map_err(|_| Refusal::OutOfDomain)
}

/// A whole byte from a wider signed integer.
pub fn byte(value: i64) -> Result<u8, Refusal> {
    u8::try_from(value).map_err(|_| Refusal::OutOfDomain)
}

/// The low 32 bits of a value (the contract's named truncation, e.g. the
/// UpRev selection history `history << 4 | slot`).
pub const fn truncate_u32(value: u64) -> u32 {
    (value & 0xFFFF_FFFF) as u32
}
