import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../../api/core";
import { CAPTURE_ID, LOGS, logsResponse, ordinarySession } from "../../test/workbenchFixtures";
import type { LogsResponse } from "../../types/workbench";
import { LogBrowser } from "./LogBrowser";

vi.mock("../../api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/core")>();
  return { ...actual, controlJson: vi.fn() };
});
const control = vi.mocked(controlJson);

afterEach(() => vi.clearAllMocks());

function answer(reply: LogsResponse | Error) {
  control.mockImplementation(() =>
    reply instanceof Error ? Promise.reject(reply) : Promise.resolve(reply),
  );
}
const rows = async () =>
  within(await screen.findByRole("table", { name: "Capture browser" }))
    .getAllByRole("row")
    .slice(1);

describe("LogBrowser", () => {
  it("lists each capture with its profile, start, duration, revision, quality, end and rows", async () => {
    answer(LOGS);
    render(<LogBrowser token="t" selectedId={null} onSelect={() => {}} />);
    const [first, second] = await rows();
    const cells = (row: HTMLElement) =>
      within(row)
        .getAllByRole("cell")
        .map((c) => c.textContent);
    expect(cells(first)).toEqual([
      "tuning",
      new Date("2026-09-20T12:00:00+00:00").toLocaleString(),
      "12:34",
      "ZS44A-20260920 · capture manifest",
      "pass",
      "operator stop",
      "123,456",
    ]);
    expect(cells(second)[3]).toBe("ZS44A-20260727 · registry⚑ flash inside");
    expect(cells(second)[4]).toBe("unknown");
    expect(first.querySelector("td:nth-child(2)")).toHaveAttribute("title", CAPTURE_ID);
    expect(first.querySelector("td:nth-child(3)")).toHaveAttribute("title", "capture_manifest");
  });

  it("falls back to words for what a session never recorded", async () => {
    answer(
      logsResponse({
        include_sessions: true,
        logs: [
          ordinarySession("2026-09-19T120000"),
          { ...LOGS.logs[0], profile: null, end_reason: null, row_count: null, duration_s: null },
        ],
      }),
    );
    render(<LogBrowser token="t" selectedId={null} onSelect={() => {}} />);
    const [session, capture] = await rows();
    const text = (row: HTMLElement) =>
      within(row)
        .getAllByRole("cell")
        .map((c) => c.textContent);
    // An ordinary session: no profile, no start (the id stands in), nothing known.
    expect(text(session)).toEqual([
      "session",
      "2026-09-19T120000",
      "unknown",
      "unknown · unknown",
      "unknown",
      "—",
      "unknown",
    ]);
    // A capture still open: no end reason yet.
    expect(text(capture)[0]).toBe("unknown");
    expect(text(capture)[5]).toBe("open");
  });

  it("colours the quality verdict: pass ok, any other verdict warn, none faint", async () => {
    answer(
      logsResponse({
        logs: [
          LOGS.logs[0],
          { ...LOGS.logs[0], session_id: "b", quality: { verdict: "fail", scope: "stop" } },
          { ...LOGS.logs[0], session_id: "c", quality: null },
        ],
      }),
    );
    render(<LogBrowser token="t" selectedId={null} onSelect={() => {}} />);
    const verdicts = (await rows()).map((row) => within(row).getAllByRole("cell")[4]);
    expect(verdicts.map((cell) => cell.className)).toEqual([
      "text-ok",
      "text-warn",
      "text-fg-faint",
    ]);
  });

  it("marks the selected capture and reports a click", async () => {
    answer(LOGS);
    const picked: string[] = [];
    render(
      <LogBrowser
        token="t"
        selectedId={LOGS.logs[1].session_id}
        onSelect={(id) => picked.push(id)}
      />,
    );
    const listed = await rows();
    expect(listed.map((row) => row.getAttribute("aria-selected"))).toEqual(["false", "true"]);
    fireEvent.click(listed[0]);
    expect(picked).toEqual([CAPTURE_ID]);
  });

  it("counts the listing, and says how many of the total when it was capped", async () => {
    answer(LOGS);
    const first = render(<LogBrowser token="t" selectedId={null} onSelect={() => {}} />);
    await rows();
    expect(screen.getByText("2")).toBeInTheDocument();
    first.unmount();
    answer(logsResponse({ capped: true, total: 150 }));
    render(<LogBrowser token="t" selectedId={null} onSelect={() => {}} />);
    await rows();
    expect(screen.getByText("2 of 150")).toBeInTheDocument();
  });

  it("asks for ordinary sessions only on request", async () => {
    answer(LOGS);
    render(<LogBrowser token="t" selectedId={null} onSelect={() => {}} />);
    await rows();
    expect(control).toHaveBeenCalledWith(
      "/control/logs?include_sessions=false",
      "t",
      expect.anything(),
    );
    fireEvent.click(screen.getByLabelText("Include ordinary sessions"));
    await waitFor(() =>
      expect(control).toHaveBeenCalledWith(
        "/control/logs?include_sessions=true",
        "t",
        expect.anything(),
      ),
    );
  });

  it("says an empty listing in the words of the listing asked for", async () => {
    answer(logsResponse({ logs: [], total: 0 }));
    render(<LogBrowser token="t" selectedId={null} onSelect={() => {}} />);
    expect(await screen.findByText(/No captures yet/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Include ordinary sessions"));
    expect(await screen.findByText("No sessions recorded yet.")).toBeInTheDocument();
  });

  it("asks for the token and requests nothing without one", async () => {
    answer(LOGS);
    render(<LogBrowser token="" selectedId={null} onSelect={() => {}} />);
    expect(screen.getByText("Enter the control token to browse logs.")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 20));
    expect(control).not.toHaveBeenCalled();
  });

  it("shows a refused listing in the shared words, with no table", async () => {
    answer(new HttpError(500, "boom"));
    render(<LogBrowser token="t" selectedId={null} onSelect={() => {}} />);
    expect(await screen.findByText("Server error 500.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});
