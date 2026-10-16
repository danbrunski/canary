import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../../api/core";
import { CAPTURE_ID, deferred, frame } from "../../test/workbenchFixtures";
import type { FrameResponse } from "../../types/workbench";
import { LogViewer } from "./LogViewer";

vi.mock("../../api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/core")>();
  return { ...actual, controlJson: vi.fn() };
});
const control = vi.mocked(controlJson);

const SPECS_KEY = "canary.tuning.logSpecs.v1";
const DEFAULT_SPECS = "obd:RPM,obd:MAF,obd:COMMANDED_EQUIV_RATIO,obd:O2_WR_LAMBDA_S1";

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.clearAllMocks());

const frameQueries = () =>
  control.mock.calls
    .map(([path]) => String(path))
    .filter((path) => path.includes("/frame"))
    .map((path) => new URL(path, "http://daemon").searchParams);
const table = () => screen.findByRole("table", { name: "Log table" });

function answer(reply: FrameResponse | Error) {
  control.mockImplementation(() =>
    reply instanceof Error ? Promise.reject(reply) : Promise.resolve(reply),
  );
}

/** A daemon that pages: each reply echoes the offset it was asked for, a
 *  `row_cap` of 3, and a value that names the offset it answers. */
function answerPaging(total = 10) {
  control.mockImplementation((path: string) => {
    const query = new URL(path, "http://daemon").searchParams;
    const offset = Number(query.get("offset"));
    const returned = Math.min(3, total - offset);
    const stamp = (i: number) => new Date(Date.parse("2026-09-20T12:00:00Z") + i * 1000);
    return Promise.resolve(
      frame({
        grid: query.get("grid") ?? "1s",
        offset,
        rows_total: total,
        rows_returned: returned,
        row_cap: 3,
        capped: offset + returned < total,
        timestamps: Array.from({ length: returned }, (_, i) => stamp(offset + i).toISOString()),
        values: {
          "obd:RPM": Array.from({ length: returned }, (_, i) => 1000 * (offset + i + 1)),
          "obd:MAF": Array.from({ length: returned }, () => 1),
        },
      }),
    );
  });
}

