import { describe, expect, it } from "vitest";

import type { CompactStatusResponse, StatusResponse } from "../types";
import { compactChangeKey, overlayCompact } from "./statusView";

const FULL = {
  storage_path: "/data/canary",
  free_mb: 800,
  latest_session: "2026-11-12T120000",
  vehicle: "2008 Nissan Xterra",
  model_versions: { isolation_forest: "if-1" },
  emitter_counters: { emitted: 1, suppressed_dedupe: 0 },
  gps: { has_fix: true },
  replay: { running: false },
  registry_revision: "ZS44A-20261111-2",
  vehicle_state: { state: "ignition_only", rpm: 0, replay_id: null },
  passive: { enabled: false, ecm_session_owner: "can_diagnostics" },
  capture_profile: {
    active: true,
    name: "tuning",
    started_at: "2026-11-12T12:00:00Z",
    rows_written: 1200,
    channels: ["RPM"],
  },
  health: {
    observed: 10,
    inference_latency_ms: { mean: 12, p95: 20, max: 31 },
    system: { cpu_percent: 0.1, mem_total_mb: 8000 },
    power: { raw: 0 },
    telemetry_sinks: {
      parquet: { queue_depth: 1, queue_high_water: 4, dropped_records: 0, extra: 7 },
    },
    obd_scheduler: {
      requests_sent: 100,
      requests: [{ channel: "RPM", configured_rate_hz: 2 }],
      achieved_hz_60s: { RPM: 1.9 },
    },
  },
  detectors: {
    isolation_forest: { blind_modes: ["idle"] },
    inference: { ticks_scheduled: 10, missed_ticks: 0, stage_ms: { score: 5 } },
  },
} as unknown as StatusResponse;

function compact(overrides: Record<string, unknown> = {}): CompactStatusResponse {
  return {
    view: "compact",
    served_at: "2026-11-12T12:00:05Z",
    free_mb: 799.5,
    emitter_counters: { emitted: 3, suppressed_dedupe: 1 },
    vehicle_state: { state: "engine_running", rpm: 850, replay_id: null },
    passive: { enabled: false, ecm_session_owner: "can_diagnostics" },
    capture_profile: { name: "tuning", active: true },
    health: {
      system: { cpu_percent: 0.4, mem_total_mb: 8000 },
      telemetry_sinks: { parquet: { queue_depth: 9, queue_high_water: 9, dropped_records: 2 } },
      can0: { rx_dropped: 0 },
      obd_scheduler: { requests_sent: 130, achieved_hz_60s_total: 12.5 },
    },
    detectors: { inference: { ticks_scheduled: 14, missed_ticks: 1 } },
    ...overrides,
  } as unknown as CompactStatusResponse;
}

/** `FULL` with top-level fields replaced (not merged). */
function withFull(overrides: Record<string, unknown>): StatusResponse {
  return { ...FULL, ...overrides };
}

/** A value read by path, for fields the dashboard's types do not name. */
function at(value: unknown, ...path: string[]): unknown {
  return path.reduce<unknown>(
    (node, key) => (node as Record<string, unknown> | undefined)?.[key],
    value,
  );
}

