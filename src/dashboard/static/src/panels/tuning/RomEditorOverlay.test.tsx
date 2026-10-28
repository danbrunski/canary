/**
 * The ROM Editor's live overlay: ROM Maps' behaviours (W05), merged into the
 * editor on 2026-10-26. Every assertion of the retired `RomMapsPanel.test.tsx`
 * lives here against the editor: the slot confirmation and its gates, the ECM
 * cell beside the inferred one, the trace's liveness, the log and live
 * coverage, the drive presentation. The overlay is behind a toggle and reads
 * the vehicle's trace definition from `GET .../trace`.
 */
import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../../api/core";
import type { ChannelSnapshot } from "../../hooks/useTelemetryStream";
import { treeTable } from "../../test/romEditorViews";
import { ENTRY_TUNED, SHA_TUNED, TABLES_ALL_2D } from "../../test/romLibraryFixtures";
import type { StatusResponse } from "../../types";
import type { RomTablesResponse } from "../../types/romLibrary";
import type { RomTraceContext, TableTrace } from "../../types/workbench";
import { RomEditorPage } from "./RomEditorPage";

vi.mock("../../api/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/core")>()),
  controlJson: vi.fn(),
}));
const control = vi.mocked(controlJson);

const now = new Date("2026-09-21T12:00:00Z");
const X = "mem:ECM_FUEL_TARGET_MAP_X_TRACE";
const Y = "mem:ECM_ENGINE_SPEED_RAM";
const NOTE =
  "No capture profile polls the fuel-target trace channels: the rom-trace profile was retired.";
/** The image's BFS labels (UpRev's, ms) for the fixture's X-count axis: count
 *  52 is 8.76 ms, 68 is 11.46 ms. */
const BFS_MS = [8.7627, 11.459];

function trace(verified: boolean): TableTrace {
  return {
    x: verified
      ? {
          channel: X,
          axis_mapping: "verified",
          axis_units: "display",
          scale: 1,
          offset: 0,
          evidence: "synthetic",
          request_channel: "mem:fuel_axis",
          stored_decode: { scale: 1, offset: 0 },
        }
      : {
          channel: X,
          axis_mapping: "unverified",
          axis_units: null,
          scale: null,
          offset: null,
          evidence: "Not settled: needs a capture beside BFS.",
          request_channel: "mem:fuel_axis",
          stored_decode: { scale: 1, offset: 0 },
        },
    y: {
      channel: Y,
      axis_mapping: "verified",
      axis_units: "display",
      scale: 1,
      offset: 0,
      evidence: "rpm",
      request_channel: "mem:spark",
      stored_decode: { scale: 12.5, offset: 0 },
    },
    unavailable_note: NOTE,
    out_of_range: null,
  };
}

/** W05.1 as shipped: the X byte is stored as raw x 0.250859 and lands on the
 *  ROM's raw BFS axis bytes; both axes clamp. */
const X_STORED_SCALE = 0.250859;
function shippedTrace(): TableTrace {
  const base = trace(true);
  return {
    ...base,
    x: {
      ...base.x,
      axis_units: "raw",
      evidence: "firmware",
      stored_decode: { scale: X_STORED_SCALE, offset: 0 },
    },
    out_of_range: { behaviour: "clamp", evidence: "0x026F40" },
  };
}

function context(overrides: Partial<RomTraceContext> = {}, fuel = trace(true)): RomTraceContext {
  return {
    sha256: SHA_TUNED,
    map_slot: 1,
    afr_stoich: 14.7,
    recorded_revision: "ZS44A-20260920",
    matches_recorded_revision: true,
    warnings: ["Inferred lookup only"],
    live: {
      rpm: { spec: "did:RPM", transform: "identity", factor: null },
      base_fuel_schedule: { spec: "did:BFS", transform: "scale", factor: 0.00049 },
      commanded_afr: null,
    },
    tables: [
      {
        kind: "fuel_targets",
        title: "Target AFR",
        x_label: "Base Fuel Schedule",
        x_unit: "ms",
        y_label: "Rotations Per Minute",
        y_unit: "rpm",
        unit: "AFR",
        trace: fuel,
      },
      {
        kind: "fuel_compensation",
        title: "Fuel Compensation",
        x_label: "Throttle-model fill (alpha-N)",
        x_unit: "%",
        y_label: "Rotations Per Minute",
        y_unit: "rpm",
        unit: "%",
        trace: null,
      },
    ],
    ...overrides,
  };
}

