import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../../api/core";
import { trimCell, trimSurface } from "../../test/workbenchFixtures";
import type { SessionChoice, TrimSurfaceResponse } from "../../types/workbench";
import { TrimView } from "./TrimView";

vi.mock("../../api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/core")>();
  return { ...actual, controlJson: vi.fn() };
});
const control = vi.mocked(controlJson);

afterEach(() => vi.clearAllMocks());

const RUNNING: SessionChoice = { sessions: [], revision: "ZS44A-20260920" };
const run = () => fireEvent.click(screen.getByRole("button", { name: "Run" }));

function answer(response: TrimSurfaceResponse | Error) {
  control.mockImplementation(() =>
    response instanceof Error ? Promise.reject(response) : Promise.resolve(response),
  );
}

describe("TrimView", () => {
  it("runs only with a token and a choice, and shows no bank or split before a result", () => {
    const { rerender } = render(<TrimView token="t" choice={{ sessions: [], revision: null }} />);
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    rerender(<TrimView token="" choice={RUNNING} />);
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    rerender(<TrimView token="t" choice={RUNNING} />);
    expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
    expect(screen.queryByLabelText("Bank")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Split")).not.toBeInTheDocument();
  });

  it("asks for the trim surface of the chosen revision", async () => {
    answer(trimSurface());
    render(<TrimView token="t" choice={RUNNING} />);
    run();
    await screen.findByRole("table", { name: "Trim surface" });
    expect(control).toHaveBeenCalledWith(
      "/analysis/trim-surface?revision=ZS44A-20260920",
      "t",
      expect.anything(),
    );
  });

  it("shows the pooled surface per bank, or one session's, and warns that pooling hides", async () => {
    const one = trimSurface();
    answer(
      trimSurface({
        banks: {
          b1: { n: 40, cells: one.banks.b1.cells },
          b2: { n: 9, cells: [trimCell(4, false, 9)] },
        },
      }),
    );
    const { container } = render(<TrimView token="t" choice={RUNNING} />);
    run();
    const grid = await screen.findByRole("table", { name: "Trim surface" });
    expect(grid).toHaveTextContent("-5");
    expect(screen.getByText(/A pooled surface can hide a per-session effect/)).toBeInTheDocument();
    const bank = screen.getByLabelText<HTMLSelectElement>("Bank");
    expect(Array.from(bank.options).map((o) => o.textContent)).toEqual(["Bank 1", "Bank 2"]);
    fireEvent.change(bank, { target: { value: "b2" } });
    expect(screen.getByRole("table", { name: "Trim surface" })).toHaveTextContent("4");
    // A bank the chosen session lacks has no cells to draw.
    fireEvent.change(screen.getByLabelText("Split"), { target: { value: "2026-09-18T120000" } });
    expect(screen.getByText("No cells.")).toBeInTheDocument();
    expect(container.querySelector('[data-sparse="true"]')).toBeNull();
  });

  it("greys a sparse cell of one session's split, and offers each session", async () => {
    answer(trimSurface());
    const { container } = render(<TrimView token="t" choice={RUNNING} />);
    run();
    await screen.findByRole("table", { name: "Trim surface" });
    const split = screen.getByLabelText<HTMLSelectElement>("Split");
    expect(Array.from(split.options).map((o) => o.value)).toEqual(["pooled", "2026-09-18T120000"]);
    expect(container.querySelector('[data-sparse="true"]')).toBeNull();
    fireEvent.change(split, { target: { value: "2026-09-18T120000" } });
    expect(container.querySelector('[data-sparse="true"]')).not.toBeNull();
  });

  it("names why an empty surface is empty and keeps the conditions", async () => {
    answer(
      trimSurface({
        available: false,
        unavailable_kind: "gates_removed_everything",
        gates: { started: 5, filtered: { closed_loop: 5 }, remaining: 0 },
        banks: {},
        per_session: [],
      }),
    );
    render(<TrimView token="t" choice={RUNNING} />);
    run();
    const empty = await screen.findAllByText("0 of 5 samples passed: closed loop removed 5");
    expect(empty[0]).toHaveAttribute("data-empty", "gates");
    expect(screen.queryByRole("table", { name: "Trim surface" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Conditions")).toBeInTheDocument();
  });

  it("shows a refusal in its own words and no surface", async () => {
    answer(
      new HttpError(409, JSON.stringify({ detail: { error: "analysis_busy", message: "busy" } })),
    );
    render(<TrimView token="t" choice={RUNNING} />);
    run();
    expect(await screen.findByText("busy")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Trim surface" })).not.toBeInTheDocument();
  });
});
