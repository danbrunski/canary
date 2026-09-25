import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { controlJson } from "../../api/core";
import {
  SELECTED_LOG_STORAGE_KEY,
  useTuningContext,
  type LoadedRom,
  type TuningContext,
} from "../../hooks/useTuningContext";
import type { ChannelSnapshot } from "../../hooks/useTelemetryStream";
import type { RomInspection } from "../../lib/romMaps";
import type { StatusResponse, WorkbenchItem } from "../../types";
import { Workbench, type DashboardData } from "../Workbench";
import { READINESS_CHIPS, TuningContextBar, chipTone, elapsedLabel } from "./TuningContextBar";

vi.mock("../../api/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/core")>()),
  controlJson: vi.fn(),
}));
const control = vi.mocked(controlJson);

const NOW = new Date("2026-09-21T12:00:00Z");

const INSPECTION: RomInspection = {
  source_sha256: "a".repeat(64),
  revision: "ZS44A-20260920",
  recorded_revision: "ZS44A-20260920",
  matches_recorded_revision: true,
  map_slots: [1],
  afr_stoich: 14.7,
  warnings: [],
  live: {
    rpm: { spec: "did:RPM", transform: "identity", factor: null },
    base_fuel_schedule: { spec: "did:BFS", transform: "scale", factor: 0.00049 },
    commanded_afr: null,
  },
  tables: [],
};

function context(overrides: Partial<TuningContext> = {}): TuningContext {
  return {
    selectedLogId: null,
    setSelectedLogId: () => {},
    loadedRom: null,
    setLoadedRom: () => {},
    ...overrides,
  };
}

function status(overrides: Partial<StatusResponse> = {}): StatusResponse {
  return { vehicle: "2008 Nissan Xterra", ...overrides } as StatusResponse;
}

function chips(container: HTMLElement) {
  return [...container.querySelectorAll<HTMLElement>("[data-chip]")];
}

