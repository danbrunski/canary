//! UpRev map selection, the one stateful path in the trial
//! (`src/analysis/ecu_state_models.py`; contract operations `uprev_step`,
//! `uprev_power_on`, `loaded_slot`).
//!
//! State is explicit and nullable: `None` is *unobserved*, never a default.
//! A step whose result depends on an unknown field returns the reference's
//! named reason instead of a state.

use crate::{truncate_u32, Refusal};

/// The value of the magic long once UpRev's init has run.
pub const UPREV_MAGIC: u32 = 0xAA55_55AA;
/// Bits of `0xFFFF2EE0` the selector watches (`0x0DE37E`).
pub const UPREV_INPUT_MASK: u8 = 0x0A;
/// Debounce value meaning "held".
pub const UPREV_HOLD: u8 = 0x08;
/// Input bit that steps the selector.
pub const UPREV_STEP: u8 = 0x02;
/// Slots a commit can select.
pub const UPREV_SLOTS: u8 = 5;
/// The commit sets the countdown to `selector << 6`.
pub const UPREV_COUNTDOWN_SHIFT: u32 = 6;

/// UpRev's RAM block at `0xFFFFB000`; `None` is unobserved.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UpRevState {
    /// Countdown word `B000`.
    pub countdown: Option<u16>,
    /// Debounce byte `B002` (last cycle's masked input).
    pub debounce: Option<u8>,
    /// Selector byte `B003`.
    pub selector: Option<u8>,
    /// Magic long `B004`.
    pub magic: Option<u32>,
    /// Committed map byte `B008`.
    pub active_map: Option<u8>,
    /// Selection history long `B00C`.
    pub history: Option<u32>,
}

/// One evaluated cycle.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UpRevCycle {
    /// The state after the cycle.
    pub state: UpRevState,
    /// Slot copied this cycle (a valid commit).
    pub copied: Option<u8>,
    /// `0xFFFF4858` bit 0 as written this cycle (`None`: not written).
    pub indicator: Option<bool>,
}

/// A step's answer: the cycle, or the reason it is unevaluable.
pub type Step<T> = Result<T, &'static str>;

/// `0x0DE1A4`: with the magic absent, write it, zero the block and copy slot
/// 1; with it present, nothing. An unknown retained magic is refused.
pub fn uprev_power_on(retained: UpRevState) -> Result<(UpRevState, Option<u8>), Refusal> {
    match retained.magic {
        None => Err(Refusal::RetainedMagicUnknown),
        Some(UPREV_MAGIC) => Ok((retained, None)),
        Some(_) => Ok((
            UpRevState {
                countdown: Some(0),
                debounce: Some(0),
                selector: Some(0),
                magic: Some(UPREV_MAGIC),
                active_map: Some(0),
                history: Some(0),
            },
            Some(1),
        )),
    }
}

/// `0x0DE358`, one cycle (see the reference's docstring for the decoded
/// order: countdown, then the debounce-gated selector, then the debounce).
pub fn uprev_step(state: UpRevState, inputs_2ee0: u8) -> Step<UpRevCycle> {
    let inputs = inputs_2ee0 & UPREV_INPUT_MASK;
    let Some(mut countdown) = state.countdown else {
        return Err("uprev_countdown_unknown");
    };
    let Some(debounce) = state.debounce else {
        return Err("uprev_debounce_unknown");
    };
    let mut indicator = None;
    if countdown != 0 {
        countdown -= 1; // non-zero, so no underflow
        indicator = Some(countdown & 0x20 != 0);
    }
    let (mut selector, mut active, mut history, mut copied) =
        (state.selector, state.active_map, state.history, None);
    if debounce == 0 {
        selector = Some(0);
    } else if debounce == UPREV_HOLD {
        if inputs & UPREV_STEP != 0 {
            let Some(current) = selector else {
                return Err("uprev_selector_unknown");
            };
            selector = Some(current.wrapping_add(1)); // a byte: 0xFF steps to 0
        } else if inputs == 0 {
            let Some(current) = selector else {
                return Err("uprev_selector_unknown");
            };
            // A byte shifted 6 is at most 0x3FC0: a word, no wrap.
            countdown = u16::from(current) << UPREV_COUNTDOWN_SHIFT;
            let slot = current & 0x0F;
            history = history.map(|h| truncate_u32((u64::from(h) << 4) | u64::from(slot)));
            if (1..=UPREV_SLOTS).contains(&slot) {
                active = Some(slot);
                copied = Some(slot);
            }
        }
    }
    Ok(UpRevCycle {
        state: UpRevState {
            countdown: Some(countdown),
            debounce: Some(inputs),
            selector,
            magic: state.magic,
            active_map: active,
            history,
        },
        copied,
        indicator,
    })
}

/// Where the RAM copy's slot came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SlotSource {
    /// A committed 1..5.
    Committed,
    /// Slot 1 copied by the last init.
    InitDefault,
}

impl SlotSource {
    /// The contract's name.
    pub const fn name(self) -> &'static str {
        match self {
            SlotSource::Committed => "committed",
            SlotSource::InitDefault => "init_default",
        }
    }
}

/// The slot whose bytes the last copy put at `0xFFFFB040`.
pub fn loaded_slot(magic: Option<u32>, active_map: Option<u8>) -> Step<(u8, SlotSource)> {
    let (Some(magic), Some(active)) = (magic, active_map) else {
        return Err("uprev_state_unobserved");
    };
    if magic != UPREV_MAGIC {
        return Err("uprev_not_initialised");
    }
    if (1..=UPREV_SLOTS).contains(&active) {
        return Ok((active, SlotSource::Committed));
    }
    if active == 0 {
        return Ok((1, SlotSource::InitDefault));
    }
    Err("uprev_active_map_invalid")
}
