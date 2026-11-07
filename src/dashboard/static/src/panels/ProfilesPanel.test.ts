import { createElement } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { VehicleParameterField, VehicleProfilesResponse } from "../types";
import { HttpErrorStub, mockHttp } from "../test/harness";

const mocks = mockHttp([
  "getProfiles",
  "updateProfileName",
  "updateProfile",
  "revertProfile",
  "deleteProfile",
  "restoreProfile",
  "readVin",
]);

const { ProfilesPanel } = await import("./ProfilesPanel");

const PROFILES: VehicleProfilesResponse = {
  active_slug: "jn8az18u1...",
  count: 2,
  profiles: [
    {
      slug: "jn8az18u1...",
      vin: "JN8AZ18U1...",
      name: "",
      make: "",
      model: "",
      year: null,
      parameters: {},
      calibration_id: "",
      cvn: "",
    },
    {
      slug: "nissan-xterra-2008",
      vin: "5N1AN08U08...",
      name: "2008 Xterra",
      make: "Nissan",
      model: "Xterra",
      year: 2008,
      parameters: {},
      calibration_id: "CAL123456789ABCD",
      cvn: "1A2B3C4D",
    },
  ],
};

describe("ProfilesPanel", () => {
  it("prompts for a token without one", () => {
    render(createElement(ProfilesPanel, { token: "" }));
    expect(screen.getByText(/needs the control token/)).toBeInTheDocument();
    expect(mocks.getProfiles).not.toHaveBeenCalled();
  });

  it("lists profiles, marks the active one, and shows the unnamed placeholder", async () => {
    mocks.getProfiles.mockResolvedValue(PROFILES);
    render(createElement(ProfilesPanel, { token: "secret" }));
    expect(await screen.findByText("2008 Xterra")).toBeInTheDocument();
    expect(screen.getByText("(unnamed)")).toBeInTheDocument();
    // The active profile (the freshly-seen, unnamed one) carries the badge.
    expect(screen.getByText("active")).toBeInTheDocument();
  });

  it("shows the calibration ID / CVN for a profile that has one, and a placeholder otherwise", async () => {
    mocks.getProfiles.mockResolvedValue(PROFILES);
    render(createElement(ProfilesPanel, { token: "secret" }));
    expect(await screen.findByText(/Cal ID CAL123456789ABCD/)).toBeInTheDocument();
    expect(screen.getByText(/CVN 1A2B3C4D/)).toBeInTheDocument();
    expect(screen.getAllByText(/Cal ID — · CVN —/).length).toBeGreaterThan(0);
  });

  it("saves a new display name for an unnamed profile", async () => {
    mocks.getProfiles.mockResolvedValue(PROFILES);
    mocks.updateProfileName.mockResolvedValue({
      profile: { ...PROFILES.profiles[0], name: "Daily" },
    });
    render(createElement(ProfilesPanel, { token: "secret" }));
    fireEvent.click(await screen.findByRole("button", { name: "Name it" }));
    const input = screen.getByLabelText("Display name for jn8az18u1...");
    fireEvent.change(input, { target: { value: "Daily" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.updateProfileName).toHaveBeenCalledOnce());
    expect(mocks.updateProfileName).toHaveBeenCalledWith("secret", "jn8az18u1...", "Daily");
  });
});

const FIELDS: VehicleParameterField[] = [
  {
    name: "mass_kg",
    label: "Vehicle mass",
    unit: "kg",
    kind: "number",
    evidence_field: "mass_evidence",
    evidence_for: [],
  },
  {
    name: "mass_evidence",
    label: "Mass evidence",
    unit: "",
    kind: "evidence",
    evidence_field: null,
    evidence_for: ["mass_kg"],
  },
  {
    name: "usual_payload_kg",
    label: "Usual payload",
    unit: "kg",
    kind: "number",
    evidence_field: null,
    evidence_for: [],
  },
];

const NA_VIN = "1M8GDM9AXKP042788";

const EDITABLE: VehicleProfilesResponse = {
  active_slug: "xterra",
  count: 1,
  profiles: [
    {
      slug: "xterra",
      vin: "",
      name: "Xterra",
      make: "Nissan",
      model: "Xterra",
      year: 2008,
      parameters: { mass_kg: 2184.4, mass_evidence: "itemised estimate", usual_payload_kg: 127 },
      calibration_id: "",
      cvn: "",
      tracked: true,
      overrides: ["parameters.usual_payload_kg"],
    },
  ],
  in_use: {
    make: "Nissan",
    model: "Xterra",
    year: 2008,
    vin: "",
    parameters: { mass_kg: 2184.4, usual_payload_kg: 120 },
  },
  pending_restart: ["parameters.usual_payload_kg"],
  restart_required: true,
  applies: "Restart the daemon for the change to reach the dyno.",
  resolution: { slug: "xterra", how: "no_vin_read", vin_read: null },
  parameter_fields: FIELDS,
};

const EDIT_RESULT = {
  profile: EDITABLE.profiles[0],
  changed: ["parameters.mass_kg", "parameters.mass_evidence"],
  warnings: [],
  vin_resolution: null,
  active: true,
  pending_restart: ["parameters.mass_kg"],
  restart_required: true,
  applies: "Restart the daemon.",
};

describe("ProfilesPanel parameters", () => {
  it("shows every parameter with its unit, evidence, origin and a pending restart", async () => {
    mocks.getProfiles.mockResolvedValue(EDITABLE);
    render(createElement(ProfilesPanel, { token: "secret" }));
    const mass = await screen.findByTestId("param-mass_kg");
    expect(mass).toHaveTextContent("2184.4 kg");
    expect(mass).toHaveTextContent("tracked");
    expect(screen.getByTestId("param-mass_evidence")).toHaveTextContent("itemised estimate");
    const payload = screen.getByTestId("param-usual_payload_kg");
    expect(payload).toHaveTextContent("this machine");
    expect(payload).toHaveTextContent("restart pending");
    expect(screen.getByTestId("profiles-restart-pending")).toHaveTextContent(
      "parameters.usual_payload_kg",
    );
    expect(screen.getByText(/read no VIN/)).toBeInTheDocument();
  });

  it("reverts one overridden parameter, and all overrides", async () => {
    mocks.getProfiles.mockResolvedValue(EDITABLE);
    mocks.revertProfile.mockResolvedValue({
      ...EDIT_RESULT,
      changed: ["parameters.usual_payload_kg"],
    });
    render(createElement(ProfilesPanel, { token: "secret" }));
    fireEvent.click(await screen.findByRole("button", { name: "Revert Usual payload" }));
    await waitFor(() =>
      expect(mocks.revertProfile).toHaveBeenCalledWith("secret", "xterra", [
        "parameters.usual_payload_kg",
      ]),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Revert all" }));
    await waitFor(() =>
      expect(mocks.revertProfile).toHaveBeenLastCalledWith("secret", "xterra", undefined),
    );
  });

  it("asks for new evidence before a changed mass can be saved", async () => {
    mocks.getProfiles.mockResolvedValue(EDITABLE);
    mocks.updateProfile.mockResolvedValue(EDIT_RESULT);
    render(createElement(ProfilesPanel, { token: "secret" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Vehicle mass"), { target: { value: "2230" } });
    const save = screen.getByRole("button", { name: "Save changes" });
    expect(save).toBeDisabled();
    expect(screen.getByTestId("profile-editor-blockers")).toHaveTextContent(
      "mass_kg changed: give a new mass_evidence",
    );
    fireEvent.change(screen.getByLabelText("Mass evidence"), {
      target: { value: "scale ticket 2026-10-10" },
    });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(mocks.updateProfile).toHaveBeenCalledOnce());
    expect(mocks.updateProfile).toHaveBeenCalledWith("secret", "xterra", {
      parameters: { mass_kg: 2230, mass_evidence: "scale ticket 2026-10-10" },
    });
    expect(await screen.findByText(/^Saved: parameters.mass_kg/)).toHaveTextContent(
      "Restart the daemon.",
    );
  });

  it("saves a tire size picked from the sidewall code without touching the evidence", async () => {
    const tireFields: VehicleParameterField[] = [
      {
        name: "tire_diameter_m",
        label: "Tire diameter",
        unit: "m",
        kind: "number",
        evidence_field: null,
        evidence_for: [],
      },
      {
        name: "final_drive",
        label: "Final drive",
        unit: "",
        kind: "number",
        evidence_field: "driveline_evidence",
        evidence_for: [],
      },
      {
        name: "tire_loaded_circumference_m",
        label: "Loaded tire circumference",
        unit: "m",
        kind: "number",
        evidence_field: null,
        evidence_for: [],
      },
      {
        name: "driveline_evidence",
        label: "Driveline evidence",
        unit: "",
        kind: "evidence",
        evidence_field: null,
        evidence_for: ["final_drive", "gearbox_ratios"],
      },
    ];
    const [profile] = EDITABLE.profiles;
    mocks.getProfiles.mockResolvedValue({
      ...EDITABLE,
      profiles: [
        {
          ...profile,
          parameters: {
            tire_diameter_m: 0.804,
            final_drive: 4.08,
            tire_loaded_circumference_m: 2.559,
            driveline_evidence: "axle tag",
          },
        },
      ],
      parameter_fields: tireFields,
    });
    mocks.updateProfile.mockResolvedValue(EDIT_RESULT);
    render(createElement(ProfilesPanel, { token: "secret" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Tire size"), { target: { value: "285/75R16" } });
    expect(screen.getByLabelText("Tire diameter")).toHaveValue("0.834");
    expect(screen.getByTestId("tire-size-result")).toHaveTextContent("0.834 m diameter");
    fireEvent.change(screen.getByLabelText("Loaded tire circumference"), {
      target: { value: "2.571" },
    });
    expect(screen.queryByTestId("profile-editor-blockers")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(mocks.updateProfile).toHaveBeenCalledOnce());
    expect(mocks.updateProfile).toHaveBeenCalledWith("secret", "xterra", {
      parameters: { tire_diameter_m: 0.834, tire_loaded_circumference_m: 2.571 },
    });
  });

  it("shows the daemon's refusal in full", async () => {
    mocks.getProfiles.mockResolvedValue(EDITABLE);
    const refusal = Object.assign(new HttpErrorStub("HTTP 409"), {
      status: 409,
      body: JSON.stringify({ detail: "VIN 1M8GDM9AXKP042788 already identifies profile 'work'" }),
    });
    mocks.updateProfile.mockRejectedValue(refusal);
    render(createElement(ProfilesPanel, { token: "secret" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("VIN for xterra"), { target: { value: NA_VIN } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already identifies profile 'work'");
  });

  it("reads the VIN from the vehicle into the field without saving it", async () => {
    mocks.getProfiles.mockResolvedValue(EDITABLE);
    mocks.readVin.mockResolvedValue({
      vin_read: {
        ok: true,
        vin: NA_VIN,
        refusal: null,
        detail: "read from the connected vehicle",
        raw: NA_VIN,
        negative_response_code: null,
        held_by: null,
      },
    });
    render(createElement(ProfilesPanel, { token: "secret" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Read VIN from vehicle" }));
    expect(await screen.findByTestId("vin-read")).toHaveTextContent(`Read ${NA_VIN}`);
    expect(screen.getByLabelText("VIN for xterra")).toHaveValue(NA_VIN);
    expect(screen.getByTestId("vin-check")).toHaveTextContent("Check digit valid");
    expect(mocks.updateProfile).not.toHaveBeenCalled();
  });

  it("shows why the VIN could not be read and leaves manual entry open", async () => {
    mocks.getProfiles.mockResolvedValue(EDITABLE);
    mocks.readVin.mockResolvedValue({
      vin_read: {
        ok: false,
        vin: null,
        refusal: "key_off",
        detail: "key off: the vehicle is parked",
        raw: null,
        negative_response_code: null,
      },
    });
    render(createElement(ProfilesPanel, { token: "secret" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Read VIN from vehicle" }));
    expect(await screen.findByTestId("vin-read")).toHaveTextContent(
      "Could not read the VIN (key_off): key off: the vehicle is parked. Type it in instead.",
    );
    const input = screen.getByLabelText("VIN for xterra");
    fireEvent.change(input, { target: { value: "1M8GDM9A1KP042788" } });
    expect(screen.getByTestId("vin-check")).toHaveTextContent("Check digit mismatch");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("explains why the active profile's VIN is fixed when it was read at startup", async () => {
    mocks.getProfiles.mockResolvedValue({
      ...EDITABLE,
      resolution: { slug: "xterra", how: "vin_matched", vin_read: NA_VIN },
    });
    render(createElement(ProfilesPanel, { token: "secret" }));
    expect(await screen.findByText(/read VIN 1M8GDM9AXKP042788 and matched/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByText(/the daemon refuses it/)).toBeInTheDocument();
  });
});

const ACTIVE_REFUSAL =
  "the daemon is running on it (the active profile); restart it elsewhere first";
const BLOCK_REFUSAL =
  "canary.yaml's vehicle: block names this profile; change the vehicle: block first";

function profileRow(
  slug: string,
  over: Partial<VehicleProfilesResponse["profiles"][number]> = {},
): VehicleProfilesResponse["profiles"][number] {
  return {
    slug,
    vin: "",
    name: slug,
    make: "Nissan",
    model: "Xterra",
    year: 2013,
    parameters: {},
    calibration_id: "",
    cvn: "",
    tracked: true,
    overrides: [],
    delete_refusal: null,
    ...over,
  };
}

const DELETABLE: VehicleProfilesResponse = {
  active_slug: "xterra-2013",
  count: 4,
  profiles: [
    profileRow("xterra-2013", { delete_refusal: ACTIVE_REFUSAL }),
    profileRow("nissan-xterra-2008", { name: "2008 Xterra" }),
    profileRow("block-truck", { delete_refusal: BLOCK_REFUSAL }),
    profileRow("nissan-2013", { name: "", tracked: false }),
  ],
  deleted: [],
};

function deleteButton(slug: string): HTMLElement {
  return within(screen.getByTestId(`profile-${slug}`)).getByRole("button", { name: "Delete" });
}

describe("ProfilesPanel delete", () => {
  it("disables Delete with the daemon's reason for the active and the vehicle: block profiles", async () => {
    mocks.getProfiles.mockResolvedValue(DELETABLE);
    render(createElement(ProfilesPanel, { token: "secret" }));
    await screen.findByTestId("profile-xterra-2013");
    expect(deleteButton("xterra-2013")).toBeDisabled();
    expect(deleteButton("xterra-2013")).toHaveAttribute("title", ACTIVE_REFUSAL);
    expect(deleteButton("block-truck")).toBeDisabled();
    expect(deleteButton("block-truck")).toHaveAttribute("title", BLOCK_REFUSAL);
    expect(deleteButton("nissan-xterra-2008")).toBeEnabled();
    expect(deleteButton("nissan-2013")).toBeEnabled();
  });

  it("confirms in the page, never with window.confirm, and says what a tracked delete does", async () => {
    const confirm = vi.spyOn(window, "confirm");
    mocks.getProfiles.mockResolvedValue(DELETABLE);
    render(createElement(ProfilesPanel, { token: "secret" }));
    await screen.findByTestId("profile-nissan-xterra-2008");
    fireEvent.click(deleteButton("nissan-xterra-2008"));
    const group = screen.getByRole("group", { name: "Confirm delete nissan-xterra-2008" });
    expect(group).toHaveTextContent("hidden on this machine only");
    expect(group).toHaveTextContent("stays in git");
    expect(group).toHaveTextContent("edits to it (name, VIN, parameters) are dropped");
    expect(group).toHaveTextContent("restore it");
    expect(group).toHaveTextContent("Recorded data (sessions, models, ledgers) is not deleted");
    expect(mocks.deleteProfile).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
    fireEvent.click(within(group).getByRole("button", { name: "Not now" }));
    expect(screen.queryByRole("group", { name: /Confirm delete/ })).not.toBeInTheDocument();
    expect(mocks.deleteProfile).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  it("says a machine-only profile cannot be restored", async () => {
    mocks.getProfiles.mockResolvedValue(DELETABLE);
    render(createElement(ProfilesPanel, { token: "secret" }));
    await screen.findByTestId("profile-nissan-2013");
    fireEvent.click(deleteButton("nissan-2013"));
    const group = screen.getByRole("group", { name: "Confirm delete nissan-2013" });
    expect(group).toHaveTextContent("exists only on this machine");
    expect(group).toHaveTextContent("cannot be restored");
    expect(group).not.toHaveTextContent("stays in git");
    expect(group).toHaveTextContent("Recorded data (sessions, models, ledgers) is not deleted");
  });

  it("deletes after confirmation, refreshes, and lists the data it left", async () => {
    mocks.getProfiles.mockResolvedValue(DELETABLE);
    mocks.deleteProfile.mockResolvedValue({
      deleted: "nissan-xterra-2008",
      tracked: true,
      data_left: ["/data/canary/sessions/nissan-xterra-2008", "/repo/data/nissan-xterra-2008"],
    });
    render(createElement(ProfilesPanel, { token: "secret" }));
    await screen.findByTestId("profile-nissan-xterra-2008");
    expect(mocks.getProfiles).toHaveBeenCalledTimes(1);
    fireEvent.click(deleteButton("nissan-xterra-2008"));
    fireEvent.click(screen.getByRole("button", { name: "Delete profile" }));
    await waitFor(() =>
      expect(mocks.deleteProfile).toHaveBeenCalledWith("secret", "nissan-xterra-2008"),
    );
    const result = await screen.findByTestId("profile-deleted-result");
    expect(result).toHaveTextContent("still in git");
    expect(result).toHaveTextContent("/data/canary/sessions/nissan-xterra-2008");
    expect(result).toHaveTextContent("/repo/data/nissan-xterra-2008");
    expect(result).toHaveTextContent("not deleted");
    await waitFor(() => expect(mocks.getProfiles).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("group", { name: /Confirm delete/ })).not.toBeInTheDocument();
    fireEvent.click(within(result).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("profile-deleted-result")).not.toBeInTheDocument();
  });

  it("says when a deleted profile left no data", async () => {
    mocks.getProfiles.mockResolvedValue(DELETABLE);
    mocks.deleteProfile.mockResolvedValue({
      deleted: "nissan-2013",
      tracked: false,
      data_left: [],
    });
    render(createElement(ProfilesPanel, { token: "secret" }));
    await screen.findByTestId("profile-nissan-2013");
    fireEvent.click(deleteButton("nissan-2013"));
    fireEvent.click(screen.getByRole("button", { name: "Delete profile" }));
    expect(await screen.findByTestId("profile-deleted-result")).toHaveTextContent(
      "No recorded data was found",
    );
  });

  it("shows the daemon's refusal and keeps the profile when the delete is refused", async () => {
    mocks.getProfiles.mockResolvedValue(DELETABLE);
    mocks.deleteProfile.mockRejectedValue(
      Object.assign(new HttpErrorStub("HTTP 409"), {
        status: 409,
        body: JSON.stringify({ detail: BLOCK_REFUSAL }),
      }),
    );
    render(createElement(ProfilesPanel, { token: "secret" }));
    await screen.findByTestId("profile-nissan-xterra-2008");
    fireEvent.click(deleteButton("nissan-xterra-2008"));
    fireEvent.click(screen.getByRole("button", { name: "Delete profile" }));
    expect(await screen.findByText(/change the vehicle: block first/)).toBeInTheDocument();
    expect(screen.queryByTestId("profile-deleted-result")).not.toBeInTheDocument();
    expect(screen.getByTestId("profile-nissan-xterra-2008")).toBeInTheDocument();
  });

  it("lists the profiles hidden on this machine and restores one", async () => {
    mocks.getProfiles.mockResolvedValue({
      ...DELETABLE,
      deleted: [
        {
          slug: "nissan-xterra-2008",
          name: "2008 Xterra",
          make: "Nissan",
          model: "Xterra",
          year: 2008,
        },
      ],
    });
    mocks.restoreProfile.mockResolvedValue({ profile: profileRow("nissan-xterra-2008") });
    render(createElement(ProfilesPanel, { token: "secret" }));
    const hidden = await screen.findByTestId("profiles-hidden");
    expect(hidden).toHaveTextContent("Hidden on this machine");
    expect(screen.getByTestId("hidden-nissan-xterra-2008")).toHaveTextContent("2008 Xterra");
    fireEvent.click(within(hidden).getByRole("button", { name: "Restore nissan-xterra-2008" }));
    await waitFor(() =>
      expect(mocks.restoreProfile).toHaveBeenCalledWith("secret", "nissan-xterra-2008"),
    );
    expect(await screen.findByText(/Restored nissan-xterra-2008/)).toBeInTheDocument();
    await waitFor(() => expect(mocks.getProfiles).toHaveBeenCalledTimes(2));
  });

  it("shows no hidden list when nothing is hidden", async () => {
    mocks.getProfiles.mockResolvedValue(DELETABLE);
    render(createElement(ProfilesPanel, { token: "secret" }));
    await screen.findByTestId("profile-xterra-2013");
    expect(screen.queryByTestId("profiles-hidden")).not.toBeInTheDocument();
  });
});
