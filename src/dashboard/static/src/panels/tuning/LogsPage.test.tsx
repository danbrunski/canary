import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../../api/core";
import { describeRefusal, gateSentence } from "../../lib/workbenchApi";
import { CAPTURE_ID, LOGS, frame, tuningContext as context } from "../../test/workbenchFixtures";
import type { FrameResponse } from "../../types/workbench";
import { toCsv } from "../../util/csv";
import { formatClockDuration } from "../../util/time";
import { FrameTable } from "./FrameTable";
import { FrameVisual } from "./FrameVisual";
import { LogBrowser } from "./LogBrowser";
import { LogViewer } from "./LogViewer";
import * as page from "./LogsPage";
import { LogsPage } from "./LogsPage";

vi.mock("../../api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/core")>();
  return { ...actual, controlJson: vi.fn() };
});
const control = vi.mocked(controlJson);

function route(frameResponse: FrameResponse | Error) {
  control.mockImplementation((path: string) => {
    if (path.startsWith("/control/logs")) return Promise.resolve(LOGS);
    if (path.includes("/frame")) {
      return frameResponse instanceof Error
        ? Promise.reject(frameResponse)
        : Promise.resolve(frameResponse);
    }
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.clearAllMocks());

describe("LogsPage browser", () => {
  it("with no log selected shows the browser and nothing else", async () => {
    route(frame());
    render(<LogsPage token="t" context={context()} />);
    const table = await screen.findByRole("table", { name: "Capture browser" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("operator stop");
    expect(rows[0]).toHaveTextContent("ZS44A-20260920");
    expect(rows[0]).toHaveTextContent("12:34");
    expect(rows[1]).toHaveTextContent("daemon shutdown");
    expect(rows[1]).toHaveTextContent("flash inside");
    expect(screen.queryByRole("table", { name: "Log table" })).not.toBeInTheDocument();
    expect(control.mock.calls.some(([path]) => String(path).includes("/frame"))).toBe(false);
  });

  it("selecting a log sets the group's selectedLogId", async () => {
    route(frame());
    const picked: Array<string | null> = [];
    render(<LogsPage token="t" context={context({ setSelectedLogId: (id) => picked.push(id) })} />);
    fireEvent.click(await screen.findByText("tuning-did"));
    expect(picked).toEqual(["_capture-tuning-2026-09-10T120000"]);
  });

  it("toggles ordinary sessions into the listing", async () => {
    route(frame());
    render(<LogsPage token="t" context={context()} />);
    await screen.findByRole("table", { name: "Capture browser" });
    fireEvent.click(screen.getByLabelText("Include ordinary sessions"));
    await waitFor(() =>
      expect(control).toHaveBeenCalledWith(
        "/control/logs?include_sessions=true",
        "t",
        expect.anything(),
      ),
    );
  });
});

describe("LogsPage viewer (direct route with a persisted log)", () => {
  const selected = context({ selectedLogId: "_capture-tuning-2026-09-20T120000" });

  it("opens the table presentation with units, freshness and sorting", async () => {
    route(frame());
    render(<LogsPage token="t" context={selected} />);
    const table = await screen.findByRole("table", { name: "Log table" });
    expect(within(table).getByText("obd:RPM (rpm)")).toBeInTheDocument();
    expect(screen.getByText(/2 of 5 cells dropped by the freshness bound/)).toBeInTheDocument();
    const firstCell = () => within(table).getAllByRole("row")[1].querySelectorAll("td")[1];
    expect(firstCell()).toHaveTextContent("800");
    fireEvent.click(within(table).getByRole("button", { name: /obd:RPM/ }));
    fireEvent.click(within(table).getByRole("button", { name: /obd:RPM/ }));
    expect(firstCell()).toHaveTextContent("3000");
  });

  it("opens the visual presentation with tag markers on shared strips", async () => {
    route(frame());
    const { container } = render(<LogsPage token="t" context={selected} />);
    await screen.findByRole("table", { name: "Log table" });
    fireEvent.click(screen.getByRole("button", { name: "Visual" }));
    expect(screen.getByLabelText("Time-series strips")).toBeInTheDocument();
    expect(container.querySelectorAll('[data-marker="tag"]').length).toBe(2);
    expect(screen.getByLabelText("Scatter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Heat map" }));
    expect(screen.getByLabelText("Heat map")).toBeInTheDocument();
  });

  it("warns and marks a revision boundary inside the log", async () => {
    route(
      frame({
        revision_boundaries: [{ revision: "B", flashed_at: "2026-09-20T12:00:01+00:00" }],
      }),
    );
    const { container } = render(<LogsPage token="t" context={selected} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/mix two\s+calibrations/);
    fireEvent.click(screen.getByRole("button", { name: "Visual" }));
    expect(container.querySelectorAll('[data-marker="flash"]').length).toBe(2);
  });

  it("states a capped page and names channels the capture never polled", async () => {
    route(
      frame({
        capped: true,
        rows_total: 50000,
        missing: [
          {
            spec: "obd:INTAKE_TEMP",
            reason: "This capture's profile does not poll obd:INTAKE_TEMP",
          },
        ],
      }),
    );
    render(<LogsPage token="t" context={selected} />);
    expect(await screen.findByText(/Showing rows 1–3 of 50,000/)).toBeInTheDocument();
    expect(
      screen.getByText("This capture's profile does not poll obd:INTAKE_TEMP"),
    ).toBeInTheDocument();
  });

  it("defers while a capture records, with the laptop path", async () => {
    route(
      new HttpError(
        409,
        JSON.stringify({
          detail: {
            error: "analysis_deferred",
            profile: "tuning",
            message: "analysis is deferred while 'tuning' is recording",
            laptop_path: "ssh canary@canary.local 'tar -cf - -C /data s' | tar -xf -",
          },
        }),
      ),
    );
    render(<LogsPage token="t" context={selected} />);
    expect(await screen.findByText(/is recording/)).toHaveTextContent(
      "Deferred while tuning is recording",
    );
    expect(screen.getByText(/tar -cf -/)).toBeInTheDocument();
  });

  it("sends the regime filter to the frame endpoint", async () => {
    route(frame());
    render(<LogsPage token="t" context={selected} />);
    await screen.findByRole("table", { name: "Log table" });
    fireEvent.change(screen.getByLabelText("Regime"), { target: { value: "fuel_cut" } });
    await waitFor(() =>
      expect(control.mock.calls.some(([path]) => String(path).includes("regime=fuel_cut"))).toBe(
        true,
      ),
    );
  });
});

describe("LogsPage composition (R19)", () => {
  const selected = context({ selectedLogId: CAPTURE_ID });

  it("re-exports the components it was split into from their old path", () => {
    expect(page.LogBrowser).toBe(LogBrowser);
    expect(page.LogViewer).toBe(LogViewer);
    expect(page.FrameTable).toBe(FrameTable);
    expect(page.FrameVisual).toBe(FrameVisual);
  });

  it("without a token asks for nothing and mounts no viewer", async () => {
    route(frame());
    render(<LogsPage token="" context={selected} />);
    expect(screen.getByText("Enter the control token to browse logs.")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 20));
    expect(control).not.toHaveBeenCalled();
    expect(screen.queryByRole("table", { name: "Log table" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Channels/)).not.toBeInTheDocument();
  });

  it("opens the viewer for the selected capture and follows a new selection", async () => {
    route(frame());
    const view = render(<LogsPage token="t" context={selected} />);
    await screen.findByRole("table", { name: "Log table" });
    expect(screen.getByRole("heading", { name: CAPTURE_ID })).toBeInTheDocument();
    view.rerender(
      <LogsPage
        token="t"
        context={context({ selectedLogId: "_capture-tuning-2026-09-10T120000" })}
      />,
    );
    expect(
      await screen.findByRole("heading", { name: "_capture-tuning-2026-09-10T120000" }),
    ).toBeInTheDocument();
    const rows = within(screen.getByRole("table", { name: "Capture browser" })).getAllByRole("row");
    expect(rows.slice(1).map((row) => row.getAttribute("aria-selected"))).toEqual([
      "false",
      "true",
    ]);
  });

  it("says a refused token in the shared words, for the browser and the viewer", async () => {
    control.mockRejectedValue(new HttpError(401, ""));
    render(<LogsPage token="t" context={selected} />);
    await waitFor(() =>
      expect(
        screen.getAllByText("Not authorized — enter or check the control token."),
      ).toHaveLength(2),
    );
    expect(screen.queryByText(/HTTP 401/)).not.toBeInTheDocument();
  });

  it("shows the daemon's own sentence when it explains a refusal", async () => {
    control.mockRejectedValue(new HttpError(401, '{"detail":"control token required"}'));
    render(<LogsPage token="t" context={selected} />);
    await waitFor(() => expect(screen.getAllByText("control token required")).toHaveLength(2));
  });
});

describe("workbench helpers", () => {
  it("exports CSV of exactly the given rows", () => {
    expect(
      toCsv(
        ["a", "b"],
        [
          [1, null],
          ["x,y", 2],
        ],
      ),
    ).toBe('a,b\n1,\n"x,y",2\n');
  });

  it("formats durations", () => {
    // The log browser's options: rounded, unpadded minutes, "unknown".
    const log = { rounding: "round", padMinutes: false, missing: "unknown" } as const;
    expect(formatClockDuration(754, log)).toBe("12:34");
    expect(formatClockDuration(3725, log)).toBe("1:02:05");
    expect(formatClockDuration(65.6, log)).toBe("1:06");
    expect(formatClockDuration(null, log)).toBe("unknown");
  });

  it("phrases the gates-removed-everything sentence", () => {
    expect(gateSentence({ started: 9, filtered: { fuel_cut: 9 }, remaining: 0 })).toBe(
      "0 of 9 samples passed: fuel cut removed 9",
    );
  });

  it("reads structured refusals", () => {
    const busy = describeRefusal(
      new HttpError(409, JSON.stringify({ detail: { error: "analysis_busy", message: "busy" } })),
    );
    expect(busy.kind).toBe("busy");
    expect(describeRefusal(new Error("boom")).kind).toBe("error");
  });
});
