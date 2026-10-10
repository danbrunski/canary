import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { controlJson } from "../../api/core";
import type { TuningContext } from "../../hooks/useTuningContext";
import type { StatusResponse } from "../../types";
import type {
  Conditions,
  DynoCurve,
  DynoPrerequisite,
  DynoResponse,
  DynoRunOk,
} from "../../types/workbench";
import { AnalysisPage } from "./AnalysisPage";
import { DynoView, dynoEmptySentence } from "./DynoView";

vi.mock("../../api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/core")>();
  return { ...actual, controlJson: vi.fn() };
});
const control = vi.mocked(controlJson);

const CONDITIONS: Conditions = {
  sessions: ["2026-09-26T120000"],
  n_sessions: 1,
  revisions: { "ZS44A-20261004": ["2026-09-26T120000"] },
  mixed_revisions: false,
  revision_boundaries: {},
  baro: { spec: "obd:BAROMETRIC_PRESSURE", min_kpa: 80.1, max_kpa: 80.4, sessions_without: [] },
  gates: null,
};

const PENDING: DynoPrerequisite[] = [
  {
    fact: "inertia",
    status: "estimated",
    detail: "rotating inertia from the stated estimates in dyno.yaml",
    owed_by: "H07.6 two-gear pulls (H07.12)",
    blocks_power: false,
  },
  {
    fact: "road_load",
    status: "pending",
    detail: "no coast-down pair recorded in dyno.yaml; nothing stands in for it",
    owed_by: "H07.5 coast-downs (H07.12)",
    blocks_power: true,
  },
  {
    fact: "squat",
    status: "pending",
    detail: "no flat-ground squat fit recorded",
    owed_by: "H07.4 flat-ground accelerations (H07.12)",
    blocks_power: true,
  },
];

function pending(): DynoResponse {
  return {
    available: false,
    unavailable_kind: "coefficients_pending",
    reason:
      "coefficients pending: road_load (pending, owed by H07.5 coast-downs (H07.12)); squat " +
      "(pending, owed by H07.4 flat-ground accelerations (H07.12)). No power is computed from " +
      "a coefficient nobody measured.",
    request: {
      road: "Hwy 7",
      payload_kg: 127,
      induction: "na",
      ambient_temp_c: null,
      allow_single_direction: false,
    },
    prerequisites: PENDING,
    demand: { spec: "did:ECM_THROTTLE_POS_SENSOR_1", reason: null },
    unavailable: [],
    sessions: [
      {
        session_id: "2026-09-26T120000",
        revision: "ZS44A-20261004",
        revision_source: "registry",
        n_windows: 2,
        n_pulls: 1,
        windows: [
          {
            start: "2026-09-26T12:00:05+00:00",
            end: "2026-09-26T12:00:15+00:00",
            rpm_start: 2450,
            rpm_end: 5500,
            course_deg: 2,
            gear: 3,
            heading: "course-000",
            refusal: null,
          },
          {
            start: "2026-09-26T12:01:05+00:00",
            end: "2026-09-26T12:01:09+00:00",
            rpm_start: 2000,
            rpm_end: 3000,
            course_deg: 90,
            gear: null,
            heading: null,
            refusal: {
              available: false,
              estimator: "find_power_pulls",
              kind: "lift_or_shift",
              reason: "the rpm slope collapsed mid-pull",
            },
          },
        ],
      },
    ],
    runs: [
      {
        available: false,
        session_id: "2026-09-26T120000",
        gear: 3,
        estimator: "dyno_workbench",
        kind: "coefficients_pending",
        reason: "coefficients pending: road_load, squat",
        n_pulls: 1,
        headings: ["course-000"],
      },
    ],
    conditions: CONDITIONS,
  };
}

function curve(scale: number): DynoCurve {
  const rpm = [2500, 3000, 3500, 4000];
  return {
    rpm,
    wheel_force_n: rpm.map(() => 4000 * scale),
    wheel_power_kw: [60, 75, 88, 95].map((v) => v * scale),
    wheel_torque_nm: [229, 239, 240, 227].map((v) => v * scale),
    engine_power_kw_estimate: [71, 88, 104, 112].map((v) => v * scale),
    engine_torque_nm_estimate: [269, 281, 282, 267].map((v) => v * scale),
  };
}

