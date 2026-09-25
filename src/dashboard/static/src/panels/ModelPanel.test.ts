import { createElement } from "react";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SequenceDetectorStatus, StatusResponse } from "../types";
import { ModelPanel } from "./ModelPanel";

function status(overrides: Partial<StatusResponse>): StatusResponse {
  return {
    storage_path: "/tmp/canary",
    free_mb: 1024,
    latest_session: null,
    latest_session_path: null,
    model_versions: {},
    vehicle: "2008 Nissan Xterra",
    emitter_counters: null,
    health: null,
    ...overrides,
  };
}

const TCN_ACTIVE: SequenceDetectorStatus = {
  selector: "tcn",
  active: "tcn_autoencoder",
  active_version: "tcn_v1",
  available: {
    lstm_autoencoder: {
      selector: "lstm",
      artifact_present: true,
      version: "lstm_v1",
      active: false,
    },
    tcn_autoencoder: { selector: "tcn", artifact_present: true, version: "tcn_v1", active: true },
  },
};

describe("ModelPanel sequence detector", () => {
  it("shows the active backbone + version", () => {
    const { container } = render(
      createElement(ModelPanel, { status: status({ sequence_detector: TCN_ACTIVE }), events: [] }),
    );
    expect(container.textContent).toContain("sequence detector");
    expect(container.textContent).toContain("tcn_autoencoder (tcn_v1)");
  });

  it("flags a live override when active diverges from the config selector", () => {
    const overridden: SequenceDetectorStatus = {
      ...TCN_ACTIVE,
      selector: "lstm", // config default lstm, but tcn is live
    };
    const { container } = render(
      createElement(ModelPanel, { status: status({ sequence_detector: overridden }), events: [] }),
    );
    expect(container.textContent).toContain("live override");
  });

  it("omits the row when no sequence detector is active", () => {
    const { container } = render(
      createElement(ModelPanel, { status: status({ sequence_detector: null }), events: [] }),
    );
    expect(container.textContent).not.toContain("sequence detector");
  });
});
