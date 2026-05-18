import { describe, expect, it } from "vitest";

import type { ScoreMessage } from "../types";
import { applyScore, MAX_PER_SOURCE, type ScoresBySource } from "./useScoreStream";

function msg(source: ScoreMessage["source"], score: number | null): ScoreMessage {
  return {
    type: "score",
    source,
    score,
    threshold: null,
    operating_mode: "idle",
    model_version: "v1",
    timestamp: "now",
    missing_features: score === null,
  };
}

describe("applyScore", () => {
  it("appends per source, keeping sources independent", () => {
    let s: ScoresBySource = {};
    s = applyScore(s, msg("isolation_forest", 0));
    s = applyScore(s, msg("lstm_autoencoder", 1.2));
    s = applyScore(s, msg("isolation_forest", 0.05));
    expect(s.isolation_forest?.map((m) => m.score)).toEqual([0, 0.05]);
    expect(s.lstm_autoencoder?.map((m) => m.score)).toEqual([1.2]);
  });

  it("keeps a normal 0 (does not drop falsy scores)", () => {
    const s = applyScore({}, msg("isolation_forest", 0));
    expect(s.isolation_forest).toHaveLength(1);
    expect(s.isolation_forest?.[0].score).toBe(0);
  });

  it("caps the ring at MAX_PER_SOURCE, keeping the newest", () => {
    let s: ScoresBySource = {};
    for (let i = 0; i < MAX_PER_SOURCE + 25; i++) {
      s = applyScore(s, msg("isolation_forest", i));
    }
    const ring = s.isolation_forest ?? [];
    expect(ring).toHaveLength(MAX_PER_SOURCE);
    expect(ring[0].score).toBe(25); // oldest 25 dropped
    expect(ring[ring.length - 1].score).toBe(MAX_PER_SOURCE + 24);
  });

  it("does not mutate the previous state object", () => {
    const a: ScoresBySource = {};
    const b = applyScore(a, msg("isolation_forest", 0));
    expect(a).toEqual({});
    expect(b).not.toBe(a);
  });
});
