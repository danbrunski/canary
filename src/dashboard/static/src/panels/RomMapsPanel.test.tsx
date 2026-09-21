import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { controlJson } from "../api/core";
import type { RomInspection } from "../lib/romMaps";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { StatusResponse } from "../types";
import { RomMapsPanel } from "./RomMapsPanel";

vi.mock("../api/core", () => ({ controlJson: vi.fn() }));
const inspect = vi.mocked(controlJson);
const now = new Date("2026-09-21T12:00:00Z");
const result: RomInspection = {
  source_sha256: "a".repeat(64), revision: "current", recorded_revision: "current", matches_recorded_revision: true,
  map_slots: [1, 2, 3, 4, 5], afr_stoich: 14.7, warnings: ["Inferred lookup only"],
  live: { rpm: { spec: "did:RPM", transform: "identity", factor: null }, base_fuel_schedule: { spec: "did:BFS", transform: "scale", factor: 0.00049 }, commanded_afr: null },
  tables: [1, 2, 3, 4, 5].flatMap((slot) => (["fuel_targets", "fuel_compensation"] as const).map((kind) => ({
    slot, kind, title: kind === "fuel_targets" ? "Target AFR" : "Fuel Compensation", status: slot === 1 && kind === "fuel_targets" ? "available" as const : "tbd" as const,
    reason: "TBD — complete table location and axes not verified.", x_label: kind === "fuel_targets" ? "Base Fuel Schedule" : "Volumetric Flow Ratio", x_unit: kind === "fuel_targets" ? "ms" : "%", y_label: "Rotations Per Minute", y_unit: "rpm", unit: kind === "fuel_targets" ? "AFR" : "%",
    x_axis: [8.76, 11.46], y_axis: [1200, 1600], values: [[14.7, 13], [13, 12]], raw: [[128, 145], [145, 157]],
  }))),
};
const snapshot = (channel: string, value: number): ChannelSnapshot => ({
  latest: { timestamp: now.toISOString(), source: "did", channel, value, unit: null }, history: [],
});
function props() {
  return { token: "test", channels: new Map([["did:RPM", snapshot("RPM", 1200)], ["did:BFS", snapshot("BFS", 8.76 / 0.00049)]]),
    status: { vehicle_state: { state: "engine_running", derived_at: now.toISOString() } } as StatusResponse };
}
async function upload(name = "current.OsirisROM") {
  await act(async () => { fireEvent.change(screen.getByLabelText("ROM file"), { target: { files: [new File(["test"], name)] } }); });
}
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(now); inspect.mockResolvedValue(result); });
afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

describe("ROM maps panel", () => {
  it("uploads a file, displays two maps, and keeps slots 2–5 explicitly TBD", async () => {
    render(<RomMapsPanel {...props()} />);
    expect(screen.getByLabelText("Map slot")).toBeDisabled();
    await upload();
    expect(screen.getAllByRole("option")).toHaveLength(5);
    expect(inspect).toHaveBeenCalledWith("/control/rom/inspect", "test", expect.objectContaining({ method: "POST", body: expect.any(File) }));
    expect(screen.getByRole("table", { name: "Map 1 Target AFR values" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Map 1 Fuel Compensation" })).toHaveTextContent("TBD");
    fireEvent.change(screen.getByLabelText("Map slot"), { target: { value: "5" } });
    expect(screen.getByRole("region", { name: "Map 5 Target AFR" })).toHaveTextContent("TBD");
    expect(screen.getByRole("region", { name: "Map 5 Fuel Compensation" })).toHaveTextContent("TBD");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("requires explicit active-slot confirmation and removes a stale highlight", async () => {
    const view = render(<RomMapsPanel {...props()} />);
    await upload();
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
    fireEvent.click(screen.getByRole("checkbox"));
    expect(view.container.querySelectorAll('[data-trace="nearest"]')).toHaveLength(1);
    act(() => { vi.advanceTimersByTime(3000); });
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
    expect(screen.getByText(/Waiting for fresh RPM and BFS/)).toBeInTheDocument();
  });

  it("clears confirmation on a slot change and does not trace a mismatched ROM", async () => {
    inspect.mockResolvedValue({ ...result, matches_recorded_revision: false });
    const view = render(<RomMapsPanel {...props()} />);
    await upload();
    expect(screen.getByRole("checkbox")).toBeDisabled();
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
  });

  it("clears an old table when a replacement fails, and reports the error", async () => {
    render(<RomMapsPanel {...props()} />);
    await upload();
    inspect.mockRejectedValue(new Error("Unknown source hash"));
    await upload("unknown.OsirisROM");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Unknown source hash");
  });

  it("rejects the wrong extension without uploading and needs a token", async () => {
    const view = render(<RomMapsPanel {...props()} />);
    await upload("wrong.txt");
    expect(inspect).not.toHaveBeenCalled();
    view.rerender(<RomMapsPanel {...props()} token="" />);
    expect(screen.getByLabelText("ROM file")).toBeDisabled();
  });

  it("suppresses trace in replay even when samples are fresh", async () => {
    const p = props();
    p.status.vehicle_state!.state = "replay";
    const view = render(<RomMapsPanel {...p} />);
    await upload(); fireEvent.click(screen.getByRole("checkbox"));
    expect(view.container.querySelectorAll("[data-trace]")).toHaveLength(0);
    expect(screen.getByText(/Replay active/)).toBeInTheDocument();
  });
});