interface Extra {
  profiles?: unknown;
  residency?: unknown;
  trace?: RomTraceContext | Error;
}

/** Route the mocked control client by path. */
function routeControl(ctx: RomTraceContext | Error, extra: Extra = {}) {
  control.mockImplementation(async (path: string) => {
    if (path === "/control/rom/library") return { entries: [ENTRY_TUNED] };
    if (path === `/control/rom/library/${SHA_TUNED}/tables`) return TABLES_ALL_2D;
    if (path === `/control/rom/library/${SHA_TUNED}/trace`) {
      if (ctx instanceof Error) throw ctx;
      return ctx;
    }
    if (path === "/control/capture/profiles") return extra.profiles ?? { profiles: [] };
    if (path.includes("/cell-residency")) {
      if (extra.residency instanceof Error) throw extra.residency;
      return extra.residency;
    }
    throw new Error(`unexpected ${path}`);
  });
}

const snapshot = (channel: string, value: number): ChannelSnapshot => ({
  latest: { timestamp: now.toISOString(), source: "did", channel, value, unit: null },
  history: [],
});
function props() {
  return {
    token: "test",
    channels: new Map([
      ["did:RPM", snapshot("RPM", 1200)],
      ["did:BFS", snapshot("BFS", BFS_MS[0] / 0.00049)],
    ]),
    status: {
      vehicle_state: { state: "engine_running", derived_at: now.toISOString() },
    } as StatusResponse,
  };
}
/** Memory rows arrive on the stream keyed `obd:mem:<NAME>`. */
const memRow = (channel: string, value: number, at = now): [string, ChannelSnapshot] => [
  `obd:${channel}`,
  {
    latest: { timestamp: at.toISOString(), source: "obd", channel, value, unit: null },
    history: [],
  },
];
function traceProps(x: number, y: number, status: Partial<StatusResponse> = {}) {
  const base = props();
  const channels = new Map([...base.channels, memRow(X, x), memRow(Y, y)]);
  return { ...base, channels, status: { ...base.status, ...status } };
}
const polling = (memory: string[], extra: Record<string, unknown> = {}) =>
  ({
    capture_profile: {
      active: true,
      name: "tuning",
      started_at: now.toISOString(),
      memory,
      mode22_enabled: true,
      ...extra,
    },
  }) as unknown as Partial<StatusResponse>;
const BOTH = ["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"];

/** Let the mocked fetches settle (fake timers: no `findBy`). */
async function settle() {
  for (let i = 0; i < 6; i += 1) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

type EditorProps = Parameters<typeof RomEditorPage>[0];
async function mount(p: Omit<EditorProps, "token"> & { token?: string } = props()) {
  window.history.replaceState(null, "", `/?rom=${SHA_TUNED}`);
  const view = render(<RomEditorPage token="test" {...p} />);
  await settle();
  return view;
}
const toggle = () => screen.getByRole("checkbox", { name: /^Live overlay/ });
async function overlayOn() {
  await act(async () => {
    fireEvent.click(toggle());
  });
  await settle();
}
/** Switch the editor's view (fake timers: no `findBy`, so not showRomView). */
async function view(name: RegExp) {
  const views = screen.getByRole("group", { name: "ROM Editor views" });
  await act(async () => {
    fireEvent.click(within(views).getByRole("button", { name }));
  });
  await settle();
}
async function confirm() {
  fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
}
const coverageBox = () =>
  screen.getByRole("checkbox", { name: "Coverage overlay Map 1 Fuel Targets" });

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(now);
  routeControl(context());
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});

