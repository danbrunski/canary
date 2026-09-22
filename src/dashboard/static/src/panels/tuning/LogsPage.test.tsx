import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../../api/core";
import type { TuningContext } from "../../hooks/useTuningContext";
import {
  describeRefusal,
  formatDuration,
  gateSentence,
  toCsv,
  type FrameResponse,
  type LogsResponse,
} from "../../lib/workbenchApi";
import { LogsPage } from "./LogsPage";

vi.mock("../../api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/core")>();
  return { ...actual, controlJson: vi.fn() };
});
const control = vi.mocked(controlJson);

const LOGS: LogsResponse = {
  include_sessions: false,
  total: 2,
  limit: 100,
  capped: false,
  logs: [
    {
      session_id: "_capture-tuning-2026-09-20T120000",
      is_capture: true,
      profile: "tuning",
      started_at: "2026-09-20T12:00:00+00:00",
      duration_s: 754,
      duration_source: "capture_manifest",
      row_count: 123456,
      revision: { revision: "ZS44A-20260920", source: "capture_manifest", conflict: false },
      revision_boundaries: [],
      quality: { verdict: "pass", scope: "stop" },
      end_reason: "operator_stop",
      tag_count: 2,
    },
    {
      session_id: "_capture-tuning-2026-09-10T120000",
      is_capture: true,
      profile: "tuning-did",
      started_at: "2026-09-10T12:00:00+00:00",
      duration_s: 60,
      duration_source: "parquet_span",
      row_count: 900,
      revision: { revision: "ZS44A-20260727", source: "registry", conflict: false },
      revision_boundaries: [{ revision: "ZS44A-20260910", flashed_at: "2026-09-10T12:00:30+00:00" }],
      quality: null,
      end_reason: "daemon_shutdown",
      tag_count: 0,
    },
  ],
};

function frame(overrides: Partial<FrameResponse> = {}): FrameResponse {
  return {
    session_id: "_capture-tuning-2026-09-20T120000",
    profile: "tuning",
    grid: "1s",
    columns: [
      { spec: "obd:RPM", unit: "rpm" },
      { spec: "obd:MAF", unit: "g/s" },
    ],
    missing: [],
    timestamps: [
      "2026-09-20T12:00:00+00:00",
      "2026-09-20T12:00:01+00:00",
      "2026-09-20T12:00:02+00:00",
    ],
    values: { "obd:RPM": [800, 3000, 1500], "obd:MAF": [4, 60, 20] },
    regime_flags: {},
    regimes: {},
    regime_report: { regime: "all", before: 3, after: 3 },
    freshness: { cells_with_any: 5, kept: 3, dropped: 2, rule: "every channel" },
    rows_total: 3,
    offset: 0,
    rows_returned: 3,
    row_cap: 20000,
    capped: false,
    tags: [{ t: "2026-09-20T12:00:01+00:00", label: "pull_start" }],
    revision: { revision: "ZS44A-20260920", source: "capture_manifest", conflict: false },
    revision_boundaries: [],
    span: { first: null, last: null },
    ...overrides,
  };
}

function context(overrides: Partial<TuningContext> = {}): TuningContext {
  return {
    selectedLogId: null,
    setSelectedLogId: () => {},
    loadedRom: null,
    setLoadedRom: () => {},
    ...overrides,
  };
}

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
        missing: [{ spec: "obd:INTAKE_TEMP", reason: "This capture's profile does not poll obd:INTAKE_TEMP" }],
      }),
    );
    render(<LogsPage token="t" context={selected} />);
    expect(await screen.findByText(/Showing rows 1–3 of 50,000/)).toBeInTheDocument();
    expect(screen.getByText("This capture's profile does not poll obd:INTAKE_TEMP")).toBeInTheDocument();
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
    expect(await screen.findByText(/is recording/)).toHaveTextContent("Deferred while tuning is recording");
    expect(screen.getByText(/tar -cf -/)).toBeInTheDocument();
  });

  it("sends the regime filter to the frame endpoint", async () => {
    route(frame());
    render(<LogsPage token="t" context={selected} />);
    await screen.findByRole("table", { name: "Log table" });
    fireEvent.change(screen.getByLabelText("Regime"), { target: { value: "fuel_cut" } });
    await waitFor(() =>
      expect(
        control.mock.calls.some(([path]) => String(path).includes("regime=fuel_cut")),
      ).toBe(true),
    );
  });
});

describe("workbench helpers", () => {
  it("exports CSV of exactly the given rows", () => {
    expect(toCsv(["a", "b"], [[1, null], ["x,y", 2]])).toBe('a,b\n1,\n"x,y",2\n');
  });

  it("formats durations", () => {
    expect(formatDuration(754)).toBe("12:34");
    expect(formatDuration(3725)).toBe("1:02:05");
    expect(formatDuration(null)).toBe("unknown");
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