beforeEach(() => {
  window.localStorage.clear();
});
afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("TuningContextBar empty states", () => {
  it("names every empty segment with its cause", () => {
    render(<TuningContextBar status={null} context={context()} onNavigate={() => {}} />);
    expect(screen.getByText("running: unknown (ECM not yet read)")).toBeInTheDocument();
    expect(screen.getByText("capture: none")).toBeInTheDocument();
    expect(screen.getByText("log: none selected")).toBeInTheDocument();
    expect(screen.getByText("ROM: none loaded")).toBeInTheDocument();
  });

  it("links each segment to the page that fills it", () => {
    const visited: WorkbenchItem[] = [];
    render(
      <TuningContextBar status={null} context={context()} onNavigate={(i) => visited.push(i)} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Capture: open Logging" }));
    fireEvent.click(screen.getByRole("button", { name: "Log: open Logs" }));
    fireEvent.click(screen.getByRole("button", { name: "ROM: open ROM Maps" }));
    expect(visited).toEqual(["tuning-logging", "tuning-logs", "tuning-rom-maps"]);
  });
});

describe("TuningContextBar running calibration", () => {
  it("calls out a mismatch with a link to Calibration", () => {
    const visited: WorkbenchItem[] = [];
    render(
      <TuningContextBar
        status={status({
          ecu_calibration_id: "1ZS44A00000",
          calibration_matches_registry: false,
          registry_revision: "ZS44A-20260920",
        })}
        context={context()}
        onNavigate={(i) => visited.push(i)}
      />,
    );
    expect(screen.getByText("running: 1ZS44A00000 — not in flash history")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Calibration" }));
    expect(visited).toEqual(["tuning-calibration"]);
  });

  it("shows the registry revision when the ECU id matches it", () => {
    render(
      <TuningContextBar
        status={status({
          ecu_calibration_id: "1ZS44A00000",
          calibration_matches_registry: true,
          registry_revision: "ZS44A-20260920",
        })}
        context={context()}
        onNavigate={() => {}}
      />,
    );
    expect(screen.getByText("running ZS44A-20260920")).toBeInTheDocument();
    expect(screen.queryByText(/not in flash history/)).not.toBeInTheDocument();
  });
});

describe("TuningContextBar populated segments", () => {
  it("shows the active capture with elapsed time, the selected log and the ROM", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const started = new Date(NOW.getTime() - (3600 + 2 * 60 + 3) * 1000).toISOString();
    const rom: LoadedRom = { filename: "ZS44A-Tuned.OsirisROM", inspection: INSPECTION };
    render(
      <TuningContextBar
        status={status({
          capture_profile: { active: true, name: "tuning", started_at: started } as never,
        })}
        context={context({ selectedLogId: "_tuning-2026-09-21T120000", loadedRom: rom })}
        onNavigate={() => {}}
      />,
    );
    expect(screen.getByText("01:02:03")).toBeInTheDocument();
    expect(screen.getByText(/capture: tuning/)).toBeInTheDocument();
    expect(screen.getByText("log: _tuning-2026-09-21T120000")).toBeInTheDocument();
    expect(screen.getByText("ROM: ZS44A-Tuned.OsirisROM")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByText("01:02:05")).toBeInTheDocument();
  });

  it("formats elapsed time and refuses unparseable starts", () => {
    expect(elapsedLabel("2026-09-21T11:59:00Z", NOW.getTime())).toBe("00:01:00");
    expect(elapsedLabel("garbage", NOW.getTime())).toBeNull();
    expect(elapsedLabel(null, NOW.getTime())).toBeNull();
  });
});

describe("TuningContextBar readiness chips (W02.7: unknown is not OK)", () => {
  it("renders every chip grey when /status has no tuning_readiness key", () => {
    const view = render(
      <TuningContextBar status={status()} context={context()} onNavigate={() => {}} />,
    );
    const rendered = chips(view.container);
    expect(rendered).toHaveLength(READINESS_CHIPS.length);
    for (const chip of rendered) {
      expect(chip.dataset.tone).toBe("unknown");
      expect(chip).toHaveTextContent("not reported");
    }
    expect(view.container.querySelector('[data-tone="ok"]')).toBeNull();
  });

  it("a null value is grey even when ok claims true", () => {
    expect(chipTone({ ok: true, value: null, detail: "", fix: null })).toBe("unknown");
    expect(chipTone(undefined)).toBe("unknown");
    expect(chipTone({ ok: true, value: 80, detail: "", fix: null })).toBe("ok");
    expect(chipTone({ ok: null, value: 80, detail: "", fix: "x" })).toBe("warn");
    expect(chipTone({ ok: false, value: 20, detail: "", fix: "x" })).toBe("bad");
  });

  it("colours reported chips and shows the fix on hover and on tap", () => {
    const view = render(
      <TuningContextBar
        status={status({
          tuning_readiness: {
            ups_soc: { ok: false, value: 22, detail: "UPS at 22%", fix: "Charge the UPS" },
            storage_free_mb: { ok: true, value: 20480, detail: "20 GB", fix: null },
            memory_channels: {
              ok: null,
              value: { passed: 3, failed: 0, total: 5, never_ran: 2, unverifiable: 0 },
              detail: "3 passed",
              fix: "Run re-verify on the Calibration page",
            },
            calibration: { ok: true, value: "1ZS44A00000", detail: "matches", fix: null },
            vehicle_state: { ok: true, value: "engine_running", detail: "running", fix: null },
          },
        })}
        context={context()}
        onNavigate={() => {}}
      />,
    );
    const byKey = Object.fromEntries(chips(view.container).map((c) => [c.dataset.chip, c]));
    expect(byKey.ups_soc.dataset.tone).toBe("bad");
    expect(byKey.ups_soc).toHaveTextContent("UPS: 22%");
    expect(byKey.ups_soc).toHaveAttribute("title", "Charge the UPS");
    expect(byKey.storage_free_mb.dataset.tone).toBe("ok");
    expect(byKey.memory_channels.dataset.tone).toBe("warn");
    expect(byKey.memory_channels).toHaveTextContent("3/5 verified");
    expect(byKey.vehicle_state).toHaveTextContent("engine running");

    fireEvent.click(byKey.ups_soc);
    expect(screen.getByRole("note")).toHaveTextContent("Charge the UPS");
    fireEvent.click(byKey.ups_soc);
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });
});

describe("useTuningContext", () => {
  it("persists the selected log and not the ROM", () => {
    const first = renderHook(() => useTuningContext());
    act(() => {
      first.result.current.setSelectedLogId("_tuning-2026-09-21T120000");
      first.result.current.setLoadedRom({ filename: "x.OsirisROM", inspection: INSPECTION });
    });
    first.unmount();
    expect(JSON.parse(window.localStorage.getItem(SELECTED_LOG_STORAGE_KEY) ?? "null")).toBe(
      "_tuning-2026-09-21T120000",
    );
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const key = window.localStorage.key(i) ?? "";
      expect(window.localStorage.getItem(key)).not.toContain("OsirisROM");
    }
    const second = renderHook(() => useTuningContext());
    expect(second.result.current.selectedLogId).toBe("_tuning-2026-09-21T120000");
    expect(second.result.current.loadedRom).toBeNull();
  });
});

