// Characterisation of the cards that hand-wrote the panel shell before R08.12
// moved them onto <Panel>: each keeps its heading, and its header status sits
// beside the title inside the panel's <header>.
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { mockHttp } from "../test/harness";
import type { PreflightResponse, TestProfile } from "../types";

const mocks = mockHttp([
  "cancelJob",
  "getJobLogs",
  "submitJob",
  "uploadModelArtifact",
  "switchSequenceDetector",
  "deleteSession",
  "downloadSession",
  "pinSession",
  "syncSessionToCompanion",
  "unpinSession",
  "updateSessionMetadata",
  "getReplay",
  "startReplay",
  "stopReplay",
]);

const { ControlEventsCard, PreflightCard } = await import("./ControlPanelSections");
const { JobsCard } = await import("./JobsCard");
const { ModelUploadCard } = await import("./ModelUploadCard");
const { SequenceDetectorCard } = await import("./SequenceDetectorCard");
const { TestSessionSetupScreen } = await import("./TestSessionScreens");
const { SessionsCard } = await import("./SessionsCard");
const { ReplayCard } = await import("./ReplayCard");

const run = vi.fn(() => Promise.resolve());

/** The <header> that holds the panel titled `title`. */
function headerOf(title: string): HTMLElement {
  const heading = screen.getByRole("heading", { level: 2, name: title });
  const header = heading.closest("header");
  expect(header).not.toBeNull();
  expect(header!.parentElement!.tagName).toBe("SECTION");
  return header!;
}

const PREFLIGHT: PreflightResponse = { ok: false, checked_at: "2026-09-24T10:00:00Z", checks: [] };

describe("panel shells (R08.12)", () => {
  it("PreflightCard shows its verdict in the header", () => {
    render(<PreflightCard hasToken preflight={PREFLIGHT} />);
    expect(within(headerOf("Preflight")).getByText("attention")).toHaveClass("text-bad");
  });

  it("ControlEventsCard shows the stream state in the header", () => {
    render(<ControlEventsCard connection="open" events={[]} />);
    expect(within(headerOf("Control Events")).getByText("open")).toBeInTheDocument();
  });

  it("JobsCard counts jobs in the header", () => {
    render(<JobsCard token="t" busy={null} run={run} jobs={[]} />);
    expect(within(headerOf("Jobs")).getByText("0")).toBeInTheDocument();
  });

  it("ModelUploadCard is a titled panel", () => {
    render(<ModelUploadCard token="t" busy={null} run={run} />);
    expect(headerOf("Model Upload")).toBeInTheDocument();
  });

  it("SequenceDetectorCard names the active backbone in the header", () => {
    render(
      <SequenceDetectorCard
        token="t"
        busy={null}
        run={run}
        sequenceDetector={{ selector: "tcn", active: "tcn", active_version: null, available: {} }}
      />,
    );
    expect(within(headerOf("Sequence Detector")).getByText("tcn")).toBeInTheDocument();
  });

  it("TestSessionSetupScreen: profile, preflight verdict and orphan clearing", () => {
    const onClearOrphans = vi.fn();
    const profile: TestProfile = {
      id: "p",
      label: "Pull",
      description: "",
      protocol: "",
      vehicle_state: "engine_running",
      purpose: "",
      order: 1,
      events: [],
    };
    render(
      <TestSessionSetupScreen
        profiles={[profile]}
        profilesLoaded
        selectedProfileId="p"
        onSelectProfile={() => {}}
        selectedProfile={profile}
        authRejected={false}
        preflight={{ ...PREFLIGHT, ok: true }}
        purpose=""
        onPurposeChange={() => {}}
        busy={false}
        onStart={() => {}}
        onRerunPreflight={() => {}}
        recent={[]}
        orphans={2}
        onClearOrphans={onClearOrphans}
      />,
    );
    expect(headerOf("Test profile")).toBeInTheDocument();
    expect(within(headerOf("Preflight")).getByText("GO")).toHaveClass("text-ok");
    fireEvent.click(within(headerOf("Recent events")).getByText("clear 2 orphaned"));
    expect(onClearOrphans).toHaveBeenCalled();
  });

  it("SessionsCard counts sessions in the header", () => {
    render(
      <SessionsCard
        token="t"
        run={run}
        sessions={[]}
        companion={{ baseUrl: "", token: "", setBaseUrl: () => {}, setToken: () => {} }}
        selectedSessionId=""
        onSelectSessionId={() => {}}
      />,
    );
    expect(within(headerOf("Sessions")).getByText("0")).toBeInTheDocument();
  });

  it("ReplayCard shows idle in the header before any replay", () => {
    mocks.getReplay.mockReturnValue(new Promise(() => {}));
    render(<ReplayCard token="t" run={run} sessions={[]} selectedSessionId="" />);
    expect(within(headerOf("Replay")).getByText("idle")).toHaveClass("text-fg-dim");
  });
});