describe("ROM Editor: the live overlay is behind a toggle", () => {
  it("fetches nothing and draws nothing until it is switched on", async () => {
    const view = await mount();
    expect(toggle()).not.toBeChecked();
    expect(control.mock.calls.some(([path]) => String(path).endsWith("/trace"))).toBe(false);
    expect(
      view.container.querySelectorAll("[data-trace], [data-ecm], [data-coverage]"),
    ).toHaveLength(0);
    expect(screen.queryByRole("checkbox", { name: /I confirm/ })).not.toBeInTheDocument();
    await overlayOn();
    expect(
      control.mock.calls.some(([path]) => path === `/control/rom/library/${SHA_TUNED}/trace`),
    ).toBe(true);
    expect(screen.getByRole("checkbox", { name: /I confirm/ })).toBeInTheDocument();
    expect(screen.getByLabelText("Live map inputs")).toHaveTextContent("RPM: 1200");
    expect(screen.getByText("Inferred lookup only")).toBeInTheDocument();
    // Off again: the marks go with it.
    await act(async () => {
      fireEvent.click(toggle());
    });
    expect(screen.queryByLabelText("Live map inputs")).not.toBeInTheDocument();
  });

  it("is mounted only while Tables is showing: its clock never runs behind a hidden grid", async () => {
    await mount();
    await overlayOn();
    expect(screen.getByText(/I confirm/)).toBeInTheDocument();
    await view(/^Edits & Preview/);
    // Gone, not hidden: the grid stays mounted, the overlay does not.
    expect(screen.queryByText(/I confirm/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("grid", { name: "Fuel Targets (Map 1) cells", hidden: true }),
    ).toBeInTheDocument();
    await view(/^Tables/);
    expect(toggle()).toBeChecked();
    expect(screen.getByText(/I confirm/)).toBeInTheDocument();
  });

  it("says so when the vehicle has no viewer definition, and the editor still works", async () => {
    routeControl(
      new HttpError(
        409,
        JSON.stringify({
          detail:
            "ROM map definition unavailable for this vehicle: [Errno 2] No such file: rom_viewer.yaml",
        }),
      ),
    );
    await mount();
    await overlayOn();
    expect(screen.getByText(/Live overlay unavailable\..*rom_viewer\.yaml/)).toBeInTheDocument();
    expect(screen.getByRole("grid", { name: "Fuel Targets (Map 1) cells" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Operation" })).toBeInTheDocument();
  });

  it("names tables with no trace mapped, and other slots, instead of drawing a cell", async () => {
    const view = await mount(props());
    await overlayOn();
    fireEvent.click(treeTable("Fuel Compensation (Map 1)"));
    expect(screen.getByText(/maps no trace channels to Fuel Compensation/)).toBeInTheDocument();
    fireEvent.click(treeTable("Timing Main (trim A) (Map 1)"));
    expect(
      screen.getByText(/places cells on the tables the vehicle maps a trace to/),
    ).toBeInTheDocument();
    fireEvent.click(treeTable("Fuel Targets (Map 2)"));
    expect(screen.getByText(/Fuel Targets in Map 2 has none/)).toBeInTheDocument();
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
  });

  it("keeps the confirmation across a move to another slot's copy: it is about the image, not the slot", async () => {
    // ROM Maps cleared it when its dropdown changed the traced slot. Here the
    // overlay's subject is fixed at the trace definition's slot (Map 1), so
    // looking at Map 2's copy and coming back neither needs asking again nor
    // draws a cell it has not been told is running.
    const view = await mount(props());
    await overlayOn();
    await confirm();
    const box = () => screen.getByRole("checkbox", { name: /I confirm/ });
    expect(box()).toBeChecked();
    expect(view.container.querySelectorAll('[data-trace="nearest"]')).toHaveLength(1);
    fireEvent.click(treeTable("Fuel Targets (Map 2)"));
    expect(screen.getByText(/Fuel Targets in Map 2 has none/)).toBeInTheDocument();
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
    expect(box()).toBeChecked();
    fireEvent.click(treeTable("Fuel Targets (Map 1)"));
    expect(box()).toBeChecked();
    expect(view.container.querySelectorAll('[data-trace="nearest"]')).toHaveLength(1);
  });

  it("refuses to place a cell on an axis that is not in the lookup's units", async () => {
    // The image's BFS axis without UpRev's ms labels is raw counts, which a
    // live BFS in ms must never be compared with.
    const raw: RomTablesResponse = {
      ...TABLES_ALL_2D,
      tables: TABLES_ALL_2D.tables.map((table) =>
        table.id === "fuel_targets" && table.axes.cols
          ? { ...table, axes: { ...table.axes, cols: { ...table.axes.cols, editor_labels: null } } }
          : table,
      ),
    };
    control.mockImplementation(async (path: string) => {
      if (path === `/control/rom/library/${SHA_TUNED}/tables`) return raw;
      if (path === `/control/rom/library/${SHA_TUNED}/trace`) return context();
      if (path === "/control/rom/library") return { entries: [ENTRY_TUNED] };
      if (path === "/control/capture/profiles") return { profiles: [] };
      throw new Error(`unexpected ${path}`);
    });
    const view = await mount(props());
    await overlayOn();
    expect(
      screen.getByText(/needs Base Fuel Schedule in ms on the columns.*labels them raw counts/),
    ).toBeInTheDocument();
    await confirm();
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
  });
});

describe("ROM Editor: slot confirmation and the trace gates (ROM Maps W05.5)", () => {
  it("requires explicit active-slot confirmation and removes a stale highlight", async () => {
    const view = await mount(props());
    await overlayOn();
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
    await confirm();
    expect(view.container.querySelectorAll('[data-trace="nearest"]')).toHaveLength(1);
    const nearest = view.container.querySelector('[data-trace="nearest"]');
    expect(nearest).toHaveAttribute("data-row", "0");
    expect(nearest).toHaveAttribute("data-col", "0");
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
    expect(screen.getByText(/Waiting for fresh RPM and BFS/)).toBeInTheDocument();
  });

  it("does not trace an image that is not the recorded running revision", async () => {
    routeControl(context({ matches_recorded_revision: false }));
    const view = await mount(props());
    await overlayOn();
    expect(screen.getByRole("checkbox", { name: /I confirm/ })).toBeDisabled();
    expect(screen.getByText(/does not match — historical viewing only/)).toBeInTheDocument();
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
  });

  it("suppresses trace in replay even when samples are fresh", async () => {
    const p = props();
    p.status.vehicle_state!.state = "replay";
    const view = await mount(p);
    await overlayOn();
    await confirm();
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
    expect(screen.getAllByText(/Replay active/).length).toBeGreaterThan(0);
  });

  it("refuses to call any trace active when the ECU calibration id contradicts the registry", async () => {
    const status = {
      ...polling(BOTH),
      calibration_matches_registry: false,
      ecu_calibration_id: "ZS44A-OTHER",
      registry_revision: "current",
      registry_calibration_id: "ZS44A",
    } as Partial<StatusResponse>;
    const view = await mount(traceProps(11.459, 1200, status));
    await overlayOn();
    await confirm();
    expect(view.container.querySelectorAll("[data-trace], [data-ecm]")).toHaveLength(0);
    expect(
      screen.getAllByText(/ECU reports calibration id ZS44A-OTHER, which does not match/).length,
    ).toBeGreaterThan(0);
  });
});

describe("ROM Editor: the ECM's own cell (ROM Maps W05.1-W05.5)", () => {
  it("refuses the ECM cell while the X-trace mapping is unverified, and says why", async () => {
    routeControl(context({}, trace(false)));
    const view = await mount(traceProps(11.46, 1200, polling(BOTH)));
    await overlayOn();
    await confirm();
    expect(view.container.querySelectorAll('[data-trace="nearest"]')).toHaveLength(1);
    expect(view.container.querySelectorAll("[data-ecm]")).toHaveLength(0);
    expect(
      screen.getByText(
        /ECM cell not drawn: how the X trace \(mem:ECM_FUEL_TARGET_MAP_X_TRACE\).*not verified/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/Not settled: needs a capture beside BFS/)).toBeInTheDocument();
  });

  it("outlines the ECM cell beside the inferred one and states the disagreement", async () => {
    const view = await mount(traceProps(11.46, 1200, polling(BOTH)));
    await overlayOn();
    await confirm();
    const ecm = view.container.querySelectorAll('[data-ecm="cell"]');
    expect(ecm).toHaveLength(1);
    expect(ecm[0]).toHaveAttribute("data-row", "0");
    expect(ecm[0]).toHaveAttribute("data-col", "1");
    expect(ecm[0]).toHaveAttribute("aria-label", expect.stringContaining(", ECM cell"));
    expect(view.container.querySelector('[data-trace="nearest"]')).toHaveAttribute("data-col", "0");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "ECM cell differs from the inferred cell by +1 column (ECM 11.46 vs inferred 8.76 ms)",
    );
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(screen.getByText(/Disagreed on 1 of 1 ECM samples/)).toBeInTheDocument();
  });

  it("draws the ECM cell from the raw X byte on the raw BFS axis (W05.1)", async () => {
    // Stored 17.06 (= 0x44 x 0.250859) is the byte 0x44 = 68: column 1. Compared
    // scaled against the raw bytes it would sit below 52.
    routeControl(context({}, shippedTrace()));
    const view = await mount(traceProps(68 * X_STORED_SCALE, 1200, polling(BOTH)));
    await overlayOn();
    await confirm();
    const ecm = view.container.querySelectorAll('[data-ecm="cell"]');
    expect(ecm).toHaveLength(1);
    expect(ecm[0]).toHaveAttribute("data-col", "1");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "ECM cell differs from the inferred cell by +1 column (ECM 11.46 vs inferred 8.76 ms)",
    );
    expect(screen.queryByText(/ECM cell not drawn/)).not.toBeInTheDocument();
  });

  it("outlines the edge cell the ECM reads past the axes, and says so", async () => {
    // Idle: X byte 17 and 750 rpm are below both first breakpoints.
    routeControl(context({}, shippedTrace()));
    const view = await mount(traceProps(17 * X_STORED_SCALE, 750, polling(BOTH)));
    await overlayOn();
    await confirm();
    const ecm = view.container.querySelectorAll('[data-ecm="cell"]');
    expect(ecm).toHaveLength(1);
    expect(ecm[0]).toHaveAttribute("data-row", "0");
    expect(ecm[0]).toHaveAttribute("data-col", "0");
    expect(
      screen.getByText(/Edge cell: .*below the first breakpoint.*reads the edge there/),
    ).toBeInTheDocument();
  });

  it("says the cells agree when they do", async () => {
    await mount(traceProps(8.8, 1210, polling(BOTH)));
    await overlayOn();
    await confirm();
    expect(screen.getByText("ECM cell and inferred cell agree.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("greys out a stale trace as polled-but-not-answering with the request counters", async () => {
    const status = {
      ...polling(BOTH),
      health: {
        obd_scheduler: {
          requests: [
            {
              kind: "memory",
              channel: "mem:fuel_axis",
              configured_rate_hz: 8,
              requests_sent: 12,
              responses: 0,
              timeouts: 12,
              negative_responses: 0,
              backed_off: true,
              last_response_age_seconds: null,
            },
          ],
        },
      },
    } as unknown as Partial<StatusResponse>;
    const view = await mount(traceProps(11.46, 1200, status));
    await overlayOn();
    await confirm();
    expect(view.container.querySelectorAll("[data-ecm]")).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(3300);
    });
    expect(view.container.querySelectorAll("[data-ecm]")).toHaveLength(0);
    const line = view.container.querySelector('[data-trace-liveness="not_answering"]');
    expect(line).toHaveTextContent("polled but not answering");
    expect(line).toHaveTextContent(
      "mem:ECM_FUEL_TARGET_MAP_X_TRACE (mem:fuel_axis): sent 12, responses 0, timeouts 12, NRCs 0",
    );
  });

  it("names the active profile when it does not poll the trace, and offers no nonexistent profile", async () => {
    routeControl(context(), {
      profiles: {
        profiles: [{ name: "tuning", memory: ["ECM_ENGINE_SPEED_RAM"], mode22_enabled: true }],
      },
    });
    const base = props();
    const view = await mount({
      ...base,
      status: { ...base.status, ...polling(["ECM_ENGINE_SPEED_RAM"]) },
      onNavigate: vi.fn(),
    });
    await overlayOn();
    const line = view.container.querySelector('[data-trace-liveness="not_polled"]');
    expect(line).toHaveTextContent("not polled by the active profile “tuning”");
    expect(line).toHaveTextContent("No capture profile in this vehicle's catalog polls them.");
    expect(line).toHaveTextContent(NOTE);
    expect(screen.queryByRole("button", { name: "Open Logging" })).not.toBeInTheDocument();
  });

  it("offers a catalog profile that carries the trace channels on Logging", async () => {
    routeControl(context(), {
      profiles: { profiles: [{ name: "rom-trace", memory: BOTH, mode22_enabled: true }] },
    });
    const onNavigate = vi.fn();
    const base = props();
    await mount({ ...base, status: { ...base.status, ...polling([]) }, onNavigate });
    await overlayOn();
    expect(screen.getByText(/“rom-trace” carries them — start it on Logging/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open Logging" }));
    expect(onNavigate).toHaveBeenCalledWith("tuning-logging");
  });

  it("names no-ECM-data when the key is off", async () => {
    const base = props();
    const view = await mount({
      ...base,
      status: {
        vehicle_state: { state: "engine_off", derived_at: now.toISOString() },
      } as StatusResponse,
    });
    await overlayOn();
    expect(view.container.querySelector('[data-trace-liveness="no_ecm_data"]')).toHaveTextContent(
      "no ECM data (vehicle state engine_off)",
    );
  });
});

describe("ROM Editor: coverage (ROM Maps W05.7-W05.9)", () => {
  const AXES = {
    x_axis: [8.76, 11.46, 15.67, 16.85, 18.2, 18.54, 21.4, 21.57],
    y_axis: [1200, 1600, 2000, 2800, 3600, 4000, 4400, 5200],
  };
  const grid = (fill: (r: number, c: number) => number) =>
    Array.from({ length: 8 }, (_, r) => Array.from({ length: 8 }, (_, c) => fill(r, c)));

  it("asks for a log when none is selected, and offers Logs", async () => {
    const onNavigate = vi.fn();
    await mount({ ...props(), onNavigate });
    await overlayOn();
    fireEvent.click(coverageBox());
    expect(screen.getByText(/No log selected/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open Logs" }));
    expect(onNavigate).toHaveBeenCalledWith("tuning-logs");
  });

  it("shades cells by sample count for the selected log, never-visited distinct from thin", async () => {
    routeControl(context(), {
      residency: {
        available: true,
        session_id: "_capture-tuning-x",
        table: "fuel_targets",
        revision: "ZS44A-20260920",
        reason: null,
        ...AXES,
        counts: grid((r, c) =>
          r === 0 && c === 1 ? 3 : r === 1 ? 40 : r === 0 && c === 2 ? 12 : 0,
        ),
        dwell_s: grid((r, c) => (r === 0 && c === 1 ? 0.4 : r === 1 ? 5 : 0)),
        samples: 55,
        outside_axes: 2,
      },
    });
    const view = await mount({ ...props(), selectedLogId: "_capture-tuning-x" });
    await overlayOn();
    await act(async () => {
      fireEvent.click(coverageBox());
    });
    await settle();
    expect(control).toHaveBeenCalledWith(
      "/control/sessions/_capture-tuning-x/cell-residency?table=fuel_targets",
      "test",
      expect.anything(),
    );
    const cell = (r: number, c: number) =>
      view.container.querySelector(`[data-row="${r}"][data-col="${c}"]`);
    expect(cell(0, 0)).toHaveAttribute("data-coverage", "unvisited");
    expect(cell(0, 1)).toHaveAttribute("data-coverage", "thin");
    expect(cell(0, 2)).toHaveAttribute("data-coverage", "visited");
    expect(cell(1, 0)).toHaveAttribute("data-coverage", "visited");
    expect(cell(0, 1)).toHaveAttribute("title", expect.stringContaining("3 samples"));
    expect(cell(0, 1)).toHaveAttribute("title", expect.stringContaining("0.4 s"));
    expect(screen.getByText(/55 ECM samples, 2 outside the axes/)).toBeInTheDocument();
    expect(screen.getByText(/hover a cell for its count/)).toBeInTheDocument();
  });

  it("names the reason a log has no coverage instead of drawing an empty grid", async () => {
    routeControl(context(), {
      residency: {
        available: false,
        session_id: "old",
        table: "fuel_targets",
        reason: "not_captured",
        message: "This log holds no mem:ECM_FUEL_TARGET_MAP_X_TRACE",
        missing: [X],
      },
    });
    const view = await mount({ ...props(), selectedLogId: "old" });
    await overlayOn();
    await act(async () => {
      fireEvent.click(coverageBox());
    });
    await settle();
    expect(
      screen.getByText(/No coverage for old: This log holds no mem:ECM_FUEL_TARGET_MAP_X_TRACE/),
    ).toBeInTheDocument();
    expect(view.container.querySelectorAll("[data-coverage]")).toHaveLength(0);
  });

  it("refuses to overlay a log binned on another revision's axes", async () => {
    routeControl(context(), {
      residency: {
        available: true,
        session_id: "old",
        table: "fuel_targets",
        revision: "older",
        reason: null,
        x_axis: [8, 12, 15.67, 16.85, 18.2, 18.54, 21.4, 21.57],
        y_axis: AXES.y_axis,
        counts: grid(() => 1),
        dwell_s: grid(() => 0),
        samples: 64,
        outside_axes: 0,
      },
    });
    const view = await mount({ ...props(), selectedLogId: "old" });
    await overlayOn();
    await act(async () => {
      fireEvent.click(coverageBox());
    });
    await settle();
    expect(screen.getByText(/ran revision older, whose axes differ/)).toBeInTheDocument();
    expect(view.container.querySelectorAll("[data-coverage]")).toHaveLength(0);
  });

  it("accumulates live coverage during a capture and resets when it ends", async () => {
    const status = polling(BOTH);
    const view = await mount(traceProps(11.46, 1200, status));
    await overlayOn();
    await confirm();
    await act(async () => {
      fireEvent.click(coverageBox());
    });
    const rerender = (p: ReturnType<typeof traceProps>) => view.rerender(<RomEditorPage {...p} />);
    for (let i = 1; i <= 5; i += 1) {
      const at = new Date(now.getTime() + i * 125);
      act(() => {
        vi.setSystemTime(at);
      });
      const p = traceProps(11.46, 1200, status);
      p.channels = new Map([...p.channels, memRow(X, 11.46, at), memRow(Y, 1200, at)]);
      rerender(p);
    }
    const cell = (r: number, c: number) =>
      view.container.querySelector(`[data-row="${r}"][data-col="${c}"]`);
    expect(cell(0, 1)).toHaveAttribute("data-coverage", "visited");
    expect(cell(0, 0)).toHaveAttribute("data-coverage", "unvisited");
    expect(screen.getByText(/Live coverage for this capture: 6 ECM samples/)).toBeInTheDocument();
    rerender(
      traceProps(11.46, 1200, {
        capture_profile: { active: false, name: "default", memory: [] },
      } as unknown as Partial<StatusResponse>),
    );
    expect(screen.getByText(/No capture is recording/)).toBeInTheDocument();
    rerender(traceProps(11.46, 1200, polling(BOTH, { started_at: "2026-09-21T13:00:00Z" })));
    expect(
      screen.getByText(/Live coverage for this capture: [01] ECM samples/),
    ).toBeInTheDocument();
  });

  it("steps each ECM sample once under StrictMode's double render (R08.13)", async () => {
    // The tally and the live grid are stepped while rendering; both write
    // absolute values, so the discarded second render cannot count twice.
    const status = polling(BOTH);
    window.history.replaceState(null, "", `/?rom=${SHA_TUNED}`);
    const view = render(
      <StrictMode>
        <RomEditorPage {...traceProps(11.46, 1200, status)} />
      </StrictMode>,
    );
    await settle();
    await overlayOn();
    await confirm();
    await act(async () => {
      fireEvent.click(coverageBox());
    });
    for (let i = 1; i <= 3; i += 1) {
      const at = new Date(now.getTime() + i * 125);
      act(() => {
        vi.setSystemTime(at);
      });
      const p = traceProps(11.46, 1200, status);
      p.channels = new Map([...p.channels, memRow(X, 11.46, at), memRow(Y, 1200, at)]);
      view.rerender(
        <StrictMode>
          <RomEditorPage {...p} />
        </StrictMode>,
      );
    }
    expect(screen.getByText(/Live coverage for this capture: 4 ECM samples/)).toBeInTheDocument();
    expect(screen.getByText(/Disagreed on 4 of 4 ECM samples/)).toBeInTheDocument();
  });
});

describe("ROM Editor: drive presentation (ROM Maps)", () => {
  it("opens and closes a large presentation of the traced table", async () => {
    await mount(props());
    await overlayOn();
    fireEvent.click(screen.getByRole("button", { name: "Drive presentation" }));
    const dialog = screen.getByRole("dialog", { name: "Drive presentation" });
    expect(dialog).toHaveTextContent("Map 1 · Fuel Targets");
    expect(within(dialog).getByRole("table", { name: /drive presentation/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Exit drive presentation" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
