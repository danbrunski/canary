// AlarmsPanel's 1 s evaluation tick must keep firing while telemetry
// streams. It used to be keyed on `channels`, so every telemetry update
// re-armed the interval and a breach never fired during a drive.
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { AlarmsPanel } from "./AlarmsPanel";

const KEY = "canary.alarms";

function channels(value: number): Map<string, ChannelSnapshot> {
  return new Map([
    [
      "obd:COOLANT_TEMP",
      {
        latest: {
          timestamp: "2026-11-12T10:00:00+00:00",
          source: "obd",
          channel: "COOLANT_TEMP",
          value,
          unit: "C",
        },
        history: [value],
      },
    ],
  ]);
}

describe("AlarmsPanel", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    window.localStorage.setItem(
      KEY,
      JSON.stringify([
        {
          id: "r1",
          channel: "obd:COOLANT_TEMP",
          op: ">",
          value: 100,
          severity: "critical",
          debounce: 3,
          cooldownS: 30,
          enabled: true,
        },
      ]),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    window.localStorage.clear();
  });

  it("fires a sustained breach while channels update faster than the tick", () => {
    const { rerender } = render(<AlarmsPanel channels={channels(105)} />);
    // A fresh channels Map every 50 ms (a frame-rate stream) for 5 s.
    for (let step = 0; step < 100; step += 1) {
      act(() => {
        vi.advanceTimersByTime(50);
      });
      rerender(<AlarmsPanel channels={channels(105 + (step % 3))} />);
    }
    expect(screen.getByText("History")).toBeTruthy();
  });

  it("does not fire below the debounce", () => {
    const { rerender } = render(<AlarmsPanel channels={channels(105)} />);
    for (let step = 0; step < 30; step += 1) {
      act(() => {
        vi.advanceTimersByTime(50);
      });
      rerender(<AlarmsPanel channels={channels(105)} />);
    }
    expect(screen.queryByText("History")).toBeNull();
  });
});
