import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../../api/core";
import { FLOOR, delivery, deferred } from "../../test/workbenchFixtures";
import type { DeliveryErrorResponse, SessionChoice } from "../../types/workbench";
import { DeliveryView, MEASURED_SOURCES } from "./DeliveryView";

vi.mock("../../api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/core")>();
  return { ...actual, controlJson: vi.fn() };
});
const control = vi.mocked(controlJson);

afterEach(() => vi.clearAllMocks());

const RUNNING: SessionChoice = { sessions: [], revision: "ZS44A-20260920" };
const calls = () => control.mock.calls.map(([path]) => String(path));
const source = (value: string) =>
  fireEvent.change(screen.getByLabelText("Measured-AFR source"), { target: { value } });
const minLoad = () => screen.getByPlaceholderText("none");
const run = () => fireEvent.click(screen.getByRole("button", { name: "Run" }));

function answer(response: DeliveryErrorResponse | Error) {
  control.mockImplementation(() =>
    response instanceof Error ? Promise.reject(response) : Promise.resolve(response),
  );
}

describe("DeliveryView", () => {
  it("offers exactly the named measured-AFR sources and picks none", () => {
    render(<DeliveryView token="t" choice={RUNNING} />);
    const select = screen.getByLabelText<HTMLSelectElement>("Measured-AFR source");
    expect(select.value).toBe("");
    expect(
      Array.from(select.options)
        .slice(1)
        .map((o) => o.value),
    ).toEqual(MEASURED_SOURCES.map((s) => s.id));
  });

  it("runs only with a token, a choice and a source", () => {
    const { rerender } = render(<DeliveryView token="t" choice={RUNNING} />);
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    source("obd:O2_WR_LAMBDA_S1");
    expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
    rerender(<DeliveryView token="" choice={RUNNING} />);
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    rerender(<DeliveryView token="t" choice={{ sessions: [], revision: null }} />);
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    rerender(<DeliveryView token="t" choice={{ sessions: ["s1"], revision: null }} />);
    expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
  });

  it("says Running… while it waits and shows the result when it lands", async () => {
    const reply = deferred<DeliveryErrorResponse>();
    control.mockImplementation(() => reply.promise);
    render(<DeliveryView token="t" choice={RUNNING} />);
    source("obd:O2_WR_LAMBDA_S1");
    run();
    expect(await screen.findByRole("button", { name: "Running…" })).toBeDisabled();
    reply.resolve(delivery());
    expect(await screen.findByRole("button", { name: "Run" })).toBeEnabled();
    expect(screen.getByLabelText("Delivery error by rpm band")).toBeInTheDocument();
    expect(screen.getByText(/Band-averaged 1.90 % · pooled 2.10 % · n 20/)).toBeInTheDocument();
  });

  it("sends a minimum load only for a Mode 01 source, and keeps the typed value for later", async () => {
    answer(delivery());
    render(<DeliveryView token="t" choice={RUNNING} />);
    source("obd:O2_WR_LAMBDA_S1");
    fireEvent.change(minLoad(), { target: { value: " 35 " } });
    run();
    await screen.findByLabelText("Delivery error by rpm band");
    expect(calls()[0]).toContain("min_load=35");

    source("semantic:measured_afr_b1");
    expect(minLoad()).toBeDisabled();
    expect(minLoad()).toHaveValue("");
    run();
    await waitFor(() => expect(calls()).toHaveLength(2));
    expect(calls()[1]).not.toContain("min_load");

    source("obd:O2_WR_LAMBDA_S1");
    expect(minLoad()).toBeEnabled();
    expect(minLoad()).toHaveValue(" 35 ");
  });

  it("states a noise floor that was not measured, with no band drawn", async () => {
    answer(delivery({ noise_floor: { available: false, channel: "delivery_error_pct" } }));
    const { container } = render(<DeliveryView token="t" choice={RUNNING} />);
    source("obd:O2_WR_LAMBDA_S1");
    run();
    expect(
      await screen.findByText("Noise floor: not measured for this vehicle."),
    ).toBeInTheDocument();
    expect(container.querySelector("[data-floor]")).toBeNull();
    expect(screen.queryByText(/null run/)).not.toBeInTheDocument();
  });

  it("states a measured floor beside the result, with the conditions", async () => {
    answer(delivery({ noise_floor: { ...FLOOR, unit: "deg", min: null, max: null, sd: 0.5 } }));
    render(<DeliveryView token="t" choice={RUNNING} />);
    source("obd:O2_WR_LAMBDA_S1");
    run();
    const note = await screen.findByText(/Noise floor \(null run\)/);
    expect(note).toHaveTextContent("sd 0.50 deg, per-cell range not recorded");
    expect(screen.getByLabelText("Conditions")).toHaveTextContent("2 pooled");
    expect(screen.getByLabelText("Conditions")).toHaveTextContent("Measured");
    expect(screen.getByLabelText("Conditions")).toHaveTextContent("Load floor");
  });

  it("keeps the floor and the conditions with an empty result, beside the missing inputs", async () => {
    answer(
      delivery({
        available: false,
        unavailable_kind: "not_captured",
        n: 0,
        bands: [],
        gates: null,
        unavailable: [
          { session_id: "s1", kind: "not_captured", reason: "r", missing: ["obd:A", "obd:B"] },
          { session_id: "s2", kind: "not_captured", reason: "r", missing: ["obd:B"] },
        ],
      }),
    );
    render(<DeliveryView token="t" choice={RUNNING} />);
    source("obd:O2_WR_LAMBDA_S1");
    run();
    const empty = await screen.findByText("This capture's profile does not poll obd:A, obd:B");
    expect(empty).toHaveAttribute("data-empty", "not-captured");
    expect(screen.queryByLabelText("Delivery error by rpm band")).not.toBeInTheDocument();
    expect(screen.queryByRole("note", { name: "Pairing" })).not.toBeInTheDocument();
    expect(screen.getByText(/Noise floor \(null run\)/)).toBeInTheDocument();
    expect(screen.getByLabelText("Conditions")).toBeInTheDocument();
  });

  it("states the pairing and an unstable delay bracket with an available result", async () => {
    const base = delivery();
    answer(
      delivery({
        delay_sensitivity: {
          at: [{ delay_s: 0, n: 3, band_averaged_pct: -0.3, pooled_pct: -0.2 }],
          stable: false,
          reason: "the sign changes",
          spread_pct: 1,
        },
        pairing: { ...base.pairing },
      }),
    );
    render(<DeliveryView token="t" choice={RUNNING} />);
    source("obd:O2_WR_LAMBDA_S1");
    run();
    expect(await screen.findByRole("note", { name: "Pairing" })).toBeInTheDocument();
    expect(screen.getByRole("alert", { name: "Delay sensitivity" })).toHaveTextContent(
      "the sign changes (0 s: -0.30 %)",
    );
  });

  it("keeps the result while a rerun waits, and a refusal replaces it", async () => {
    const second = deferred<DeliveryErrorResponse>();
    let calls = 0;
    control.mockImplementation(() => {
      calls += 1;
      if (calls === 1) return Promise.resolve(delivery());
      if (calls === 2) return second.promise;
      return Promise.reject(
        new HttpError(
          409,
          JSON.stringify({ detail: { error: "analysis_busy", message: "try later" } }),
        ),
      );
    });
    render(<DeliveryView token="t" choice={RUNNING} />);
    source("obd:O2_WR_LAMBDA_S1");
    run();
    await screen.findByLabelText("Delivery error by rpm band");
    run();
    expect(await screen.findByRole("button", { name: "Running…" })).toBeInTheDocument();
    expect(screen.getByLabelText("Delivery error by rpm band")).toBeInTheDocument();
    second.resolve(delivery({ band_averaged_pct: 7.77 }));
    await screen.findByText(/Band-averaged 7.77 %/);
    run();
    expect(await screen.findByText("try later")).toBeInTheDocument();
    expect(screen.queryByLabelText("Delivery error by rpm band")).not.toBeInTheDocument();
  });

  it("says a refused token in the shared words", async () => {
    answer(new HttpError(401, ""));
    render(<DeliveryView token="t" choice={RUNNING} />);
    source("obd:O2_WR_LAMBDA_S1");
    run();
    expect(
      await screen.findByText("Not authorized — enter or check the control token."),
    ).toBeInTheDocument();
  });

  it("aborts the run in flight when it unmounts", async () => {
    control.mockImplementation(() => new Promise(() => {}));
    const { unmount } = render(<DeliveryView token="t" choice={RUNNING} />);
    source("obd:O2_WR_LAMBDA_S1");
    run();
    await screen.findByRole("button", { name: "Running…" });
    const signal = (control.mock.calls[0][2] as { signal: AbortSignal }).signal;
    expect(signal.aborted).toBe(false);
    unmount();
    expect(signal.aborted).toBe(true);
  });
});