function computed(): DynoResponse {
  const run: DynoRunOk = {
    available: true,
    session_id: "2026-09-26T120000",
    gear: 3,
    direction: "pair",
    n_pulls: 2,
    curve: curve(1),
    per_heading: { "course-000": curve(0.98), "course-180": curve(1.02) },
    conditions: {
      gear: 3,
      direction: "pair",
      tune_revision: "ZS44A-20261004",
      induction: "na",
      baro_kpa: 80.2,
      intake_temp_c: 31,
      ambient_temp_c: 22,
      road: "Hwy 7",
      date: "2026-09-26",
      payload_kg: 127,
      sessions: ["2026-09-26T120000"],
    },
    flags: {
      inertia: "estimated",
      squat_corrected: true,
      imu_pitch_offset_deg: 0.31,
      imu_pitch_offset_source: "estimated from this run's own pulls",
      imu_reference_id: "frame-1",
      correction: "none",
      engine_estimate: "engine = wheel / eta (estimate)",
      wheel_torque_convention: "chassis-dyno",
    },
    test_mass_kg: 2311,
    effective_mass_kg: 2495,
    driveline_efficiency: 0.85,
    uncertainty: {
      noise_floor: {
        wheel_power_kw: {
          available: true,
          unit: "kW",
          n_pull_pairs: 3,
          conditions: {},
          bands: [{ rpm_min: 2500, rpm_max: 4000, sd: 1.2, n_points: 4 }],
        },
        wheel_torque_nm: {
          available: false,
          estimator: "noise_floor",
          kind: "no_floor",
          reason: "no dyno noise floor has been recorded for this vehicle",
        },
      },
      heading_spread: {
        meaning: "half the difference between the two headings; not a noise floor",
        wheel_power_kw: [1.2, 1.5, 1.8, 1.9],
        wheel_torque_nm: [4.6, 4.8, 4.8, 4.5],
      },
    },
  };
  return {
    ...pending(),
    available: true,
    unavailable_kind: null,
    reason: null,
    prerequisites: PENDING.map((row) => ({
      ...row,
      status: row.fact === "inertia" ? "estimated" : "recorded",
      owed_by: row.fact === "inertia" ? row.owed_by : null,
      blocks_power: false,
    })),
    runs: [run],
  };
}

