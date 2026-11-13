/**
 * Telemetry replaces the channel Map up to once a frame (R28.1). Every commit
 * re-renders `Workbench`, so a page that shows no channel must not render with
 * it: this counts renders under the real stream hook, for a page that ignores
 * the channels (Control) and one that reads them (Alarms).
 */
import { act, cleanup, render } from "@testing-library/react";
import { Profiler } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TelemetryRow } from "../types";

const mocks = vi.hoisted(() => ({
  getLatestPerChannel: vi.fn(),
  openReconnectingSocket: vi.fn(),
  renders: { control: 0, alarms: 0 },
}));
vi.mock("../api/http", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/http")>()),
  getLatestPerChannel: mocks.getLatestPerChannel,
}));
vi.mock("../api/websocket", () => ({
  openReconnectingSocket: mocks.openReconnectingSocket,
}));
vi.mock("./ControlPanel", () => ({
  ControlPanel: () => {
    mocks.renders.control += 1;
    return <p>control page</p>;
  },
}));
vi.mock("./AlarmsPanel", () => ({
  AlarmsPanel: ({ channels }: { channels: Map<string, unknown> }) => {
    mocks.renders.alarms += 1;
    return <p>alarms page: {channels.size} channels</p>;
  },
}));

const { Workbench } = await import("./Workbench");
const { useTelemetryStream } = await import("../hooks/useTelemetryStream");
const { useCompanionSettings } = await import("../hooks/useCompanionSettings");

const FRAMES = 20;
const PER_FRAME = 100;

function Harness({
  item,
  commits,
}: {
  item: "system-control" | "observe-alarms";
  commits: number[];
}) {
  const telemetry = useTelemetryStream();
  const companion = useCompanionSettings();
  return (
    <Profiler id="app" onRender={() => commits.push(1)}>
      <Workbench
        active={item}
        onNavigate={() => {}}
        data={{
          mode: "parked",
          channels: telemetry.channels,
          bindings: {},
          events: [],
          liveScores: {},
          status: null,
          statusError: null,
          behaviorModels: null,
          behaviorModelsError: null,
          behaviorLive: null,
          token: "t",
          companion,
        }}
      />
    </Profiler>
  );
}

/** Mount, then deliver FRAMES frames of PER_FRAME messages each, one commit
 *  per frame. Returns what the page rendered during the stream alone. */
async function stream(item: "system-control" | "observe-alarms") {
  let onMessage: (row: TelemetryRow) => void = () => {};
  mocks.openReconnectingSocket.mockImplementation(
    (opts: { onMessage: (row: TelemetryRow) => void }) => {
      onMessage = opts.onMessage;
      return { close: vi.fn() };
    },
  );
  const commits: number[] = [];
  render(<Harness item={item} commits={commits} />);
  await act(async () => {
    await Promise.resolve();
  });
  const before = { ...mocks.renders, commits: commits.length };
  for (let frame = 0; frame < FRAMES; frame += 1) {
    await act(async () => {
      for (let i = 0; i < PER_FRAME; i += 1) {
        onMessage({
          timestamp: "2026-11-13T12:00:00+00:00",
          source: "can",
          channel: `ch${i}`,
          value: frame,
          unit: null,
        });
      }
      await vi.advanceTimersByTimeAsync(20);
    });
  }
  return {
    commits: commits.length - before.commits,
    control: mocks.renders.control - before.control,
    alarms: mocks.renders.alarms - before.alarms,
  };
}

beforeEach(() => {
  mocks.getLatestPerChannel.mockResolvedValue({ rows: [], count: 0 });
  mocks.renders.control = 0;
  mocks.renders.alarms = 0;
  vi.useFakeTimers({
    toFake: ["requestAnimationFrame", "cancelAnimationFrame", "setTimeout", "clearTimeout"],
  });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("Workbench under a telemetry stream", () => {
  it("does not render a page that shows no channel when the channels change", async () => {
    const seen = await stream("system-control");
    // 2,000 messages, one commit a frame: the shell commits every frame and
    // the Control page not at all.
    expect(seen.commits).toBe(FRAMES);
    expect(seen.control).toBe(0);
  });

  it("renders a page that reads the channels once per commit", async () => {
    const seen = await stream("observe-alarms");
    expect(seen.commits).toBe(FRAMES);
    expect(seen.alarms).toBe(FRAMES);
  });
});
