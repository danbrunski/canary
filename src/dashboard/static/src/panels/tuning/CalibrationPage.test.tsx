import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, NetworkError, controlJson } from "../../api/core";
import type { StatusResponse } from "../../types";
import type {
  CalibrationResponse,
  MemoryChannelsResponse,
  VerifyMemoryResponse,
} from "../../types/workbench";
import { CalibrationPage } from "./CalibrationPage";

vi.mock("../../api/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/core")>()),
  controlJson: vi.fn(),
}));
const control = vi.mocked(controlJson);

const CALIBRATION: CalibrationResponse = {
  running_revision: "ZS44A-20260920",
  registry_calibration_id: "CAL-1",
  ecu_calibration_id: "CAL-1",
  ecu_calibration_read_at: "2026-09-21T12:00:00Z",
  calibration_matches_registry: true,
  calibration_changed: false,
  history: [
    {
      revision: "ZS44A-20260920",
      flashed_at: "2026-09-20T12:00:00Z",
      calibration_id: "CAL-1",
      k: 12.3,
      rom_file: "current.OsirisROM",
      rom_sha256: null,
      notes: "WOT +3%",
    },
  ],
};

const MEMORY: MemoryChannelsResponse = {
  running_revision: "ZS44A-20260920",
  count: 2,
  channels: [
    {
      name: "PHI_CMD",
      address: "0xFFFF8000",
      window: "fuel",
      unit: "",
      verified_tune_revision: "ZS44A-20260920",
      revision_carried_reason: null,
      known_answer: { relation: "equals_did", did: 0x1134 },
      state: "verified",
    },
    {
      name: "AIR_CHARGE",
      address: "0xFFFF8004",
      window: "fuel",
      unit: "",
      verified_tune_revision: "ZS44A-20260727",
      revision_carried_reason: null,
      known_answer: null,
      state: "unverifiable",
    },
  ],
};

const VERIFY: VerifyMemoryResponse = {
  verdicts: [{ channel: "PHI_CMD", outcome: "pass", reason: "1:1", samples: 40, agreements: 40 }],
  counts: { pass: 1 },
  recorded: 0,
  notes: [],
};

const STATUS = { vehicle_state: { state: "engine_running" } } as StatusResponse;

function route(overrides: Record<string, unknown> = {}) {
  control.mockImplementation((path: string, _token: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${path}`;
    if (key in overrides) {
      const value = overrides[key];
      return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
    }
    if (key === "GET /control/calibration") return Promise.resolve(CALIBRATION);
    if (key === "GET /control/memory-channels") return Promise.resolve(MEMORY);
    if (key === "POST /control/memory-channels/verify") return Promise.resolve(VERIFY);
    if (key === "POST /control/calibration") return Promise.resolve({});
    return Promise.reject(new Error(`unexpected ${key}`));
  });
}

function bodyOf(method: string, path: string): unknown {
  const call = control.mock.calls.find(([p, , init]) => p === path && init?.method === method);
  return call ? JSON.parse(call[2]?.body as string) : undefined;
}

afterEach(() => vi.clearAllMocks());

describe("CalibrationPage", () => {
  it("asks for the token and fetches nothing without one", () => {
    route();
    render(<CalibrationPage token="" status={STATUS} />);
    expect(
      screen.getByText("Enter the control token to inspect calibration evidence."),
    ).toBeInTheDocument();
    expect(control).not.toHaveBeenCalled();
  });

  it("shows the running revision, flash history and RAM evidence grouped by window", async () => {
    route();
    render(<CalibrationPage token="t" status={STATUS} />);
    expect(await screen.findByText("WOT +3%")).toBeInTheDocument();
    expect(screen.getByText("ECM: CAL-1")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "fuel" })).toBeInTheDocument();
    expect(screen.getByText("equals_did vs 0x1134")).toBeInTheDocument();
    expect(screen.getByText("verified")).toHaveClass("text-ok");
  });

  it("re-verifies without recording, then shows each verdict", async () => {
    route();
    render(<CalibrationPage token="t" status={STATUS} />);
    await screen.findByText("WOT +3%");
    fireEvent.click(screen.getByRole("button", { name: "Re-verify" }));
    expect(await screen.findByText("PHI_CMD: pass — 1:1")).toBeInTheDocument();
    expect(bodyOf("POST", "/control/memory-channels/verify")).toEqual({
      record_revision: null,
      carry_reason: null,
    });
  });

  it("needs a carry reason before recording while a window member is unverifiable", async () => {
    route();
    render(<CalibrationPage token="t" status={STATUS} />);
    await screen.findByText("WOT +3%");
    const record = screen.getByRole("button", { name: "Record for ZS44A-20260920" });
    expect(record).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Reason to carry/), {
      target: { value: "same program" },
    });
    expect(record).toBeEnabled();
    fireEvent.click(record);
    await waitFor(() =>
      expect(bodyOf("POST", "/control/memory-channels/verify")).toEqual({
        record_revision: "ZS44A-20260920",
        carry_reason: "same program",
      }),
    );
  });

  it("refuses verification while a capture records", async () => {
    route();
    const capturing = {
      ...STATUS,
      capture_profile: { active: true, name: "tuning" },
    } as StatusResponse;
    render(<CalibrationPage token="t" status={capturing} />);
    await screen.findByText("WOT +3%");
    expect(screen.getByRole("button", { name: "Re-verify" })).toBeDisabled();
    expect(screen.getByText(/deferred while tuning is recording/)).toBeInTheDocument();
  });

  it("records a flash with typed fields", async () => {
    route();
    render(<CalibrationPage token="t" status={STATUS} />);
    await screen.findByText("WOT +3%");
    fireEvent.change(screen.getByPlaceholderText("Revision name"), {
      target: { value: "ZS44A-20260924" },
    });
    const when = document.querySelector('input[name="flashed_at"]') as HTMLInputElement;
    fireEvent.change(when, { target: { value: "2026-09-24T08:00" } });
    fireEvent.change(screen.getByPlaceholderText("Injector K"), { target: { value: "12.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Record flash" }));
    await waitFor(() =>
      expect(bodyOf("POST", "/control/calibration")).toEqual({
        revision: "ZS44A-20260924",
        flashed_at: "2026-09-24T08:00",
        calibration_id: null,
        k: 12.5,
        rom_file: null,
        rom_sha256: null,
        notes: "",
      }),
    );
  });

  it("describes an unreachable daemon in the shared words (R08.2)", async () => {
    route({
      "GET /control/calibration": new NetworkError("/control/calibration", new TypeError("x")),
    });
    render(<CalibrationPage token="t" status={STATUS} />);
    expect(await screen.findByText(/Can't reach the daemon/)).toBeInTheDocument();
  });

  it("describes a refused verification in the shared words (R08.2)", async () => {
    route({ "POST /control/memory-channels/verify": new HttpError(401, "") });
    render(<CalibrationPage token="t" status={STATUS} />);
    await screen.findByText("WOT +3%");
    fireEvent.click(screen.getByRole("button", { name: "Re-verify" }));
    expect(
      await screen.findByText("Not authorized — enter or check the control token."),
    ).toBeInTheDocument();
  });
});
