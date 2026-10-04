import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../../api/core";
import { ENTRY_K106, ENTRY_TUNED, SHA_K106, SHA_TUNED } from "../../test/romLibraryFixtures";
import type { RomCompareResponse } from "../../types/romLibrary";
import { RomCompare } from "./RomCompare";

vi.mock("../../api/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/core")>()),
  controlJson: vi.fn(),
}));
const control = vi.mocked(controlJson);

/** k106 → 20260920 as the 2026-10-04 map records it: the two checksum words
 *  and one Fuel Targets cell (row 7, col 6: 11.61 → 11.54 AFR). */
const K106_TO_TUNED: RomCompareResponse = {
  a: SHA_K106,
  b: SHA_TUNED,
  changed_bytes: 3,
  ranges: [
    {
      start: 0x009598,
      end: 0x0095a3,
      length: 12,
      changed: 2,
      labels: ["Checksum XOR word", "Checksum sum word"],
      cells: [],
    },
    {
      start: 0x0e025c,
      end: 0x0e025c,
      length: 1,
      changed: 1,
      labels: ["Fuel Targets (Map 1)", "UpRev map slots 1–5"],
      cells: [
        {
          table: "fuel_targets_slot",
          table_label: "Fuel Targets (Map 1)",
          slot: 1,
          address: 0x0e025c,
          unit: "AFR",
          row: 7,
          col: 6,
          old: 162,
          new: 163,
          old_decoded: 11.614814,
          new_decoded: 11.543558,
        },
      ],
    },
  ],
};

afterEach(() => vi.clearAllMocks());

async function compare() {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
  });
}

describe("ROM compare (W08.6)", () => {
  it("compares older → newer by default and lists ranges, labels and per-cell values", async () => {
    control.mockResolvedValue(K106_TO_TUNED);
    render(<RomCompare token="t" entries={[ENTRY_TUNED, ENTRY_K106]} />);
    expect(screen.getByLabelText("Compare from")).toHaveValue(SHA_K106);
    expect(screen.getByLabelText("Compare to")).toHaveValue(SHA_TUNED);
    await compare();
    expect(control).toHaveBeenCalledWith(
      `/control/rom/compare?a=${SHA_K106}&b=${SHA_TUNED}`,
      "t",
      expect.anything(),
    );
    const result = within(screen.getByLabelText("Compare result"));
    expect(result.getByText("k106.bin → Running tune: 3 bytes changed in 2 ranges.")).toBeVisible();
    expect(result.getByText("0x009598–0x0095A3")).toBeInTheDocument();
    expect(result.getByText("Checksum XOR word · Checksum sum word")).toBeInTheDocument();
    expect(result.getByText("2 of 12 B changed")).toBeInTheDocument();
    expect(result.getByText("0x0E025C")).toBeInTheDocument();
    expect(result.getByText("Fuel Targets (Map 1) · UpRev map slots 1–5")).toBeInTheDocument();
    expect(result.getByText("1 B")).toBeInTheDocument();
    // Only table ranges carry a cell list.
    expect(result.getAllByRole("table", { name: "Changed cells" })).toHaveLength(1);
    const cells = within(result.getByRole("table", { name: "Changed cells" }));
    const row = cells.getAllByRole("row")[1];
    expect(within(row).getByText("Fuel Targets (Map 1)")).toBeInTheDocument();
    expect(within(row).getByText("11.61 AFR (raw 162)")).toBeInTheDocument();
    expect(within(row).getByText("11.54 AFR (raw 163)")).toBeInTheDocument();
  });

  it("says why the ranges are unlabelled when the map covers only one image", async () => {
    control.mockResolvedValue({
      a: SHA_K106,
      b: SHA_TUNED,
      changed_bytes: 40000,
      map_applied: false,
      map_reason: "no map definition for calibration ZS04C",
      ranges: [{ start: 0x8204, end: 0x9cfff, length: 0x94dfc, changed: 40000, labels: [] }],
    });
    render(<RomCompare token="t" entries={[ENTRY_TUNED, ENTRY_K106]} />);
    await compare();
    expect(
      screen.getByText("Ranges are unlabelled: no map definition for calibration ZS04C."),
    ).toBeVisible();
    expect(screen.getByText("unmapped bytes")).toBeVisible();
  });

  it("reports identical images as identical", async () => {
    control.mockResolvedValue({ a: SHA_K106, b: SHA_TUNED, ranges: [] });
    render(<RomCompare token="t" entries={[ENTRY_TUNED, ENTRY_K106]} />);
    await compare();
    expect(screen.getByText(/the images are identical/)).toBeVisible();
  });

  it("falls back to the spanned byte count when the changed count is not reported", async () => {
    control.mockResolvedValue({ ...K106_TO_TUNED, changed_bytes: undefined });
    render(<RomCompare token="t" entries={[ENTRY_TUNED, ENTRY_K106]} />);
    await compare();
    expect(screen.getByText(/2 ranges spanning 13 bytes\./)).toBeVisible();
  });

  it("refuses to compare an image with itself", () => {
    render(<RomCompare token="t" entries={[ENTRY_TUNED, ENTRY_K106]} />);
    fireEvent.change(screen.getByLabelText("Compare from"), { target: { value: SHA_TUNED } });
    expect(screen.getByRole("button", { name: "Compare" })).toBeDisabled();
    expect(screen.getByText("Pick two different images.")).toBeVisible();
  });

  it("shows the server's refusal verbatim", async () => {
    control.mockRejectedValue(
      new HttpError(404, JSON.stringify({ detail: `no library image ${SHA_K106}` })),
    );
    render(<RomCompare token="t" entries={[ENTRY_TUNED, ENTRY_K106]} />);
    await compare();
    expect(screen.getByText(`no library image ${SHA_K106}`)).toBeVisible();
  });
});
