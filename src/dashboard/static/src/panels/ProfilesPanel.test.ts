import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { VehicleProfilesResponse } from "../types";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(["getProfiles", "updateProfileName"]);

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
    expect(screen.getByText(/Enter the control token/)).toBeInTheDocument();
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