const DATA: DashboardData = {
  mode: "parked",
  channels: new Map<string, ChannelSnapshot>(),
  bindings: {},
  events: [],
  liveScores: {},
  status: null,
  statusError: null,
  behaviorModels: null,
  behaviorModelsError: null,
  behaviorLive: null,
  token: "test",
  companion: {
    baseUrl: "http://127.0.0.1:8765",
    token: "",
    setBaseUrl: () => {},
    setToken: () => {},
  },
};

describe("Workbench renders the bar on Engine Tuning pages", () => {
  beforeEach(() => {
    control.mockImplementation((path: string) =>
      path === "/control/rom/inspect"
        ? Promise.resolve(INSPECTION as never)
        : new Promise<never>(() => {}),
    );
  });

  it("shows the bar on every tuning-* route loaded directly, and nowhere else", () => {
    for (const item of [
      "tuning-logging",
      "tuning-logs",
      "tuning-analysis",
      "tuning-rom-maps",
      "tuning-calibration",
    ] as const) {
      const view = render(<Workbench active={item} onNavigate={() => {}} data={DATA} />);
      expect(screen.getByRole("region", { name: "Engine Tuning context" })).toBeInTheDocument();
      expect(screen.getByText("capture: none")).toBeInTheDocument();
      view.unmount();
    }
    render(<Workbench active="observe-charts" onNavigate={() => {}} data={DATA} />);
    expect(screen.queryByRole("region", { name: "Engine Tuning context" })).toBeNull();
  });

  it("keeps a loaded ROM across navigation within the group", async () => {
    const view = render(<Workbench active="tuning-rom-maps" onNavigate={() => {}} data={DATA} />);
    await act(async () => {
      fireEvent.change(screen.getByLabelText("ROM file"), {
        target: { files: [new File(["rom"], "ZS44A-Tuned.OsirisROM")] },
      });
    });
    expect(screen.getByText("ROM: ZS44A-Tuned.OsirisROM")).toBeInTheDocument();
    view.rerender(<Workbench active="tuning-calibration" onNavigate={() => {}} data={DATA} />);
    expect(screen.getByText("ROM: ZS44A-Tuned.OsirisROM")).toBeInTheDocument();
    view.rerender(<Workbench active="tuning-rom-maps" onNavigate={() => {}} data={DATA} />);
    expect(screen.getByText(/ZS44A-Tuned\.OsirisROM · ZS44A-20260920/)).toBeInTheDocument();
  });
});