describe("overlayCompact", () => {
  it("takes the fields the compact view carries from the compact view", () => {
    const merged = overlayCompact(FULL, compact());
    expect(merged.free_mb).toBe(799.5);
    expect(merged.vehicle_state?.state).toBe("engine_running");
    expect(merged.emitter_counters?.emitted).toBe(3);
    expect(merged.health?.system.cpu_percent).toBe(0.4);
    expect(at(merged, "health", "obd_scheduler", "requests_sent")).toBe(130);
  });

  it("keeps every full-only field, at the path the panels read it", () => {
    const merged = overlayCompact(FULL, compact());
    expect(merged.latest_session).toBe("2026-11-12T120000");
    expect(merged.storage_path).toBe("/data/canary");
    expect(merged.vehicle).toBe("2008 Nissan Xterra");
    expect(merged.model_versions).toEqual({ isolation_forest: "if-1" });
    expect(merged.gps).toEqual({ has_fix: true });
    expect(merged.replay).toEqual({ running: false });
    expect(merged.registry_revision).toBe("ZS44A-20261111-2");
  });

  it("merges blocks the compact view carries a subset of", () => {
    const merged = overlayCompact(FULL, compact());
    // capture_profile: name and active refreshed, the rest the full payload's.
    expect(merged.capture_profile).toMatchObject({
      name: "tuning",
      active: true,
      started_at: "2026-11-12T12:00:00Z",
      rows_written: 1200,
      channels: ["RPM"],
    });
    // health: counters refreshed, the request table and latency kept.
    expect(merged.health?.inference_latency_ms.mean).toBe(12);
    expect(merged.health?.system).toEqual({ cpu_percent: 0.4, mem_total_mb: 8000 });
    expect(merged.health?.obd_scheduler?.requests).toHaveLength(1);
    expect(merged.health?.obd_scheduler?.achieved_hz_60s).toEqual({ RPM: 1.9 });
    expect(
      (merged.health as unknown as Record<string, Record<string, Record<string, number>>>)
        .telemetry_sinks.parquet,
    ).toEqual({ queue_depth: 9, queue_high_water: 9, dropped_records: 2, extra: 7 });
    // detectors: only `inference` is in the compact view.
    expect(merged.detectors?.isolation_forest.blind_modes).toEqual(["idle"]);
    expect(at(merged, "detectors", "inference")).toMatchObject({
      ticks_scheduled: 14,
      stage_ms: { score: 5 },
    });
  });

  it("never lets the compact view's null erase a block the full payload has", () => {
    const merged = overlayCompact(
      FULL,
      compact({ health: null, detectors: null, emitter_counters: null }),
    );
    expect(merged.health?.inference_latency_ms.mean).toBe(12);
    expect(merged.detectors?.isolation_forest.blind_modes).toEqual(["idle"]);
    expect(merged.emitter_counters?.emitted).toBe(1);
  });

  it("does not invent a field the full payload lacks, from a null", () => {
    const bare = {
      ...FULL,
      health: { ...FULL.health, obd_scheduler: undefined },
    } as StatusResponse;
    const merged = overlayCompact(bare, compact({ health: { obd_scheduler: null } }));
    expect(merged.health?.obd_scheduler).toBeUndefined();
  });

  it("lets a null replace a scalar: a reading that went null is no longer the old value", () => {
    const merged = overlayCompact(
      withFull({ vehicle_state: { state: "ignition_only", rpm: 850, coolant_temp: 88 } }),
      compact({
        vehicle_state: { state: "ignition_only", rpm: null, coolant_temp: null, replay_id: null },
        health: { system: { cpu_percent: null, mem_total_mb: 8000 } },
      }),
    );
    expect(merged.vehicle_state?.rpm).toBeNull();
    expect(merged.vehicle_state?.coolant_temp).toBeNull();
    expect(merged.health?.system.cpu_percent).toBeNull();
    expect(merged.health?.system.mem_total_mb).toBe(8000);
  });

  it("replaces the blocks compact carries whole, so a key the daemon dropped does not linger", () => {
    const full = withFull({
      vehicle_state: { state: "engine_running", rpm: 850, coolant_temp: 88, replay_id: null },
      passive: {
        enabled: true,
        ecm_session_owner: "foreign",
        ecm_session_claims: { foreign: 5 },
        foreign_tester: { tester_address: "7DF" },
      },
      emitter_counters: { emitted: 1, suppressed_dedupe: 0, suppressed_cooldown: 4 },
      health: {
        ...FULL.health,
        system: { cpu_percent: 0.1, mem_total_mb: 8000, temperature_c: 61 },
        can0: { rx_dropped: 3, rx_errors: 1 },
        obd_scheduler: {
          requests_sent: 100,
          requests: [{ channel: "RPM" }],
          profile_start_withheld: { no_response: 2 },
          receive: { frames_seen: 40, frames_dropped: 1 },
        },
      },
      detectors: {
        isolation_forest: { blind_modes: ["idle"] },
        inference: {
          ticks_scheduled: 10,
          missed_by_reason: { compute_busy: 3 },
          stage_ms: { a: 1 },
        },
      },
    });
    const merged = overlayCompact(
      full,
      compact({
        vehicle_state: { state: "engine_running", rpm: 850, replay_id: null },
        passive: { enabled: false, ecm_session_owner: "can_diagnostics", ecm_session_claims: {} },
        emitter_counters: { emitted: 3, suppressed_dedupe: 1 },
        health: {
          system: { cpu_percent: 0.4, mem_total_mb: 8000 },
          telemetry_sinks: {},
          can0: { rx_dropped: 0 },
          obd_scheduler: {
            requests_sent: 130,
            profile_start_withheld: {},
            receive: { frames_seen: 55 },
          },
        },
        detectors: { inference: { ticks_scheduled: 14, missed_by_reason: {} } },
      }),
    );
    // Whole blocks: nothing of the old value is left in them.
    expect(at(merged, "passive", "ecm_session_claims")).toEqual({});
    expect(at(merged, "passive", "foreign_tester")).toBeUndefined();
    expect(merged.vehicle_state).toEqual({ state: "engine_running", rpm: 850, replay_id: null });
    expect(merged.emitter_counters).toEqual({ emitted: 3, suppressed_dedupe: 1 });
    expect(merged.health?.system).toEqual({ cpu_percent: 0.4, mem_total_mb: 8000 });
    expect(merged.health?.can0).toEqual({ rx_dropped: 0 });
    // Maps inside a narrowed block are whole too.
    expect(at(merged, "health", "obd_scheduler", "profile_start_withheld")).toEqual({});
    expect(at(merged, "health", "obd_scheduler", "receive")).toEqual({ frames_seen: 55 });
    expect(at(merged, "detectors", "inference", "missed_by_reason")).toEqual({});
    // The narrowed blocks still keep what compact does not carry.
    expect(merged.health?.obd_scheduler?.requests).toHaveLength(1);
    expect(at(merged, "detectors", "inference", "stage_ms")).toEqual({ a: 1 });
    expect(merged.health?.power).toEqual({ raw: 0 });
    expect(merged.health?.inference_latency_ms.mean).toBe(12);
  });

  it("drops a telemetry sink compact no longer lists, and keeps a listed sink's other fields", () => {
    const full = withFull({
      health: {
        ...FULL.health,
        telemetry_sinks: {
          parquet: { queue_depth: 1, queue_high_water: 4, dropped_records: 0, extra: 7 },
          remote: { queue_depth: 0, queue_high_water: 0, dropped_records: 0, extra: 1 },
        },
      },
    });
    const merged = overlayCompact(
      full,
      compact({
        health: {
          system: { cpu_percent: 0.4, mem_total_mb: 8000 },
          telemetry_sinks: { parquet: { queue_depth: 9, queue_high_water: 9, dropped_records: 2 } },
        },
      }),
    );
    expect(at(merged, "health", "telemetry_sinks")).toEqual({
      parquet: { queue_depth: 9, queue_high_water: 9, dropped_records: 2, extra: 7 },
    });
  });

  it("takes a disarmed passive switch's nulls, not the armed state's source and ttl", () => {
    const armed = withFull({
      passive: { enabled: true, source: "operator", ttl_remaining_seconds: 120 },
    });
    const merged = overlayCompact(
      armed,
      compact({ passive: { enabled: false, source: null, ttl_remaining_seconds: null } }),
    );
    expect(merged.passive).toMatchObject({
      enabled: false,
      source: null,
      ttl_remaining_seconds: null,
    });
  });

  it("drops the old profile's plan when the capture profile switches", () => {
    const running = withFull({
      capture_profile: {
        active: true,
        name: "tuning",
        channels: ["RPM"],
        session_dir: "/data/_capture-tuning",
        target_rate_hz: 10,
        started_at: "2026-11-12T12:00:00Z",
        rows_written: 1200,
      },
    });
    const merged = overlayCompact(
      running,
      compact({ capture_profile: { name: "fuel", active: true } }),
    );
    expect(merged.capture_profile).toEqual({ name: "fuel", active: true });
  });

  it("drops the ended capture's start and row count when it stops", () => {
    const merged = overlayCompact(
      FULL,
      compact({ capture_profile: { name: "default", active: false } }),
    );
    expect(merged.capture_profile).toEqual({ name: "default", active: false });
  });

  it("keeps the plan while the profile and the active flag are the full payload's", () => {
    const merged = overlayCompact(FULL, compact());
    expect(merged.capture_profile).toMatchObject({ channels: ["RPM"], rows_written: 1200 });
  });

  it("does not mutate either input", () => {
    const full = structuredClone(FULL);
    const small = compact();
    const smallBefore = structuredClone(small);
    overlayCompact(full, small);
    expect(full).toEqual(FULL);
    expect(small).toEqual(smallBefore);
  });
});

describe("compactChangeKey", () => {
  it("is stable while the daemon does the same thing", () => {
    expect(compactChangeKey(compact())).toBe(compactChangeKey(compact({ free_mb: 1 })));
  });

  it.each([
    ["the vehicle state", { vehicle_state: { state: "engine_off", replay_id: null } }],
    ["a replay starting", { vehicle_state: { state: "replay", replay_id: "r1" } }],
    ["a capture stopping", { capture_profile: { name: "tuning", active: false } }],
    ["another capture profile", { capture_profile: { name: "fuel", active: true } }],
    ["the passive switch", { passive: { enabled: true, ecm_session_owner: "can_diagnostics" } }],
    ["the ECM session owner", { passive: { enabled: false, ecm_session_owner: "foreign" } }],
  ])("moves with %s", (_label, change) => {
    expect(compactChangeKey(compact(change))).not.toBe(compactChangeKey(compact()));
  });
});
