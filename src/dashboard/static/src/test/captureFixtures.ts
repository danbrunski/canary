// Shared fixtures for the capture runner and profile editor tests (R08.6).
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { vi } from "vitest";

import type {
  CaptureProfile,
  ObdSchedulerRequestStatus,
  ResolvedPlanMember,
  StatusResponse,
} from "../types";

type Mock = ReturnType<typeof vi.fn>;
export type CaptureMocks = Record<
  | "pauseProfileCapture"
  | "getCaptureProfiles"
  | "getObdCoverage"
  | "getDids"
  | "startProfileCapture"
  | "stopProfileCapture"
  | "saveCaptureProfile"
  | "deleteCaptureProfile",
  Mock
>;

export const CAPTURE_API = [
  "pauseProfileCapture",
  "getCaptureProfiles",
  "getObdCoverage",
  "getDids",
  "startProfileCapture",
  "stopProfileCapture",
  "saveCaptureProfile",
  "deleteCaptureProfile",
] as const;

export const DEFAULT_PROFILE: CaptureProfile = {
  name: "default",
  description: "Normal telemetry and anomaly-detection collection",
  experimental: false,
  mode01: { AUTO: 1 },
  mode22: [],
  mode22_rate_weights: {},
  mode01_enabled: true,
  mode22_enabled: true,
  did_interval_seconds: null,
  round_timestamps: true,
  target_rate_hz: null,
  did_continuous: false,
  bus_budget_override: null,
  expected_total_demand_hz: null,
  capacity_evidence: "",
  required_tuple: [],
  min_achieved_rate_hz: null,
  requires_tune_revision: false,
  corpus_eligible: true,
  behavior_model_recipes: [],
  storage_prefix: "",
};

export const FUEL_MAP: CaptureProfile = {
  name: "fuel-map",
  description: "High-rate fueling and timing capture",
  experimental: false,
  mode01: { RPM: 1, ENGINE_LOAD: 1 },
  mode22: [],
  mode22_rate_weights: {},
  mode01_enabled: true,
  mode22_enabled: true,
  did_interval_seconds: null,
  round_timestamps: true,
  target_rate_hz: null,
  did_continuous: false,
  bus_budget_override: null,
  expected_total_demand_hz: null,
  capacity_evidence: "",
  required_tuple: [],
  min_achieved_rate_hz: null,
  requires_tune_revision: false,
  corpus_eligible: false,
  behavior_model_recipes: [],
  storage_prefix: "_capture-fuel-map",
};

export const HYBRID: CaptureProfile = {
  name: "tuning-hybrid-research",
  description: "Experimental fast-exit capture",
  experimental: true,
  mode01: {
    RPM: 1,
    COMMANDED_EQUIV_RATIO: 1,
    TIMING_ADVANCE: 1,
  },
  mode22: ["ECM_AF_SENSOR_V_B1", "ECM_BASE_FUEL_SCHEDULE_CANDIDATE"],
  mode22_rate_weights: { ECM_BASE_FUEL_SCHEDULE_CANDIDATE: 0.25 },
  mode01_enabled: true,
  mode22_enabled: true,
  did_interval_seconds: 0.25,
  round_timestamps: true,
  target_rate_hz: 4,
  did_continuous: false,
  bus_budget_override: 90,
  expected_total_demand_hz: 20,
  capacity_evidence: "10 81 restored Mode 01 on the next request.",
  required_tuple: [],
  min_achieved_rate_hz: null,
  requires_tune_revision: false,
  corpus_eligible: false,
  behavior_model_recipes: [
    "air_charge_vs_rpm_throttle_did",
    "cam_primary_vs_command_rpm_did",
    "cam_secondary_vs_command_rpm_did",
  ],
  storage_prefix: "_capture-tuning-hybrid-research",
};

export function status(activeName: string | null): StatusResponse {
  return {
    capture_profile: activeName
      ? {
          active: true,
          name: activeName,
          description: "High-rate fueling and timing capture",
          experimental: false,
          started_at: "2026-07-31T10:00:00+00:00",
          session_dir: `_capture-${activeName}-2026-07-31T100000`,
          source: "control",
          channels: [],
          mode01: { RPM: 1, ENGINE_LOAD: 1 },
          mode22: [],
          mode22_rate_weights: {},
          rows_written: 12345,
          mode01_enabled: true,
          mode22_enabled: true,
          did_interval_seconds: null,
          round_timestamps: true,
          target_rate_hz: null,
          did_continuous: false,
          expected_total_demand_hz: null,
          capacity_evidence: "",
          required_tuple: [],
          min_achieved_rate_hz: null,
          requires_tune_revision: false,
          paused: false,
          corpus_eligible: false,
          behavior_model_recipes: [],
          bus_budget_override: null,
        }
      : null,
  } as unknown as StatusResponse;
}

