import type { StatusHealth } from "../types";

/** `/status.health.system` as a host reports it when nothing is known: every
 *  member present, as the daemon always sends them, each `null`. A test that
 *  cares about a reading overrides it. */
export function hostSystem(
  overrides: Partial<StatusHealth["system"]> = {},
): StatusHealth["system"] {
  return {
    load_1m: null,
    cpu_count: null,
    load_per_cpu: null,
    mem_used_fraction: null,
    mem_total_mb: null,
    cpu_temp_c: null,
    memory_limits_enforced: null,
    memory_max_bytes: null,
    memory_max_source: null,
    cpu_percent: null,
    ...overrides,
  };
}
