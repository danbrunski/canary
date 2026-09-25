// Ad-hoc analysis types (Tasks 5.1/5.3, engine-analysis-and-mapping.md).
// Mirrors src/analysis/binned_analysis.py + src/analysis/session_timeseries.py.

export type BinnedAggregation = "mean" | "median" | "p95" | "count";

export interface BinnedCell {
  mean: number;
  std: number;
  count: number;
}

export interface BinnedAnalysisUnavailable {
  available: false;
  reason: string;
  value_channel: string;
  x_channel: string;
  y_channel: string | null;
  agg: BinnedAggregation;
}

export interface BinnedAnalysis1D {
  available: true;
  kind: "1d";
  value_channel: string;
  x_channel: string;
  y_channel: null;
  agg: BinnedAggregation;
  sample_count: number;
  x_edges: number[];
  cells: BinnedCell[];
}

export interface BinnedAnalysis2D {
  available: true;
  kind: "2d";
  value_channel: string;
  x_channel: string;
  y_channel: string;
  agg: BinnedAggregation;
  sample_count: number;
  x_edges: number[];
  y_edges: number[];
  rows: number;
  cols: number;
  cells: BinnedCell[];
}

export type BinnedAnalysisResponse =
  BinnedAnalysis1D | BinnedAnalysis2D | BinnedAnalysisUnavailable;

export interface SessionChartSeries {
  timestamps: string[];
  values: (number | null)[];
}

export interface SessionChartResponse {
  session_id: string;
  resampled: boolean;
  hz: number | null;
  channels: Record<string, SessionChartSeries>;
}
