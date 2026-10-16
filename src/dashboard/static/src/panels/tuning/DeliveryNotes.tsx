import type { DelaySensitivity, DeliveryPairing } from "../../types/workbench";

/** What "same instant" means for these numbers, stated rather than implied. */
export function PairingNote({ pairing }: { pairing: DeliveryPairing }) {
  const delay = pairing.transport_delay_s;
  const bracket = pairing.transport_delay_bracket_s;
  return (
    <p className="m-0 mb-1 text-xs text-fg-dim" role="note" aria-label="Pairing">
      Target <code>{pairing.target_spec}</code> paired with <code>{pairing.measured_spec}</code> by
      acquisition time within {(pairing.max_separation_s * 1000).toFixed(0)} ms
      {pairing.transport_delay_modelled && delay != null
        ? `, with the λ read nearest t + ${(delay * 1000).toFixed(0)} ms (transport delay${
            bracket ? `, ${bracket[0]}–${bracket[1]} s supported` : ""
          })`
        : "; transport delay is not modelled on this route"}
      ; only a target steady within {pairing.target_step_tolerance} λ for{" "}
      {pairing.steady_target_dwell_s} s and {pairing.post_fuel_cut_holdoff_s} s clear of fuel cut
      counts.
    </p>
  );
}

/** A delay-bracket result that does not hold is said, never smoothed over. */
export function DelayStability({ sensitivity }: { sensitivity: DelaySensitivity | null }) {
  if (!sensitivity || sensitivity.stable) return null;
  return (
    <p className="m-0 mb-1 text-xs text-accent-2" role="alert" aria-label="Delay sensitivity">
      Unstable across the transport-delay bracket: {sensitivity.reason} (
      {sensitivity.at
        .map(
          (row) =>
            `${row.delay_s} s: ${row.band_averaged_pct == null ? "none" : `${row.band_averaged_pct.toFixed(2)} %`}`,
        )
        .join(", ")}
      ).
    </p>
  );
}
