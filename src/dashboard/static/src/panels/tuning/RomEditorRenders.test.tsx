/**
 * Telemetry replaces `channels` up to once a frame (R28.1). The editor's
 * 500-line `Workspace` shows no channel itself (only the live overlay does, and
 * it reads them from context), so a new Map must not re-render it while the
 * operator edits.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { controlJson } from "../../api/core";
import { ENTRY_TUNED, SHA_TUNED, TABLES_ALL_2D } from "../../test/romLibraryFixtures";
import type { ChannelSnapshot } from "../../hooks/useTelemetryStream";
import { RomEditorPage } from "./RomEditorPage";

const counted = vi.hoisted(() => ({ workspace: 0 }));
// PendingEdits renders unconditionally inside Workspace, so its render count is
// the Workspace's.
vi.mock("./romEditor/PendingEdits", async (importOriginal) => {
  const real = await importOriginal<typeof import("./romEditor/PendingEdits")>();
  return {
    ...real,
    PendingEdits: (props: Parameters<typeof real.PendingEdits>[0]) => {
      counted.workspace += 1;
      return real.PendingEdits(props);
    },
  };
});
vi.mock("../../api/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/core")>()),
  controlJson: vi.fn(),
}));
const control = vi.mocked(controlJson);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});

const frame = (n: number): Map<string, ChannelSnapshot> =>
  new Map([
    [
      "did:RPM",
      {
        latest: {
          timestamp: "2026-11-13T12:00:00Z",
          source: "did",
          channel: "RPM",
          value: n,
          unit: null,
        },
        history: [],
      },
    ],
  ]);

describe("ROM Editor under a telemetry stream", () => {
  it("does not re-render the workspace when the channels change", async () => {
    control.mockImplementation(async (path: string) => {
      if (path === "/control/rom/library") return { entries: [ENTRY_TUNED] };
      if (path === `/control/rom/library/${SHA_TUNED}/tables`) return TABLES_ALL_2D;
      throw new Error(`unexpected ${path}`);
    });
    window.history.replaceState(null, "", `/?rom=${SHA_TUNED}`);
    const view = render(<RomEditorPage token="t" status={null} channels={frame(0)} />);
    await screen.findByRole("navigation", { name: "Tables" });
    const settled = counted.workspace;
    expect(settled).toBeGreaterThan(0);
    for (let n = 1; n <= 50; n += 1) {
      view.rerender(<RomEditorPage token="t" status={null} channels={frame(n)} />);
    }
    expect(counted.workspace).toBe(settled);
  });
});
