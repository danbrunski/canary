import { StrictMode } from "react";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { controlJson } from "../api/core";
import type { RomInspection } from "../lib/romMaps";
import type { TableTrace } from "../lib/romTrace";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { UPREV_FUEL_COMP_LABELS } from "../test/romLibraryFixtures";
import type { StatusResponse } from "../types";
import { RomMapsPanel } from "./RomMapsPanel";

vi.mock("../api/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/core")>()),
  controlJson: vi.fn(),
}));
const inspect = vi.mocked(controlJson);
/** Route the mocked control client by path: the panel also reads the capture
 *  profile catalog (W05.4) and cell residency (W05.6). */
function routeControl(inspection: RomInspection, extra: Record<string, unknown> = {}) {
  inspect.mockImplementation(async (path: string) => {
    if (path === "/control/rom/inspect") return inspection;
    if (path === "/control/capture/profiles") return extra.profiles ?? { profiles: [] };
    if (path.includes("/cell-residency")) {
      if (extra.residency instanceof Error) throw extra.residency;
      return extra.residency;
    }
    throw new Error(`unexpected ${path}`);
  });
}
const now = new Date("2026-09-21T12:00:00Z");
const result: RomInspection = {
  source_sha256: "a".repeat(64),
  revision: "current",
  recorded_revision: "current",
  matches_recorded_revision: true,
  map_slots: [1, 2, 3, 4, 5],
  afr_stoich: 14.7,
  warnings: ["Inferred lookup only"],
  live: {
    rpm: { spec: "did:RPM", transform: "identity", factor: null },
    base_fuel_schedule: { spec: "did:BFS", transform: "scale", factor: 0.00049 },
    commanded_afr: null,
  },
  tables: [1, 2, 3, 4, 5].flatMap((slot) =>
    (["fuel_targets", "fuel_compensation"] as const).map((kind) => ({
      slot,
      kind,
      title: kind === "fuel_targets" ? "Target AFR" : "Fuel Compensation",
      status: slot === 1 && kind === "fuel_targets" ? ("available" as const) : ("tbd" as const),
      reason: "TBD — complete table location and axes not verified.",
      x_label: kind === "fuel_targets" ? "Base Fuel Schedule" : "Throttle-model fill (alpha-N)",
      x_unit: kind === "fuel_targets" ? "ms" : "%",
      y_label: "Rotations Per Minute",
      y_unit: "rpm",
      unit: kind === "fuel_targets" ? "AFR" : "%",
      editor_disagreement: kind === "fuel_compensation" ? UPREV_FUEL_COMP_LABELS : null,
      x_axis: [8.76, 11.46],
      y_axis: [1200, 1600],
      values: [
        [14.7, 13],
        [13, 12],
      ],
      raw: [
        [128, 145],
        [145, 157],
      ],
    })),
  ),
};
const snapshot = (channel: string, value: number): ChannelSnapshot => ({
  latest: { timestamp: now.toISOString(), source: "did", channel, value, unit: null },
  history: [],
});
function props() {
  return {
    token: "test",
    channels: new Map([
      ["did:RPM", snapshot("RPM", 1200)],
      ["did:BFS", snapshot("BFS", 8.76 / 0.00049)],
    ]),
    status: {
      vehicle_state: { state: "engine_running", derived_at: now.toISOString() },
    } as StatusResponse,
  };
}
async function upload(name = "current.OsirisROM") {
  await act(async () => {
    fireEvent.change(screen.getByLabelText("ROM file"), {
      target: { files: [new File(["test"], name)] },
    });
  });
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(now);
  routeControl(result);
});
afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("ROM maps panel", () => {
  it("uploads a file, displays two maps, and keeps slots 2–5 explicitly TBD", async () => {
    render(<RomMapsPanel {...props()} />);
    expect(screen.getByLabelText("Map slot")).toBeDisabled();
    await upload();
    expect(screen.getAllByRole("option")).toHaveLength(5);
    expect(inspect).toHaveBeenCalledWith(
      "/control/rom/inspect",
      "test",
      expect.objectContaining({ method: "POST", body: expect.any(File) }),
    );
    expect(screen.getByRole("table", { name: "Map 1 Target AFR values" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Map 1 Fuel Compensation" })).toHaveTextContent(
      "TBD",
    );
    fireEvent.change(screen.getByLabelText("Map slot"), { target: { value: "5" } });
    expect(screen.getByRole("region", { name: "Map 5 Target AFR" })).toHaveTextContent("TBD");
    expect(screen.getByRole("region", { name: "Map 5 Fuel Compensation" })).toHaveTextContent(
      "TBD",
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("warns on the TBD Fuel Compensation card that UpRev's axis labels differ, not on Target AFR", async () => {
    render(<RomMapsPanel {...props()} />);
    await upload();
    for (const slot of [1, 3]) {
      if (slot !== 1)
        fireEvent.change(screen.getByLabelText("Map slot"), { target: { value: String(slot) } });
      const card = screen.getByRole("region", { name: `Map ${slot} Fuel Compensation` });
      expect(card).toHaveTextContent("TBD");
      const note = within(card).getByRole("note", {
        name: `Editor labels Map ${slot} Fuel Compensation`,
      });
      expect(note).toHaveTextContent(
        "Grid positions agree: row i, column j is the same cell in UpRev and here. Its axis labels do not.",
      );
      expect(note).toHaveTextContent(
        'columns = 0xCACD (the rpm axis) x 100 / 256, named "Volumetric',
      );
      expect(card).toHaveTextContent("X: Throttle-model fill (alpha-N) (%) · Y:");
      expect(
        within(screen.getByRole("region", { name: `Map ${slot} Target AFR` })).queryByRole("note"),
      ).not.toBeInTheDocument();
    }
  });

  it("requires explicit active-slot confirmation and removes a stale highlight", async () => {
    const view = render(<RomMapsPanel {...props()} />);
    await upload();
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
    fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
    expect(view.container.querySelectorAll('[data-trace="nearest"]')).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
    expect(screen.getByText(/Waiting for fresh RPM and BFS/)).toBeInTheDocument();
  });

  it("clears confirmation on a slot change and does not trace a mismatched ROM", async () => {
    routeControl({ ...result, matches_recorded_revision: false });
    const view = render(<RomMapsPanel {...props()} />);
    await upload();
    expect(screen.getByRole("checkbox", { name: /I confirm/ })).toBeDisabled();
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
  });

  it("clears an old table when a replacement fails, and reports the error", async () => {
    render(<RomMapsPanel {...props()} />);
    await upload();
    inspect.mockImplementation(async (path: string) => {
      if (path === "/control/rom/inspect") throw new Error("Unknown source hash");
      return { profiles: [] };
    });
    await upload("unknown.OsirisROM");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Unknown source hash");
  });

  it("rejects the wrong extension without uploading and needs a token", async () => {
    const view = render(<RomMapsPanel {...props()} />);
    await upload("wrong.txt");
    expect(inspect).not.toHaveBeenCalledWith(
      "/control/rom/inspect",
      expect.anything(),
      expect.anything(),
    );
    view.rerender(<RomMapsPanel {...props()} token="" />);
    expect(screen.getByLabelText("ROM file")).toBeDisabled();
  });

  it("suppresses trace in replay even when samples are fresh", async () => {
    const p = props();
    p.status.vehicle_state!.state = "replay";
    const view = render(<RomMapsPanel {...p} />);
    await upload();
    fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
    expect(screen.getByText(/Replay active/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- W05: ECM trace and coverage

const X = "mem:ECM_FUEL_TARGET_MAP_X_TRACE";
const Y = "mem:ECM_ENGINE_SPEED_RAM";
const NOTE =
  "No capture profile polls the fuel-target trace channels: the rom-trace profile was retired.";
/** A synthetic trace whose stored values are already in axis units, or the
 *  unverified state (kept as a schema state after W05.1 settled the X axis). */
function withTrace(verified: boolean): RomInspection {
  const trace: TableTrace = {
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
  return {
    ...result,
    tables: result.tables.map((t) => (t.status === "available" ? { ...t, trace } : t)),
  };
}
/** W05.1 as shipped: the X byte is stored as raw × 0.250859 and lands on the
 *  ROM's raw BFS axis bytes (0x34 = 8.76 ms, 0x44 = 11.46 ms); both clamp. */
const X_STORED_SCALE = 0.250859;
function withShippedTrace(): RomInspection {
  const base = withTrace(true);
  return {
    ...base,
    tables: base.tables.map((t) =>
      t.status === "available" && t.trace
        ? {
            ...t,
            x_axis_raw: [0x34, 0x44],
            y_axis_raw: [0x18, 0x20],
            trace: {
              ...t.trace,
              x: {
                ...t.trace.x,
                axis_units: "raw" as const,
                evidence: "firmware",
                stored_decode: { scale: X_STORED_SCALE, offset: 0 },
              },
              out_of_range: { behaviour: "clamp" as const, evidence: "0x026F40" },
            },
          }
        : t,
    ),
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
async function confirm() {
  fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
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

describe("ROM maps — ECM trace (W05.1–W05.5)", () => {
  it("refuses the ECM cell while the X-trace mapping is unverified, and says why", async () => {
    routeControl(withTrace(false));
    const view = render(
      <RomMapsPanel
        {...traceProps(
          11.46,
          1200,
          polling(["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"]),
        )}
      />,
    );
    await upload();
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
    routeControl(withTrace(true));
    const view = render(
      <RomMapsPanel
        {...traceProps(
          11.46,
          1200,
          polling(["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"]),
        )}
      />,
    );
    await upload();
    await confirm();
    const ecm = view.container.querySelectorAll('[data-ecm="cell"]');
    expect(ecm).toHaveLength(1);
    expect(ecm[0]).toHaveAttribute("aria-label", expect.stringContaining("11.46 ms"));
    expect(view.container.querySelector('[data-trace="nearest"]')).toHaveAttribute(
      "aria-label",
      expect.stringContaining("8.76 ms"),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "ECM cell differs from the inferred cell by +1 column (ECM 11.46 vs inferred 8.76 ms)",
    );
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(screen.getByText(/Disagreed on 1 of 1 ECM samples/)).toBeInTheDocument();
  });

  it("draws the ECM cell from the raw X byte on the raw BFS axis (W05.1)", async () => {
    // Stored 17.06 (= 0x44 x 0.250859) is the byte 0x44: column 1, 11.46 ms.
    // Compared scaled against the raw bytes it would sit below 0x34.
    routeControl(withShippedTrace());
    const view = render(
      <RomMapsPanel
        {...traceProps(
          0x44 * X_STORED_SCALE,
          1200,
          polling(["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"]),
        )}
      />,
    );
    await upload();
    await confirm();
    const ecm = view.container.querySelectorAll('[data-ecm="cell"]');
    expect(ecm).toHaveLength(1);
    expect(ecm[0]).toHaveAttribute("aria-label", expect.stringContaining("11.46 ms"));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "ECM cell differs from the inferred cell by +1 column (ECM 11.46 vs inferred 8.76 ms)",
    );
    expect(screen.queryByText(/ECM cell not drawn/)).not.toBeInTheDocument();
  });

  it("outlines the edge cell the ECM reads past the axes, and says so", async () => {
    // Idle: X byte 17 (BFS ~2.9 ms) and 750 rpm are below both first breakpoints.
    routeControl(withShippedTrace());
    const view = render(
      <RomMapsPanel
        {...traceProps(
          17 * X_STORED_SCALE,
          750,
          polling(["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"]),
        )}
      />,
    );
    await upload();
    await confirm();
    const ecm = view.container.querySelectorAll('[data-ecm="cell"]');
    expect(ecm).toHaveLength(1);
    expect(ecm[0]).toHaveAttribute("aria-label", expect.stringContaining("1200 RPM, 8.76 ms"));
    expect(
      screen.getByText(/Edge cell: .*below the first breakpoint.*reads the edge there/),
    ).toBeInTheDocument();
  });

  it("says the cells agree when they do", async () => {
    routeControl(withTrace(true));
    render(
      <RomMapsPanel
        {...traceProps(8.8, 1210, polling(["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"]))}
      />,
    );
    await upload();
    await confirm();
    expect(screen.getByText("ECM cell and inferred cell agree.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("greys out a stale trace as polled-but-not-answering with the request counters", async () => {
    routeControl(withTrace(true));
    const status = {
      ...polling(["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"]),
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
    const view = render(<RomMapsPanel {...traceProps(11.46, 1200, status)} />);
    await upload();
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
    routeControl(withTrace(true), {
      profiles: {
        profiles: [{ name: "tuning", memory: ["ECM_ENGINE_SPEED_RAM"], mode22_enabled: true }],
      },
    });
    const onNavigate = vi.fn();
    const base = props();
    const view = render(
      <RomMapsPanel
        {...base}
        status={{ ...base.status, ...polling(["ECM_ENGINE_SPEED_RAM"]) }}
        onNavigate={onNavigate}
      />,
    );
    await upload();
    const line = view.container.querySelector('[data-trace-liveness="not_polled"]');
    expect(line).toHaveTextContent("not polled by the active profile “tuning”");
    expect(line).toHaveTextContent("No capture profile in this vehicle's catalog polls them.");
    expect(line).toHaveTextContent(NOTE);
    expect(screen.queryByRole("button", { name: "Open Logging" })).not.toBeInTheDocument();
  });

  it("offers a catalog profile that carries the trace channels on Logging", async () => {
    routeControl(withTrace(true), {
      profiles: {
        profiles: [
          {
            name: "rom-trace",
            memory: ["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"],
            mode22_enabled: true,
          },
        ],
      },
    });
    const onNavigate = vi.fn();
    const base = props();
    render(
      <RomMapsPanel
        {...base}
        status={{ ...base.status, ...polling([]) }}
        onNavigate={onNavigate}
      />,
    );
    await upload();
    expect(screen.getByText(/“rom-trace” carries them — start it on Logging/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open Logging" }));
    expect(onNavigate).toHaveBeenCalledWith("tuning-logging");
  });

  it("names no-ECM-data when the key is off", async () => {
    routeControl(withTrace(true));
    const base = props();
    const view = render(
      <RomMapsPanel
        {...base}
        status={
          {
            vehicle_state: { state: "engine_off", derived_at: now.toISOString() },
          } as StatusResponse
        }
      />,
    );
    await upload();
    expect(view.container.querySelector('[data-trace-liveness="no_ecm_data"]')).toHaveTextContent(
      "no ECM data (vehicle state engine_off)",
    );
  });

  it("refuses to call any trace active when the ECU calibration id contradicts the registry", async () => {
    routeControl(withTrace(true));
    const status = {
      ...polling(["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"]),
      calibration_matches_registry: false,
      ecu_calibration_id: "ZS44A-OTHER",
      registry_revision: "current",
      registry_calibration_id: "ZS44A",
    } as Partial<StatusResponse>;
    const view = render(<RomMapsPanel {...traceProps(11.46, 1200, status)} />);
    await upload();
    await confirm();
    expect(view.container.querySelectorAll("[data-trace], [data-ecm]")).toHaveLength(0);
    expect(
      screen.getAllByText(/ECU reports calibration id ZS44A-OTHER, which does not match/).length,
    ).toBeGreaterThan(0);
  });
});

describe("ROM maps — coverage (W05.7–W05.9)", () => {
  it("asks for a log when none is selected, and offers Logs", async () => {
    routeControl(withTrace(true));
    const onNavigate = vi.fn();
    render(<RomMapsPanel {...props()} onNavigate={onNavigate} />);
    await upload();
    fireEvent.click(screen.getByRole("checkbox", { name: "Coverage overlay Map 1 Target AFR" }));
    expect(screen.getByText(/No log selected/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open Logs" }));
    expect(onNavigate).toHaveBeenCalledWith("tuning-logs");
  });

  it("shades cells by sample count for the selected log, never-visited distinct from thin", async () => {
    routeControl(withTrace(true), {
      residency: {
        available: true,
        session_id: "_capture-tuning-x",
        table: "fuel_targets",
        revision: "current",
        reason: null,
        x_axis: [8.76, 11.46],
        y_axis: [1200, 1600],
        counts: [
          [0, 3],
          [40, 12],
        ],
        dwell_s: [
          [0, 0.4],
          [5, 1.5],
        ],
        samples: 55,
        outside_axes: 2,
      },
    });
    const view = render(<RomMapsPanel {...props()} selectedLogId="_capture-tuning-x" />);
    await upload();
    await act(async () => {
      fireEvent.click(screen.getByRole("checkbox", { name: "Coverage overlay Map 1 Target AFR" }));
    });
    expect(inspect).toHaveBeenCalledWith(
      "/control/sessions/_capture-tuning-x/cell-residency?table=fuel_targets",
      "test",
      expect.anything(),
    );
    const cells = [...view.container.querySelectorAll("td")].map((td) =>
      td.getAttribute("data-coverage"),
    );
    expect(cells).toEqual(["unvisited", "thin", "visited", "visited"]);
    expect(view.container.querySelectorAll("td")[1]).toHaveAttribute(
      "title",
      expect.stringContaining("3 samples"),
    );
    expect(screen.getByText(/55 ECM samples, 2 outside the axes/)).toBeInTheDocument();
  });

  it("names the reason a log has no coverage instead of drawing an empty grid", async () => {
    routeControl(withTrace(true), {
      residency: {
        available: false,
        session_id: "old",
        table: "fuel_targets",
        reason: "not_captured",
        message: "This log holds no mem:ECM_FUEL_TARGET_MAP_X_TRACE",
        missing: [X],
      },
    });
    const view = render(<RomMapsPanel {...props()} selectedLogId="old" />);
    await upload();
    await act(async () => {
      fireEvent.click(screen.getByRole("checkbox", { name: "Coverage overlay Map 1 Target AFR" }));
    });
    expect(
      screen.getByText(/No coverage for old: This log holds no mem:ECM_FUEL_TARGET_MAP_X_TRACE/),
    ).toBeInTheDocument();
    expect(view.container.querySelectorAll("[data-coverage]")).toHaveLength(0);
  });

  it("refuses to overlay a log binned on another revision's axes", async () => {
    routeControl(withTrace(true), {
      residency: {
        available: true,
        session_id: "old",
        table: "fuel_targets",
        revision: "older",
        reason: null,
        x_axis: [8, 12],
        y_axis: [1200, 1600],
        counts: [
          [1, 1],
          [1, 1],
        ],
        dwell_s: [
          [0, 0],
          [0, 0],
        ],
        samples: 4,
        outside_axes: 0,
      },
    });
    const view = render(<RomMapsPanel {...props()} selectedLogId="old" />);
    await upload();
    await act(async () => {
      fireEvent.click(screen.getByRole("checkbox", { name: "Coverage overlay Map 1 Target AFR" }));
    });
    expect(screen.getByText(/ran revision older, whose axes differ/)).toBeInTheDocument();
    expect(view.container.querySelectorAll("[data-coverage]")).toHaveLength(0);
  });

  it("accumulates live coverage during a capture and resets when it ends", async () => {
    routeControl(withTrace(true));
    const status = polling(["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"]);
    const view = render(<RomMapsPanel {...traceProps(11.46, 1200, status)} />);
    await upload();
    await confirm();
    await act(async () => {
      fireEvent.click(screen.getByRole("checkbox", { name: "Coverage overlay Map 1 Target AFR" }));
    });
    for (let i = 1; i <= 5; i += 1) {
      const at = new Date(now.getTime() + i * 125);
      act(() => {
        vi.setSystemTime(at);
      });
      const p = traceProps(11.46, 1200, status);
      p.channels = new Map([...p.channels, memRow(X, 11.46, at), memRow(Y, 1200, at)]);
      view.rerender(<RomMapsPanel {...p} />);
    }
    expect(view.container.querySelectorAll("td")[1]).toHaveAttribute("data-coverage", "visited");
    expect(view.container.querySelectorAll("td")[0]).toHaveAttribute("data-coverage", "unvisited");
    expect(screen.getByText(/Live coverage for this capture: 6 ECM samples/)).toBeInTheDocument();
    const ended = traceProps(11.46, 1200, {
      capture_profile: { active: false, name: "default", memory: [] },
    } as unknown as Partial<StatusResponse>);
    view.rerender(<RomMapsPanel {...ended} />);
    expect(screen.getByText(/No capture is recording/)).toBeInTheDocument();
    view.rerender(
      <RomMapsPanel
        {...traceProps(
          11.46,
          1200,
          polling(["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"], {
            started_at: "2026-09-21T13:00:00Z",
          }),
        )}
      />,
    );
    expect(
      screen.getByText(/Live coverage for this capture: [01] ECM samples/),
    ).toBeInTheDocument();
  });

  it("steps each ECM sample once under StrictMode's double render (R08.13)", async () => {
    // The tally and the live grid are stepped while rendering; both write
    // absolute values, so the discarded second render cannot count twice.
    routeControl(withTrace(true));
    const status = polling(["ECM_FUEL_TARGET_MAP_X_TRACE", "ECM_ENGINE_SPEED_RAM"]);
    const view = render(
      <StrictMode>
        <RomMapsPanel {...traceProps(11.46, 1200, status)} />
      </StrictMode>,
    );
    await upload();
    await confirm();
    await act(async () => {
      fireEvent.click(screen.getByRole("checkbox", { name: "Coverage overlay Map 1 Target AFR" }));
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
          <RomMapsPanel {...p} />
        </StrictMode>,
      );
    }
    expect(screen.getByText(/Live coverage for this capture: 4 ECM samples/)).toBeInTheDocument();
    expect(screen.getByText(/Disagreed on 4 of 4 ECM samples/)).toBeInTheDocument();
  });

  it("opens and closes a drive presentation of one table", async () => {
    render(<RomMapsPanel {...props()} />);
    await upload();
    fireEvent.click(screen.getByRole("button", { name: "Drive presentation" }));
    const dialog = screen.getByRole("dialog", { name: "Drive presentation" });
    expect(dialog).toHaveTextContent("Map 1 · Target AFR");
    fireEvent.click(screen.getByRole("button", { name: "Exit drive presentation" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
