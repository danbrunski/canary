import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { VehicleProfilesResponse } from "../types";

const mocks = vi.hoisted(() => ({
  getProfiles: vi.fn(),
  updateProfileName: vi.fn(),
}));

vi.mock("../api/http", () => ({
  ...mocks,
  HttpError: class extends Error {
    status = 0;
    body = "";
  },
}));

const { ProfilesPanel } = await import("./ProfilesPanel");

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
});
afterEach(() => vi.clearAllMocks());

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
    },
    {
      slug: "nissan-xterra-2008",
      vin: "5N1AN08U08...",
      name: "2008 Xterra",
      make: "Nissan",
      model: "Xterra",
      year: 2008,
      parameters: {},
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