export function liveChannels() {
  return new Map([
    ["obd:RPM", { latest: { value: 812.5, unit: "rpm" }, history: [] }],
    ["obd:ENGINE_LOAD", { latest: { value: 19.6, unit: "%" }, history: [] }],
  ]) as never;
}

/** The catalog and mutation replies every capture test starts from. */
export function installCaptureMocks(mocks: CaptureMocks): void {
  mocks.getCaptureProfiles.mockResolvedValue({
    schema_version: 1,
    profiles: [DEFAULT_PROFILE, FUEL_MAP, HYBRID],
    resolved_plans: {
      "fuel-map": {
        member_count: 17,
        aliases_deduplicated: 0,
        channel_reads_per_second: 44,
        decoded_rows_per_second: 44,
        complete_tuples_per_second: 4,
        diagnostic_requests_per_second: {
          best_case_batched: 9,
          single_request_fallback: 44,
          session_control: 0,
          discovery: 0,
        },
        raw_frames_per_second: {
          best_case_batched: 27,
          single_request_fallback: 88,
          estimate_complete: true,
          unknown_response_width_channels: [],
        },
        priority_requests_per_second_fallback: 8,
        bus_budget_requests_per_second: 20,
        priority_feasible: true,
        best_case_feasible: true,
        fallback_feasible: false,
        priority_source: "collection",
      },
    },
  });
  mocks.getObdCoverage.mockResolvedValue({
    rows: [
      { channel: "RPM", status: "decoded" },
      { channel: "ENGINE_LOAD", status: "decoded" },
      { channel: "NOT_SUPPORTED", status: "unsupported" },
    ],
  });
  mocks.getDids.mockResolvedValue({
    dids: [{ name: "ECM_AF_SENSOR_V_B1" }, { name: "ECM_BASE_FUEL_SCHEDULE_CANDIDATE" }],
    memory: [{ name: "ECM_TARGET_EQUIV_RATIO" }, { name: "ECM_ENGINE_PERIOD" }],
  });
  // The daemon returns the new snapshot from every capture mutation and the
  // panel applies it immediately — /status only polls every 5 s, which is what
  // made start/stop feel unresponsive. Mocks must return it or the tests
  // silently exercise the error path instead.
  mocks.startProfileCapture.mockResolvedValue({
    capture_profile: status("fuel-map").capture_profile,
  });
  mocks.pauseProfileCapture.mockResolvedValue({
    capture_profile: { ...status("fuel-map").capture_profile!, paused: true },
  });
  mocks.stopProfileCapture.mockResolvedValue({
    capture_profile: { ...status(null), active: false, name: "default", paused: false },
    capture_quality: null,
  });
  mocks.saveCaptureProfile.mockImplementation(async (_token, profile) => ({
    profile,
    warnings: [],
  }));
}

/** A slice of `/status.health.obd_scheduler` for one windowed DID, built by
 *  the backend's own rate policy: `tests/test_dashboard_scheduler_fixture.py`
 *  regenerates it and fails when the policy moves. `window_period_seconds` is
 *  the period that governs (cycle before interval), for assertions only. */
export interface WindowedDidScenario {
  requests: ObdSchedulerRequestStatus[];
  /** As the daemon reports it: the interval and the cycle separately. */
  did_profile: {
    window_interval_seconds: number;
    cycle: { timing_policy: "cycle" | "interval"; cycle_seconds: number | null };
  };
  resolved_plan: { members: ResolvedPlanMember[] };
  window_period_seconds: number;
}

// Read from disk (vitest's root is the dashboard directory).
export const WINDOWED_DID_SCHEDULER = JSON.parse(
  readFileSync(resolve(process.cwd(), "src/test/fixtures/windowedDidScheduler.json"), "utf8"),
) as Record<
  "config-cycle-beats-profile-interval" | "profile-cycle-every-window",
  WindowedDidScenario
>;
