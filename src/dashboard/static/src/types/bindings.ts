// ---------------------------------------------------------------------------
// Bindings — display-projection from data/bindings.yaml.
// ---------------------------------------------------------------------------

/** One binding entry as the dashboard sees it. Mirrors
 *  `read_bindings_for_display`'s payload in src/dashboard/api.py. */
export interface BindingDisplay {
  name: string;
  scale: number | null;
  offset: number | null;
  unit: string;
  confidence: "confirmed" | "high" | "medium" | "low" | "hypothesized";
  plausible_min: number | null;
  plausible_max: number | null;
}

/** Whole `/bindings` payload — keyed by `decoded:<NAME>`. */
export type BindingsByChannel = Record<string, BindingDisplay>;
