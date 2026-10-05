import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { VehicleLedgerEvent } from "../types";

vi.mock("../api/http", () => ({
  getVehicleLedger: vi.fn(),
  saveVehicleEvent: vi.fn(),
  deleteVehicleEvent: vi.fn(),
}));

const api = await import("../api/http");
const { VehicleLedgerPage, draftFromForm, emptyForm, formFromEvent, tyreFact } =
  await import("./VehicleLedgerPage");

const gauge: VehicleLedgerEvent = {
  id: "a4078578",
  recorded_at: "2026-10-04T18:52:19+00:00",
  occurred_at: "2026-10-04",
  odometer_km: null,
  kind: "maintenance",
  title: "Tread depth gauge reading: 15.0 mm",
  details: "",
  affected_channels: [],
  corpus_boundary: false,
  tyres: { action: "tread_measured", tread_depth_mm: 15.04, pressure_psi: null },
};

afterEach(() => vi.clearAllMocks());

describe("ledger form", () => {
  it("edits a gauge reading's depth and keeps its id, so the save replaces it", () => {
    const form = { ...formFromEvent(gauge), tyreValue: "12.07", title: "Tread: 12.1 mm" };
    const draft = draftFromForm(form);
    expect(draft).toMatchObject({
      event_id: "a4078578",
      title: "Tread: 12.1 mm",
      tyres: { action: "tread_measured", tread_depth_mm: 12.07 },
    });
  });

  it("refuses what the server would reject, by name", () => {
    expect(draftFromForm(emptyForm("2026-10-04"))).toBe("a title is required");
    expect(draftFromForm({ ...formFromEvent(gauge), tyreValue: "abc" })).toBe(
      "the tread depth must be a number of mm",
    );
    expect(draftFromForm({ ...formFromEvent(gauge), odometerKm: "-3" })).toBe(
      "the odometer must be a number",
    );
  });

  it("a new entry carries no id and splits its channels", () => {
    const draft = draftFromForm({
      ...emptyForm("2026-10-04"),
      title: "Cold air intake",
      kind: "mod",
      affectedChannels: "obd:MAF, obd:INTAKE_TEMP,",
    });
    expect(draft).not.toHaveProperty("event_id");
    expect(draft).toMatchObject({ affected_channels: ["obd:MAF", "obd:INTAKE_TEMP"] });
  });

  it("an edit keeps the event's intervention classification (E01.4)", () => {
    const pump: VehicleLedgerEvent = { ...gauge, kind: "mod", tyres: null, intervention: "fuel" };
    expect(draftFromForm(formFromEvent(pump))).toMatchObject({ intervention: "fuel" });
    expect(draftFromForm(formFromEvent(gauge))).not.toHaveProperty("intervention");
  });

  it("names tyre facts", () => {
    expect(tyreFact(gauge.tyres)).toBe("tread 15.04 mm");
    expect(tyreFact(null)).toBeNull();
  });
});

describe("VehicleLedgerPage", () => {
  it("saves an edit under the entry's id and deletes after confirming", async () => {
    vi.mocked(api.getVehicleLedger).mockResolvedValue({ events: [gauge], count: 1 });
    vi.mocked(api.saveVehicleEvent).mockResolvedValue({ event: gauge });
    vi.mocked(api.deleteVehicleEvent).mockResolvedValue({
      deleted: { id: gauge.id, title: gauge.title },
    });
    render(<VehicleLedgerPage token="t" />);
    expect(await screen.findByText(/Tread depth gauge reading: 15.0 mm/)).toBeInTheDocument();
    expect(screen.getByText(/tread 15.04 mm/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("tread depth (mm)"), { target: { value: "12.07" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.saveVehicleEvent).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveVehicleEvent).mock.calls[0][1]).toMatchObject({
      event_id: gauge.id,
      tyres: { tread_depth_mm: 12.07 },
    });

    await screen.findByRole("button", { name: "Delete" });
    const confirm = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(api.deleteVehicleEvent).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteVehicleEvent).toHaveBeenCalledWith("t", gauge.id));
    confirm.mockRestore();
  });
});