describe("LogViewer request", () => {
  it("asks for the stored channels, a 1 s grid, all regimes, from row 0", async () => {
    answer(frame());
    render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    await table();
    expect(control).toHaveBeenCalledTimes(1);
    expect(control.mock.calls[0][0]).toBe(
      `/control/sessions/${CAPTURE_ID}/frame?specs=obd%3ARPM%2Cobd%3AMAF%2Cobd%3ACOMMANDED_EQUIV_RATIO%2Cobd%3AO2_WR_LAMBDA_S1&grid=1s&regime=all&offset=0`,
    );
    expect(control.mock.calls[0][1]).toBe("t");
  });

  it("asks for nothing without a token", async () => {
    answer(frame());
    render(<LogViewer token="" sessionId={CAPTURE_ID} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(control).not.toHaveBeenCalled();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("applies the channel draft only on Apply, and a later mount reads it back", async () => {
    answer(frame());
    const first = render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    await table();
    expect(frameQueries()[0].get("specs")).toBe(DEFAULT_SPECS);
    const asked = frameQueries().length;
    fireEvent.change(screen.getByLabelText(/Channels/), { target: { value: " obd:A , ,obd:B " } });
    await new Promise((r) => setTimeout(r, 20));
    expect(frameQueries()).toHaveLength(asked);
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(frameQueries()).toHaveLength(asked + 1));
    expect(frameQueries()[asked].get("specs")).toBe("obd:A,obd:B");
    expect(JSON.parse(window.localStorage.getItem(SPECS_KEY) ?? "null")).toBe(" obd:A , ,obd:B ");
    first.unmount();

    control.mockClear();
    render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    await waitFor(() => expect(frameQueries()).toHaveLength(1));
    expect(frameQueries()[0].get("specs")).toBe("obd:A,obd:B");
    expect(screen.getByLabelText(/Channels/)).toHaveValue(" obd:A , ,obd:B ");
  });

  it("keeps the same storage key across mounts, writing the default on first use", async () => {
    answer(frame());
    render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    await table();
    expect(JSON.parse(window.localStorage.getItem(SPECS_KEY) ?? "null")).toBe(DEFAULT_SPECS);
  });

  it("requests no frame for an empty channel list and shows nothing under the form", async () => {
    answer(frame());
    render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    await table();
    const asked = frameQueries().length;
    fireEvent.change(screen.getByLabelText(/Channels/), { target: { value: " , " } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await new Promise((r) => setTimeout(r, 20));
    expect(frameQueries()).toHaveLength(asked);
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("offers the five grids and regimes, and sends the choice", async () => {
    answer(frame());
    render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    await table();
    const grid = screen.getByLabelText<HTMLSelectElement>("Grid");
    expect(Array.from(grid.options).map((o) => o.value)).toEqual([
      "100ms",
      "250ms",
      "500ms",
      "1s",
      "5s",
    ]);
    const regime = screen.getByLabelText<HTMLSelectElement>("Regime");
    expect(Array.from(regime.options).map((o) => o.value)).toEqual([
      "all",
      "closed_loop",
      "enrichment",
      "fuel_cut",
      "idle",
    ]);
    fireEvent.change(grid, { target: { value: "250ms" } });
    fireEvent.change(regime, { target: { value: "closed_loop" } });
    await waitFor(() => expect(frameQueries().at(-1)?.get("regime")).toBe("closed_loop"));
    expect(frameQueries().at(-1)?.get("grid")).toBe("250ms");
  });

  it("switches between the table and the visual presentation without asking again", async () => {
    answer(frame());
    render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    await table();
    const asked = control.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Visual" }));
    expect(screen.getByLabelText("Time-series strips")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Log table" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Visual" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Table" }));
    await table();
    expect(control).toHaveBeenCalledTimes(asked);
  });
});

describe("LogViewer paging", () => {
  it("pages by the row cap and starts again at row 0 when the view changes", async () => {
    answerPaging();
    const view = render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    await screen.findByText(/Showing rows 1–3 of 10/);
    expect(screen.queryByRole("button", { name: "Previous" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText(/Showing rows 4–6 of 10/);
    expect(frameQueries().at(-1)?.get("offset")).toBe("3");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText(/Showing rows 7–9 of 10/);
    expect(frameQueries().at(-1)?.get("offset")).toBe("6");
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    await screen.findByText(/Showing rows 4–6 of 10/);
    expect(frameQueries().at(-1)?.get("offset")).toBe("3");

    // Each change of view lands on row 0, and no offset-3 request is sent
    // for the new view on the way (the offset is derived, not reset later).
    const changes: Array<[() => void, Record<string, string>]> = [
      [
        () => fireEvent.change(screen.getByLabelText("Grid"), { target: { value: "5s" } }),
        { grid: "5s", regime: "all" },
      ],
      [
        () => fireEvent.change(screen.getByLabelText("Regime"), { target: { value: "idle" } }),
        { grid: "5s", regime: "idle" },
      ],
      [
        () => {
          fireEvent.change(screen.getByLabelText(/Channels/), { target: { value: "obd:RPM" } });
          fireEvent.click(screen.getByRole("button", { name: "Apply" }));
        },
        { grid: "5s", regime: "idle", specs: "obd:RPM" },
      ],
    ];
    for (const [change, expected] of changes) {
      // Page 2 (offset 3) is on screen here, for the first change and each after it.
      await waitFor(() => expect(frameQueries().at(-1)?.get("offset")).toBe("3"));
      const before = frameQueries().length;
      change();
      await waitFor(() => expect(frameQueries().length).toBeGreaterThan(before));
      const sent = frameQueries().slice(before);
      expect(sent.map((q) => q.get("offset"))).toEqual(["0"]);
      for (const [key, value] of Object.entries(expected)) expect(sent[0].get(key)).toBe(value);
      await screen.findByText(/Showing rows 1–3 of 10/);
      fireEvent.click(screen.getByRole("button", { name: "Next" }));
      await screen.findByText(/Showing rows 4–6 of 10/);
    }

    const before = frameQueries().length;
    view.rerender(<LogViewer token="t" sessionId="_capture-tuning-2026-09-10T120000" />);
    await waitFor(() => expect(frameQueries().length).toBeGreaterThan(before));
    expect(
      frameQueries()
        .slice(before)
        .map((q) => q.get("offset")),
    ).toEqual(["0"]);
  });

  it("goes back no further than row 0", async () => {
    // A page whose offset is less than the cap: Previous lands on 0, not negative.
    control.mockImplementation((path: string) => {
      const offset = Number(new URL(path, "http://daemon").searchParams.get("offset"));
      return Promise.resolve(
        frame({ offset, capped: offset === 0, rows_total: 10, row_cap: 5, rows_returned: 2 }),
      );
    });
    render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    await screen.findByText(/Showing rows 1–2 of 10/);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText(/Showing rows 3–4 of 10/);
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    await waitFor(() => expect(frameQueries().at(-1)?.get("offset")).toBe("0"));
  });
});

describe("LogViewer stale results and refusals", () => {
  it("keeps the old frame under Loading… and drops a late reply from a superseded view", async () => {
    const slow = deferred<FrameResponse>();
    const fast = deferred<FrameResponse>();
    let calls = 0;
    control.mockImplementation(() => {
      calls += 1;
      if (calls === 1) {
        return Promise.resolve(
          frame({ values: { "obd:RPM": [111, 222, 333], "obd:MAF": [1, 2, 3] } }),
        );
      }
      return (calls === 2 ? slow : fast).promise;
    });
    render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    expect(await table()).toHaveTextContent("333");

    fireEvent.change(screen.getByLabelText("Grid"), { target: { value: "100ms" } });
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    // The old page is still shown, labelled as loading, not passed off as current.
    expect(screen.getByRole("table", { name: "Log table" })).toHaveTextContent("333");
    const firstSignal = (control.mock.calls[1][2] as { signal: AbortSignal }).signal;

    fireEvent.change(screen.getByLabelText("Grid"), { target: { value: "5s" } });
    expect(firstSignal.aborted).toBe(true);
    fast.resolve(
      frame({ grid: "5s", values: { "obd:RPM": [555, 666, 777], "obd:MAF": [1, 2, 3] } }),
    );
    await waitFor(() =>
      expect(screen.getByRole("table", { name: "Log table" })).toHaveTextContent("777"),
    );
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();

    // The superseded 100ms reply lands last and must not overwrite the 5s view.
    slow.resolve(
      frame({ grid: "100ms", values: { "obd:RPM": [9991, 9992, 9993], "obd:MAF": [1, 2, 3] } }),
    );
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.getByRole("table", { name: "Log table" })).toHaveTextContent("777");
    expect(screen.queryByText("9993")).not.toBeInTheDocument();
    expect(screen.getByText(/grid 5s/)).toBeInTheDocument();
  });

  it("replaces the frame with a refusal and brings it back with the next success", async () => {
    control.mockImplementation((path: string) =>
      path.includes("regime=fuel_cut")
        ? Promise.reject(
            new HttpError(
              409,
              JSON.stringify({ detail: { error: "analysis_busy", message: "frame busy" } }),
            ),
          )
        : Promise.resolve(frame()),
    );
    render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    await table();
    fireEvent.change(screen.getByLabelText("Regime"), { target: { value: "fuel_cut" } });
    expect(await screen.findByText("frame busy")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Log table" })).not.toBeInTheDocument();
    expect(screen.queryByText(/cells dropped by the freshness bound/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Regime"), { target: { value: "all" } });
    await table();
    expect(screen.queryByText("frame busy")).not.toBeInTheDocument();
  });

  it("says a deferred capture with the laptop path, a dead daemon and a refused token each in their words", async () => {
    answer(
      new HttpError(
        409,
        JSON.stringify({
          detail: {
            error: "analysis_deferred",
            profile: "tuning",
            message: "m",
            laptop_path: "ssh canary@canary.local",
          },
        }),
      ),
    );
    const first = render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    expect(await screen.findByText(/is recording/)).toHaveTextContent(
      "Deferred while tuning is recording",
    );
    expect(screen.getByText("ssh canary@canary.local")).toBeInTheDocument();
    first.unmount();

    const { NetworkError } = await import("../../api/core");
    answer(new NetworkError("/x", new TypeError("x")));
    const second = render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    expect(await screen.findByText(/Can't reach the daemon/)).toBeInTheDocument();
    second.unmount();

    answer(new HttpError(401, ""));
    render(<LogViewer token="t" sessionId={CAPTURE_ID} />);
    expect(
      await screen.findByText("Not authorized — enter or check the control token."),
    ).toBeInTheDocument();
  });
});