function respond(body: DynoResponse) {
  control.mockImplementation((path: string) => {
    if (path.startsWith("/control/logs")) {
      return Promise.resolve({
        include_sessions: false,
        total: 0,
        limit: 100,
        capped: false,
        logs: [],
      });
    }
    if (path.startsWith("/analysis/dyno")) return Promise.resolve(body);
    if (path.startsWith("/control/profiles")) {
      return Promise.resolve({
        profiles: [
          {
            slug: "nissan-xterra-2008",
            vin: "",
            name: "",
            make: "Nissan",
            model: "Xterra",
            year: 2008,
            // Edited in Profiles since the daemon started; the dyno still
            // runs on ``in_use`` until a restart.
            parameters: { mass_kg: 2230 },
            calibration_id: "",
            cvn: "",
          },
        ],
        active_slug: "nissan-xterra-2008",
        in_use: {
          make: "Nissan",
          model: "Xterra",
          year: 2008,
          vin: "",
          parameters: { mass_kg: 1980 },
        },
      });
    }
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}

const CHOICE = { sessions: ["2026-09-26T120000"], revision: null };

afterEach(() => vi.clearAllMocks());

describe("DynoView", () => {
  it("shows the saved vehicle weight the payload is added to, and the test mass", async () => {
    respond(pending());
    render(<DynoView token="t" choice={CHOICE} />);
    expect(
      await screen.findByText(/Saved vehicle weight 1,980 kg \(Profiles\)/),
    ).toBeInTheDocument();
    expect(screen.getByText(/added to the vehicle/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Payload (kg)"), { target: { value: "127" } });
    expect(screen.getByText(/Test mass: 2,107 kg/)).toBeInTheDocument();
    expect(
      screen.getByText(/Profiles now holds 2,230 kg; the dyno uses it after a daemon restart/),
    ).toBeInTheDocument();
  });

  it("sends the stated run conditions and nothing it was not told", async () => {
    respond(pending());
    render(<DynoView token="t" choice={CHOICE} />);
    fireEvent.change(screen.getByLabelText("Road"), { target: { value: "Hwy 7" } });
    fireEvent.change(screen.getByLabelText("Payload (kg)"), { target: { value: "127" } });
    fireEvent.change(screen.getByLabelText("Induction"), { target: { value: "na" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByRole("status");
    expect(control).toHaveBeenCalledWith(
      "/analysis/dyno?sessions=2026-09-26T120000&road=Hwy+7&payload_kg=127&induction=na",
      "t",
      expect.anything(),
    );
  });

  it("names pending coefficients as the cause and draws no curve from them", async () => {
    respond(pending());
    const { container } = render(<DynoView token="t" choice={CHOICE} />);
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    const status = await screen.findByRole("status");
    expect(status).toHaveAttribute("data-empty", "coefficients_pending");
    expect(status).toHaveTextContent("No power computed — coefficients pending.");
    expect(status).toHaveTextContent("road_load");
    const facts = screen.getByLabelText("Prerequisites");
    expect(within(facts).getByText("road load: pending")).toBeInTheDocument();
    expect(facts).toHaveTextContent("owed by H07.5 coast-downs (H07.12)");
    expect(facts).toHaveTextContent("(blocks power)");
    expect(container.querySelector("svg[aria-label^='Dyno']")).toBeNull();
    expect(screen.getByLabelText("Refused runs")).toHaveTextContent("coefficients pending");
    // Coverage is shown even without a curve: every window, pull or refusal.
    expect(screen.getByText(/2 rpm rise\(s\) found, 1 usable pull/)).toBeInTheDocument();
    expect(screen.getByText(/lift or shift: the rpm slope collapsed mid-pull/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeDisabled();
  });

  it("overlays the run and its headings with the floor band, conditions and flags", async () => {
    respond(computed());
    const { container } = render(<DynoView token="t" choice={CHOICE} />);
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByLabelText("Dyno power against rpm")).toBeInTheDocument();
    expect(container.querySelector('[data-curve="pair"]')).not.toBeNull();
    expect(container.querySelectorAll("[data-heading]")).toHaveLength(2);
    expect(container.querySelector('[data-floor="band"]')).not.toBeNull();
    const table = screen.getByLabelText("Dyno runs");
    expect(table).toHaveTextContent("95.0 kW @ 4000");
    expect(table).toHaveTextContent("ZS44A-20261004 · na · Hwy 7");
    expect(table).toHaveTextContent("inertia estimated");
    expect(table).toHaveTextContent("pitch offset 0.31°");
    expect(table).toHaveTextContent("same-day floor from 3 pull pairs");
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeEnabled();

    // Torque has no floor recorded: it says so and draws no band.
    fireEvent.click(screen.getByRole("button", { name: "Wheel torque (N m)" }));
    expect(await screen.findByLabelText("Dyno torque against rpm")).toBeInTheDocument();
    expect(container.querySelector('[data-floor="band"]')).toBeNull();
    expect(screen.getByLabelText("Dyno runs")).toHaveTextContent(
      "no floor: no dyno noise floor has been recorded for this vehicle",
    );
  });

  it("gives every kind of empty its own sentence", () => {
    expect(dynoEmptySentence("no_pull_found", null)).toMatch(/no sustained rpm rise/);
    expect(dynoEmptySentence("every_pull_refused", null)).toMatch(/each window below says why/);
    expect(dynoEmptySentence("not_configured", "no dyno.yaml")).toMatch(/no dyno\.yaml/);
    expect(dynoEmptySentence("over_budget", null)).toMatch(/laptop/);
    expect(dynoEmptySentence("every_run_refused", null)).toMatch(/each run below says why/);
    expect(dynoEmptySentence("prerequisites_missing", "wot_criterion (missing)")).toMatch(
      /profile is missing what it rests on\. wot_criterion/,
    );
    // A missing configuration never reads as coefficients the truck owes.
    expect(dynoEmptySentence("prerequisites_missing", null)).not.toMatch(/pending/);
  });
});

describe("AnalysisPage dyno tab", () => {
  it("is one of the Analysis views", () => {
    respond(pending());
    const context: TuningContext = {
      selectedLogId: null,
      setSelectedLogId: () => {},
      loadedRom: null,
      setLoadedRom: () => {},
    };
    render(
      <AnalysisPage
        token="t"
        status={{ registry_revision: "ZS44A-20261004" } as StatusResponse}
        context={context}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Dyno" }));
    expect(screen.getByText("Driving dyno")).toBeInTheDocument();
  });
});
