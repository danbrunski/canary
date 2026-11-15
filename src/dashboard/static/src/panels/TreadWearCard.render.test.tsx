import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { TireStatus } from "../types";

vi.mock("../api/http", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/http")>()),
  getTires: vi.fn(),
  recordTireEvent: vi.fn(),
}));

const api = await import("../api/http");
const { TreadWearCard } = await import("./TreadWearCard");

const status: TireStatus = {
  method_version: 1,
  series_start: null,
  series_reset_by: null,
  drives: [],
  current: null,
  baseline: null,
  estimate: null,
  caveats: [],
};

describe("TreadWearCard follow-up read (operator 2026-11-15: a failed one is loud)", () => {
  it("says so when the read after a recorded event fails (the event went through)", async () => {
    vi.mocked(api.getTires).mockResolvedValueOnce(status);
    vi.mocked(api.recordTireEvent).mockResolvedValue({} as never);
    render(<TreadWearCard token="t" />);
    await screen.findByTestId("tread-headline");

    vi.mocked(api.getTires).mockRejectedValue(new Error("pi went away"));
    fireEvent.change(screen.getByLabelText("value"), { target: { value: "12.1" } });
    fireEvent.click(screen.getByRole("button", { name: "record" }));
    expect(
      await screen.findByText("Done, but the refreshed view could not be loaded: pi went away"),
    ).toBeInTheDocument();
    expect(api.recordTireEvent).toHaveBeenCalledTimes(1);
  });
});
