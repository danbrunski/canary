import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  ApplicationModelReport,
  ApplicationNode,
  ApplicationOutput,
  TwinRole,
} from "../types/ecuApplication";

vi.mock("../api/ecuApplication", () => ({ getApplicationModel: vi.fn() }));

const api = await import("../api/ecuApplication");
const { EcuApplicationModelPanel, outputStatusText, roleCounts } =
  await import("./EcuApplicationModelPanel");

function node(id: string, twin: TwinRole): ApplicationNode {
  return {
    id,
    function: "0x03907A",
    kind: "computation",
    twin,
    confidence: "code",
    implemented_by: null,
    observed_as: [],
    assumption: null,
  };
}

// Trimmed from the real `coverage_report` of
// data/nissan-xterra-2008/ecu_application_model.yaml.
const output: ApplicationOutput = {
  id: "target_equiv_ratio",
  ram: "0xFFFF26BE",
  units: "q14_phi",
  role: "final",
  status: "conditional",
  observed_as: [
    { spec: "mem:ECM_TARGET_EQUIV_RATIO", relation: "identity", confidence: "observed" },
  ],
  twin: { definition: "target_phi_twin.yaml", predicts: "enrichment_gate" },
  path: [node("final_command_word", "not_modelled"), node("target_afr_lookup", "implemented")],
  by_role: {
    not_modelled: ["cycle_snapshot", "final_command_word"],
    observed: ["engine_speed_word"],
    assumed: ["target_afr_ram_copy"],
    implemented: ["target_afr_lookup", "enrichment_gate"],
    calibration_dead: ["command_floor"],
  },
  blockers: [
    {
      name: "ram_copy_unverified",
      nodes: ["target_afr_ram_copy"],
      closes_with: "E02.2: read 0xFFFFB260 and compare with the resolved slot.",
    },
  ],
  equals_output_when: [],
  regions: [
    {
      id: "enrichment_gate_band",
      status: "unevaluable",
      when: "gated lookup result 129 with the latch not captured",
      nodes: ["enrichment_gate"],
    },
  ],
};

const report: ApplicationModelReport = {
  model: { name: "ecm_application_model", schema_version: 1, model_version: 4, sha256: "ace8" },
  program: {
    id: "nissan-1ZS44A-uprev-osiris",
    calibration_id: "1ZS44A00000",
    identity_sha256: "9f35",
    patch: "uprev_map_switching",
    images: [],
  },
  image: { sha256: "a46b", known: true, revision: "ZS44A-20260920", role: "decoded" },
  schedule: { function: "0x0575EC", cycle_start: "0x020EF2", order: [] },
  outputs: [output],
  uncovered: [
    {
      id: "pedal_flag_inputs",
      where: "0xFFFF1A64; its writer is not traced",
      blocks: "predicting the pedal flag from sensors",
      next_evidence: "Trace the writer of 0xFFFF1A64.",
    },
  ],
};

afterEach(() => vi.clearAllMocks());

describe("application model helpers", () => {
  it("reads the path in role order and omits empty roles", () => {
    const rows = roleCounts({ ...output, by_role: { ...output.by_role, assumed: [] } });
    expect(rows.map((r) => r.role)).toEqual([
      "implemented",
      "observed",
      "not_modelled",
      "calibration_dead",
    ]);
  });

  it("names what keeps an output from qualifying", () => {
    expect(outputStatusText(output)).toBe("conditional — ram_copy_unverified");
    expect(outputStatusText({ ...output, status: "qualified", blockers: [] })).toBe("qualified");
  });
});

describe("EcuApplicationModelPanel", () => {
  it("asks for the token before fetching", () => {
    render(<EcuApplicationModelPanel token="" />);
    expect(api.getApplicationModel).not.toHaveBeenCalled();
    expect(screen.getByText(/needs the control token/)).toBeTruthy();
  });

  it("reports qualified outputs, blockers, regions and uncovered logic", async () => {
    vi.mocked(api.getApplicationModel).mockResolvedValue(report);
    render(<EcuApplicationModelPanel token="t" />);
    expect(await screen.findByText("target_equiv_ratio")).toBeTruthy();
    expect(screen.getByText("conditional — ram_copy_unverified")).toBeTruthy();
    expect(screen.getByText("implemented 2")).toBeTruthy();
    expect(screen.getByText("not modelled 2")).toBeTruthy();
    expect(screen.getByText(/ram_copy_unverified \(1 nodes\): E02.2/)).toBeTruthy();
    expect(screen.getByText("unevaluable")).toBeTruthy();
    expect(screen.getByText("Uncovered logic (1)")).toBeTruthy();
    expect(screen.getByText("pedal_flag_inputs")).toBeTruthy();
    expect(screen.getByText("ZS44A-20260920 (decoded)")).toBeTruthy();
  });

  it("lists firmware overrides with how far each was checked", async () => {
    vi.mocked(api.getApplicationModel).mockResolvedValue({
      ...report,
      overrides: [
        {
          id: "fuel_cut_zero_command",
          regime: "fuel_cut",
          acts_on: ["target_equiv_ratio"],
          acts_on_ram: [],
          nodes: ["fuel_cut_gate"],
          routine: "0x03D06E",
          span: null,
          condition: "0xFFFF26B8 is 0x3F",
          effect: "the command word is zero",
          established_by: "listing",
          confidence: "code",
          qualification: "fixture",
          qualified_by: ["tests/test_ecu_overrides.py"],
          evidence: ["2026-10-04 listing pass"],
          consistent_with: [],
          gaps: ["the writer of 0xFFFF26B8"],
          verify_with: "a held-out drive with fuel cuts",
        },
      ],
    });
    render(<EcuApplicationModelPanel token="t" />);
    expect(await screen.findByText("Firmware overrides (1)")).toBeTruthy();
    expect(screen.getByText("fuel_cut_zero_command")).toBeTruthy();
    expect(screen.getByText("fixture")).toBeTruthy();
    expect(screen.getByText(/when 0xFFFF26B8 is 0x3F: the command word is zero/)).toBeTruthy();
    expect(screen.getByText(/1 gap; verify: a held-out drive/)).toBeTruthy();
  });

  it("omits the overrides section for a daemon that reports none", async () => {
    vi.mocked(api.getApplicationModel).mockResolvedValue(report);
    render(<EcuApplicationModelPanel token="t" />);
    expect(await screen.findByText("target_equiv_ratio")).toBeTruthy();
    expect(screen.queryByText(/Firmware overrides/)).toBeNull();
  });

  it("shows the server's refusal when the vehicle has no model", async () => {
    vi.mocked(api.getApplicationModel).mockRejectedValue(
      new Error("No ECU application model for this vehicle"),
    );
    render(<EcuApplicationModelPanel token="t" />);
    expect(await screen.findByText(/No ECU application model for this vehicle/)).toBeTruthy();
  });
});
