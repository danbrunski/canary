import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";

import { HttpErrorStub, mockHttp } from "../test/harness";
import type { SessionSummary } from "../types";

const mocks = mockHttp(["listSessions", "getReplay", "startReplay", "stopReplay"]);

const { SessionsPanel } = await import("./SessionsPanel");

const COMPANION = {
  baseUrl: "",
  token: "",
  setBaseUrl: () => {},
  setToken: () => {},
};

/** Wait until ReplayCard's picker holds `sessionId`. The session list lands
 *  in one commit, but the picker's value arrives two effect passes later
 *  (SessionsPanel's selection effect, then ReplayCard's follow effect). A
 *  test that waits for the list text and then reads the picker, or clicks
 *  Start, raced those passes and lost under load (T01.10): Start with an
 *  empty picker throws "Select a session first." and never calls
 *  startReplay. Wait for the state the next step depends on. */
async function pickerHolds(sessionId: string): Promise<void> {
  const picker = screen.getByRole<HTMLSelectElement>("combobox");
  await waitFor(() => expect(picker.value).toBe(sessionId));
}

function session(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    session_id: "2026-05-17T120000",
    session_path: "/data/canary/2026-05-17T120000",
    started_at: "2026-05-17T12:00:00+00:00",
    telemetry_chunks: 10,
    tag_count: 2,
    has_manifest: true,
    pinned: false,
    metadata: {},
    size_bytes: 1024,
    ...overrides,
  };
}

describe("SessionsPanel (Task 3.1 — promotes the extracted SessionsCard)", () => {
  // Load, reproduced on purpose (T01.10). React's scheduler runs queued work
  // (renders, passive effects) in 5 ms slices measured by performance.now(),
  // and yields to the event loop when a slice is spent. On a busy host a
  // slice runs out between the commit that renders the session list and the
  // effects that set the picker, so testing-library sees the text and returns
  // before the picker is set. Advancing the clock 10 ms per read makes every
  // slice run out, so the race loses on every run instead of only on a busy
  // host. Before the waits below, all three session tests failed this way.
  let schedulerClock: MockInstance | undefined;
  beforeEach(() => {
    const realNow = performance.now.bind(performance);
    let skew = 0;
    schedulerClock = vi
      .spyOn(performance, "now")
      .mockImplementation(() => realNow() + (skew += 10));
  });
  afterEach(() => schedulerClock?.mockRestore());

  it("prompts for a token before fetching anything", () => {
    render(createElement(SessionsPanel, { token: "", companion: COMPANION }));
    expect(mocks.listSessions).not.toHaveBeenCalled();
  });

  it("hosts SessionsCard and ReplayCard side by side, both sharing the session list", async () => {
    mocks.listSessions.mockResolvedValue({
      sessions: [session(), session({ session_id: "2026-05-18T090000" })],
      count: 2,
    });
    mocks.getReplay.mockResolvedValue({
      replay: {
        running: false,
        replay_id: null,
        source_session_id: null,
        started_at: null,
        speed: null,
        loop: null,
        output_session_dir: null,
        output_anomalies_path: null,
      },
    });
    render(createElement(SessionsPanel, { token: "secret", companion: COMPANION }));

    // The session id appears twice (the Sessions list button and the
    // Replay picker's `<option>`) — just wait for it to land anywhere.
    expect((await screen.findAllByText("2026-05-18T090000")).length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "Sessions" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Replay" })).toBeInTheDocument();
    // ReplayCard's picker defaults to the most-recently-selected session.
    await pickerHolds("2026-05-18T090000");
  });

  it("starts a replay for the selected session", async () => {
    mocks.listSessions.mockResolvedValue({ sessions: [session()], count: 1 });
    mocks.getReplay.mockResolvedValue({
      replay: {
        running: false,
        replay_id: null,
        source_session_id: null,
        started_at: null,
        speed: null,
        loop: null,
        output_session_dir: null,
        output_anomalies_path: null,
      },
    });
    mocks.startReplay.mockResolvedValue({
      replay: {
        running: true,
        replay_id: "r1",
        source_session_id: "2026-05-17T120000",
        started_at: "2026-07-19T10:00:00+00:00",
        speed: 1,
        loop: false,
        output_session_dir: "_replay-2026-07-19T100000",
        output_anomalies_path: "_replay-2026-07-19T100000/anomalies.jsonl",
      },
    });
    render(createElement(SessionsPanel, { token: "secret", companion: COMPANION }));

    await pickerHolds("2026-05-17T120000");
    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    expect(await screen.findByText(/_replay-2026-07-19T100000/)).toBeInTheDocument();
    expect(mocks.startReplay).toHaveBeenCalledWith("secret", {
      session_id: "2026-05-17T120000",
      speed: 1,
      loop: false,
    });
  });

  it("renders the 409 refuse-while-driving reason verbatim (7.2, no paraphrasing)", async () => {
    mocks.listSessions.mockResolvedValue({ sessions: [session()], count: 1 });
    mocks.getReplay.mockResolvedValue({
      replay: {
        running: false,
        replay_id: null,
        source_session_id: null,
        started_at: null,
        speed: null,
        loop: null,
        output_session_dir: null,
        output_anomalies_path: null,
      },
    });
    const err = new HttpErrorStub("conflict");
    err.status = 409;
    err.body = JSON.stringify({
      detail:
        "refusing to start replay: vehicle_state=engine_running (replay is a bench/desk tool; wait for ignition_only/engine_off or key out first)",
    });
    mocks.startReplay.mockRejectedValue(err);
    render(createElement(SessionsPanel, { token: "secret", companion: COMPANION }));

    await pickerHolds("2026-05-17T120000");
    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    expect(
      await screen.findByText(
        "refusing to start replay: vehicle_state=engine_running (replay is a bench/desk tool; wait for ignition_only/engine_off or key out first)",
      ),
    ).toBeInTheDocument();
  });
});
