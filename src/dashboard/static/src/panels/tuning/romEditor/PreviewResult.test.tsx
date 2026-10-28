import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fuelCompTable } from "../../../test/romLibraryFixtures";
import { LEVERS_FUEL_COMP } from "../../../test/romLeverFixtures";
import type { RomEditExportResponse, RomEditPreview } from "../../../types/romEditor";
import { type ExportState, PreviewResult } from "./PreviewResult";

afterEach(cleanup);

/** A traced, exportable preview of one Fuel Comp cell, Map 1. */
function traced(): RomEditPreview {
  const after = fuelCompTable(1);
  after.raw = after.raw.map((row) => [...row]);
  after.raw[0][0] = 122;
  return {
    tool: "rom_editor",
    tool_version: "1",
    spec: { label: "one cell" },
    spec_yaml: "schema_version: 1\n",
    export: { ok: true, reason: null, message: null, tables: [] },
    spec_sha256: "5".repeat(64),
    candidate_sha256: "c".repeat(64),
    provenance: {
      kind: "simulation_candidate",
      flashable: false,
      label: "one cell",
      base_sha256: "b".repeat(64),
      candidate_sha256: "c".repeat(64),
      spec_sha256: "5".repeat(64),
      cells: [
        { table: "fuel_compensation", slot: 1, row: 0, col: 0, address: 0, old: 120, new: 122 },
      ],
      ranges: [],
      effect: {
        outcome: "traced",
        slot: 1,
        slot_source: "default_slot",
        changed_bytes: 1,
        changed_nodes: ["fuel_compensation_lookup"],
        moved_lookups: [],
      },
    },
    tables: [after],
    levers: LEVERS_FUEL_COMP,
    sim_base: { ok: true, status: "matched", revision: "ZS44A-20260920", reason: null },
  };
}

function renderPreview(preview: RomEditPreview) {
  const onDownload = vi.fn();
  render(
    <PreviewResult preview={preview} baseTables={[fuelCompTable(1)]} onDownload={onDownload} />,
  );
  return { result: screen.getByRole("region", { name: "Preview result" }), onDownload };
}

describe("PreviewResult: editable is not simulatable, and exported needs verified", () => {
  it("says an untraced edit is not traced, never that it reaches nothing", () => {
    const { provenance } = traced();
    const preview: RomEditPreview = {
      ...traced(),
      provenance: { ...provenance, effect: null },
      levers: null,
      sim_base: { ok: false, status: "unrecognised", revision: null, reason: "sim_kit_undeclared" },
    };
    const { result } = renderPreview(preview);
    expect(within(result).getByText(/1 cell changed\. Spec/)).toBeVisible();
    expect(within(result).getByText(/Not traced: this vehicle has no simulator/)).toBeVisible();
    expect(within(result).queryByText(/feeds no node/)).toBeNull();
    expect(
      within(result).queryByRole("region", { name: "What this edit can and cannot show" }),
    ).toBeNull();
    expect(within(result).getByRole("button", { name: "Download spec" })).toBeVisible();
  });

  it("offers no download for an edit to an unverified table, and names the table", () => {
    const preview: RomEditPreview = {
      ...traced(),
      spec_yaml: null,
      export: {
        ok: false,
        reason: "table_not_verified",
        message: "['fuel_compensation'] is not confidence: verified",
        tables: ["fuel_compensation"],
      },
    };
    const { result } = renderPreview(preview);
    expect(within(result).queryByRole("button", { name: "Download spec" })).toBeNull();
    expect(
      within(result).getByText(/No spec to download: fuel_compensation is not verified/),
    ).toBeVisible();
    expect(
      within(result).getByRole("region", { name: "What this edit can and cannot show" }),
    ).toBeVisible();
    // The preview itself still shows what changed.
    expect(within(result).getByText(/1 cell changed, in 1 byte/)).toBeVisible();
  });
});

describe("PreviewResult: Export to library", () => {
  function renderExport(preview: RomEditPreview, exportState: ExportState) {
    const onExport = vi.fn();
    const onDownloadImage = vi.fn();
    render(
      <PreviewResult
        preview={preview}
        baseTables={[fuelCompTable(1)]}
        onDownload={vi.fn()}
        exportState={exportState}
        onExport={onExport}
        onDownloadImage={onDownloadImage}
      />,
    );
    return {
      result: screen.getByRole("region", { name: "Preview result" }),
      onExport,
      onDownloadImage,
    };
  }

  it("offers the export beside the spec, and calls it", () => {
    const { result, onExport } = renderExport(traced(), { kind: "idle" });
    within(result).getByRole("button", { name: "Export to library" }).click();
    expect(onExport).toHaveBeenCalledOnce();
  });

  it("offers no export when the spec cannot be exported", () => {
    const { result } = renderExport(
      {
        ...traced(),
        spec_yaml: null,
        export: {
          ok: false,
          reason: "table_not_verified",
          message: "m",
          tables: ["fuel_compensation"],
        },
      },
      { kind: "idle" },
    );
    expect(within(result).queryByRole("button", { name: "Export to library" })).toBeNull();
  });

  it("names the saved entry, its words and parent, and downloads it through the library", () => {
    const sha = "e".repeat(64);
    const data: RomEditExportResponse = {
      created: true,
      entry: {
        sha256: sha,
        crc32: "00000000",
        size: 1 << 20,
        source: "rom_editor_export",
        label: "one cell",
        added_at: "2026-10-27T00:00:00Z",
        calibration_id: "1ZS44A00000",
        checksum: { ok: true, stored: null, computed: null, reason: null },
        revision_match: { revision: null, status: "unrecognised", reference: null },
        read_meta: null,
      } as RomEditExportResponse["entry"],
      provenance: {
        kind: "rom_editor_export",
        label: "one cell",
        parent_sha256: "b".repeat(64),
        parent: { source: "revision", revision: "ZS44A-k106" },
        spec_sha256: "5".repeat(64),
        spec_yaml: "schema_version: 1\n",
        rom_map: { profile: "nissan-xterra-2008", file: "rom_map.yaml", sha256: "9".repeat(64) },
        calibration_id: "1ZS44A00000",
        checksums: {
          algorithm: "be32_sum_xor",
          parent: { sum: "0xF438FCAA", xor: "0x0C3BBAC0" },
          written: { sum: "0xF438FCA9", xor: "0x0C3BBAC7" },
        },
        cells: [],
        image_sha256: sha,
      },
    };
    const { result, onDownloadImage } = renderExport(traced(), { kind: "ok", data });
    const status = within(result).getByRole("status", { name: "Export" });
    expect(status).toHaveTextContent(/Saved to the ROM library as one cell, image eeeeeeeeeeee/);
    expect(status).toHaveTextContent(/sum 0xF438FCA9, xor 0x0C3BBAC7/);
    expect(status).toHaveTextContent(/calibration 1ZS44A00000/);
    within(status).getByRole("button", { name: "Download image" }).click();
    expect(onDownloadImage).toHaveBeenCalledWith(sha);
    expect(within(result).queryByRole("button", { name: "Export to library" })).toBeNull();
  });

  it("shows a refusal by its reason", () => {
    const { result } = renderExport(traced(), {
      kind: "refused",
      refusal: { reason: "parent_checksum_failed", message: "the parent image's checksum fails" },
    });
    expect(within(result).getByRole("alert")).toHaveTextContent(
      /Export refused: parent_checksum_failed/,
    );
    expect(within(result).getByRole("button", { name: "Export to library" })).toBeVisible();
  });
});
