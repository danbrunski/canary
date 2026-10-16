import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../../api/core";
import {
  CAPTURE_ID,
  LOGS,
  deferred,
  logsResponse,
  ordinarySession,
} from "../../test/workbenchFixtures";
import type { LogsResponse, SessionChoice } from "../../types/workbench";
import { SessionChooser } from "./SessionChooser";

vi.mock("../../api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/core")>();
  return { ...actual, controlJson: vi.fn() };
});
const control = vi.mocked(controlJson);

afterEach(() => vi.clearAllMocks());

const WITH_SESSIONS = logsResponse({
  include_sessions: true,
  logs: [...LOGS.logs, ordinarySession()],
});
const EMPTY: SessionChoice = { sessions: [], revision: null };

/** The chooser with its choice held by a parent, as the page holds it. */
function Harness({
  initial = EMPTY,
  running = "ZS44A-20260920",
  selected = null,
  token = "t",
}: {
  initial?: SessionChoice;
  running?: string | null;
  selected?: string | null;
  token?: string;
}) {
  const [choice, setChoice] = useState(initial);
  return (
    <>
      <SessionChooser
        token={token}
        choice={choice}
        onChoice={setChoice}
        runningRevision={running}
        selectedLogId={selected}
      />
      <output aria-label="choice">{JSON.stringify(choice)}</output>
    </>
  );
}
const chosen = () =>
  JSON.parse(screen.getByLabelText("choice").textContent ?? "null") as SessionChoice;

function routeLogs(response: LogsResponse | Error = WITH_SESSIONS) {
  control.mockImplementation(() =>
    response instanceof Error ? Promise.reject(response) : Promise.resolve(response),
  );
}

describe("SessionChooser", () => {
  it("lists each session with its profile and revision, 'session' and 'unknown revision' when absent", async () => {
    routeLogs();
    render(<Harness />);
    await screen.findByText(CAPTURE_ID);
    const row = screen.getByText(CAPTURE_ID).closest("label");
    expect(row).toHaveTextContent("tuning · ZS44A-20260920");
    expect(screen.getByText("2026-09-19T120000").closest("label")).toHaveTextContent(
      "session · unknown revision",
    );
  });

  it("chooses and unchooses sessions, and a session choice replaces a revision choice", async () => {
    routeLogs();
    render(<Harness />);
    await screen.findByText(CAPTURE_ID);
    expect(screen.getByText("0 chosen")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /All on the running calibration/ }));
    expect(chosen()).toEqual({ sessions: [], revision: "ZS44A-20260920" });
    expect(screen.getByText("all on ZS44A-20260920")).toBeInTheDocument();
    expect(screen.queryByText(/Choose sessions or a revision/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText(new RegExp(CAPTURE_ID)));
    expect(chosen()).toEqual({ sessions: [CAPTURE_ID], revision: null });
    expect(screen.getByText("1 chosen")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/2026-09-19T120000/));
    expect(chosen().sessions).toEqual([CAPTURE_ID, "2026-09-19T120000"]);
    fireEvent.click(screen.getByLabelText(new RegExp(CAPTURE_ID)));
    expect(chosen().sessions).toEqual(["2026-09-19T120000"]);
    expect(screen.getByLabelText(new RegExp(CAPTURE_ID))).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(chosen()).toEqual(EMPTY);
    expect(screen.getByText(/Choose sessions or a revision/)).toBeInTheDocument();
  });

  it("offers the selected log only when the group has one, and chooses just that session", async () => {
    routeLogs();
    const none = render(<Harness />);
    await screen.findByText(CAPTURE_ID);
    expect(screen.queryByRole("button", { name: "Use the selected log" })).not.toBeInTheDocument();
    none.unmount();

    render(<Harness selected={CAPTURE_ID} />);
    await screen.findByText(CAPTURE_ID);
    fireEvent.click(screen.getByRole("button", { name: "Use the selected log" }));
    expect(chosen()).toEqual({ sessions: [CAPTURE_ID], revision: null });
    // Once something is chosen the prompt, and with it that button, is gone.
    expect(screen.queryByRole("button", { name: "Use the selected log" })).not.toBeInTheDocument();
  });

  it("will not offer the running calibration before it is known", async () => {
    routeLogs();
    render(<Harness running={null} />);
    await screen.findByText(CAPTURE_ID);
    const button = screen.getByRole("button", { name: "All on the running calibration" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "The running calibration is not known yet");
    fireEvent.click(button);
    expect(chosen()).toEqual(EMPTY);
  });

  it("lists ordinary sessions only on request, one listing per choice", async () => {
    control.mockImplementation((path: string) =>
      Promise.resolve(
        path.endsWith("=true") ? WITH_SESSIONS : logsResponse({ logs: [LOGS.logs[0]] }),
      ),
    );
    render(<Harness />);
    await screen.findByText(CAPTURE_ID);
    expect(screen.queryByText("2026-09-19T120000")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Include ordinary sessions"));
    await screen.findByText("2026-09-19T120000");
    expect(control).toHaveBeenCalledWith(
      "/control/logs?include_sessions=true",
      "t",
      expect.anything(),
    );
  });

  it("asks for nothing without a token", async () => {
    routeLogs();
    render(<Harness token="" />);
    await new Promise((r) => setTimeout(r, 20));
    expect(control).not.toHaveBeenCalled();
    expect(screen.getByText(/Choose sessions or a revision/)).toBeInTheDocument();
  });

  it("shows a refused listing in the shared words and no sessions under it", async () => {
    routeLogs(new HttpError(401, ""));
    render(<Harness />);
    expect(
      await screen.findByText("Not authorized — enter or check the control token."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  it("keeps the choice when the listing is toggled, and ignores the superseded listing", async () => {
    const stale = deferred<LogsResponse>();
    control.mockImplementation((path: string) =>
      path.endsWith("=false") ? stale.promise : Promise.resolve(WITH_SESSIONS),
    );
    render(<Harness initial={{ sessions: ["2026-09-19T120000"], revision: null }} />);
    fireEvent.click(screen.getByLabelText("Include ordinary sessions"));
    await screen.findByText("2026-09-19T120000");
    stale.resolve(logsResponse({ logs: [LOGS.logs[1]] }));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByText("2026-09-19T120000")).toBeInTheDocument();
    expect(screen.getByLabelText(/2026-09-19T120000/)).toBeChecked();
    await waitFor(() => expect(chosen().sessions).toEqual(["2026-09-19T120000"]));
  });
});
