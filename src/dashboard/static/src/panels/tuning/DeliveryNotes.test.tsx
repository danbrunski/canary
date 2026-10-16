import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { delivery } from "../../test/workbenchFixtures";
import type { DelaySensitivity } from "../../types/workbench";
import { DelayStability, PairingNote } from "./DeliveryNotes";

const pairing = delivery().pairing;

describe("PairingNote", () => {
  it("states the targets, the separation in ms and the steadiness rule", () => {
    render(<PairingNote pairing={pairing} />);
    const note = screen.getByRole("note", { name: "Pairing" });
    expect(note).toHaveTextContent(
      "Target obd:COMMANDED_EQUIV_RATIO paired with obd:O2_WR_LAMBDA_S1 by acquisition time " +
        "within 250 ms; transport delay is not modelled on this route; only a target steady " +
        "within 0.01 λ for 0.6 s and 6 s clear of fuel cut counts.",
    );
  });

  it("states a modelled delay with its bracket, or without one when none is recorded", () => {
    const modelled = { ...pairing, transport_delay_modelled: true, transport_delay_s: 0.1 };
    const first = render(
      <PairingNote pairing={{ ...modelled, transport_delay_bracket_s: [0, 0.5] }} />,
    );
    expect(screen.getByRole("note", { name: "Pairing" })).toHaveTextContent(
      "with the λ read nearest t + 100 ms (transport delay, 0–0.5 s supported)",
    );
    first.unmount();
    render(<PairingNote pairing={{ ...modelled, transport_delay_bracket_s: null }} />);
    expect(screen.getByRole("note", { name: "Pairing" })).toHaveTextContent(
      "with the λ read nearest t + 100 ms (transport delay)",
    );
  });

  it("does not claim a delay it was not given", () => {
    render(
      <PairingNote
        pairing={{ ...pairing, transport_delay_modelled: true, transport_delay_s: null }}
      />,
    );
    expect(screen.getByRole("note", { name: "Pairing" })).toHaveTextContent(
      "transport delay is not modelled on this route",
    );
  });
});

describe("DelayStability", () => {
  const unstable: DelaySensitivity = {
    at: [
      { delay_s: 0.1, n: 20, band_averaged_pct: 0.7, pooled_pct: 0.6 },
      { delay_s: 0, n: 3, band_averaged_pct: null, pooled_pct: null },
    ],
    stable: false,
    reason: "the sign changes",
    spread_pct: 1.4,
  };

  it("says nothing for a result that holds, or when no bracket was run", () => {
    const first = render(<DelayStability sensitivity={{ ...unstable, stable: true }} />);
    expect(first.container).toBeEmptyDOMElement();
    const second = render(<DelayStability sensitivity={null} />);
    expect(second.container).toBeEmptyDOMElement();
  });

  it("lists each delay's result, and 'none' where a delay produced no estimate", () => {
    render(<DelayStability sensitivity={unstable} />);
    expect(screen.getByRole("alert", { name: "Delay sensitivity" })).toHaveTextContent(
      "Unstable across the transport-delay bracket: the sign changes (0.1 s: 0.70 %, 0 s: none).",
    );
  });
});
