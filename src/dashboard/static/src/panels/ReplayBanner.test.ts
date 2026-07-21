import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ReplayStatus } from "../types";
import { ReplayBanner } from "./ReplayBanner";

const RUNNING: ReplayStatus = {
  running: true,
  replay_id: "replay-1",
  source_session_id: "2026-05-17T120000",
  started_at: "2026-07-19T10:00:00+00:00",
  speed: 2,
  loop: false,
  output_session_dir: "_replay-2026-07-19T100000",
  output_anomalies_path: "_replay-2026-07-19T100000/anomalies.jsonl",
};

describe("ReplayBanner (Task 1.2)", () => {
  it("renders nothing when the replay block is absent", () => {
    render(createElement(ReplayBanner, { replay: null }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders nothing when replay is inactive", () => {
    render(createElement(ReplayBanner, { replay: { ...RUNNING, running: false } }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows a prominent banner naming the session while active", () => {
    render(createElement(ReplayBanner, { replay: RUNNING }));
    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent("REPLAY — session 2026-05-17T120000");
    expect(banner).toHaveTextContent("2x");
  });
});
