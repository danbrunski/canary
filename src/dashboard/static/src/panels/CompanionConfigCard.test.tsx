// The Pi-link card: companion mode off, a linked Pi, finding and choosing one,
// unlinking, and a refusal that must stay on screen.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HttpErrorStub, mockHttp } from "../test/harness";
import type { CompanionStatusResponse, DaemonIdentity } from "../types";

const mocks = mockHttp(["getCompanionStatus", "discoverPis", "selectPi", "clearPi"]);

const { CompanionConfigCard } = await import("./CompanionConfigCard");

const COMPANION = {
  baseUrl: "",
  token: "",
  setBaseUrl: vi.fn(),
  setToken: vi.fn(),
};

const IDENTITY: DaemonIdentity = {
  product: "canary",
  hostname: "canary-8974",
  host_kind: "raspberry_pi",
  version: "1.4.0",
  profile: "nissan-xterra-2013",
  dashboard_port: 8000,
  source_path: "/identity",
};

function status(overrides: Partial<CompanionStatusResponse> = {}): CompanionStatusResponse {
  return {
    mode: "local_library",
    library_root: "/home/x/.canary/library",
    sessions: 3,
    models: 2,
    datasets: 0,
    queued_actions: 0,
    pi: null,
    pi_token_present: true,
    discovery: { state: "idle", ran_at: null, found: [], error: null },
    ...overrides,
  };
}

function httpError(code: number, body: string): Error {
  return Object.assign(new HttpErrorStub(`HTTP ${code}: ${body}`), { status: code, body });
}

beforeEach(() => {
  COMPANION.setBaseUrl.mockReset();
  COMPANION.setToken.mockReset();
});

describe("CompanionConfigCard", () => {
  it("says companion mode is off on a 404 and offers no Pi actions", async () => {
    mocks.getCompanionStatus.mockRejectedValue(httpError(404, '{"detail":"Not Found"}'));
    render(<CompanionConfigCard companion={COMPANION} token="t" />);
    expect(
      await screen.findByText("Companion mode is off (companion.enabled in the config)"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Find Pi" })).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Companion API \(blank: this server\)/)).toBeInTheDocument();
  });

  it("shows the library line and 'No Pi linked'", async () => {
    mocks.getCompanionStatus.mockResolvedValue(status());
    render(<CompanionConfigCard companion={COMPANION} token="t" />);
    expect(
      await screen.findByText("Library: /home/x/.canary/library · 3 sessions · 2 models"),
    ).toBeInTheDocument();
    expect(screen.getByText("No Pi linked")).toBeInTheDocument();
  });

  it("renders the linked Pi with its source chip, and Unlink deletes the link", async () => {
    mocks.getCompanionStatus.mockResolvedValue(
      status({
        pi: {
          base_url: "http://canary-8974.local:8000",
          identity: IDENTITY,
          source: "discovered",
          selected_at: null,
        },
      }),
    );
    mocks.clearPi.mockResolvedValue({ pi: null });
    render(<CompanionConfigCard companion={COMPANION} token="tok" />);
    expect(await screen.findByText("canary-8974")).toBeInTheDocument();
    expect(screen.getByText("nissan-xterra-2013")).toBeInTheDocument();
    expect(screen.getByText("1.4.0")).toBeInTheDocument();
    expect(screen.getByText("http://canary-8974.local:8000")).toBeInTheDocument();
    expect(screen.getByText("found on the network")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Unlink" }));
    await waitFor(() => expect(mocks.clearPi).toHaveBeenCalledWith("tok"));
  });

  it("names the other two link sources in words", async () => {
    mocks.getCompanionStatus.mockResolvedValue(
      status({
        pi: {
          base_url: "http://10.0.0.5:8000",
          identity: null,
          source: "selected",
          selected_at: "x",
        },
      }),
    );
    const { unmount } = render(<CompanionConfigCard companion={COMPANION} token="t" />);
    expect(await screen.findByText("chosen here")).toBeInTheDocument();
    // No probed identity: the hostname falls back to the URL's host.
    expect(screen.getByText("10.0.0.5:8000")).toBeInTheDocument();
    unmount();

    mocks.getCompanionStatus.mockResolvedValue(
      status({
        pi: {
          base_url: "http://10.0.0.5:8000",
          identity: null,
          source: "configured",
          selected_at: null,
        },
      }),
    );
    render(<CompanionConfigCard companion={COMPANION} token="t" />);
    expect(await screen.findByText("from config")).toBeInTheDocument();
  });

  it("Find Pi lists what answered and Use links that base_url", async () => {
    mocks.getCompanionStatus.mockResolvedValue(status());
    mocks.discoverPis.mockResolvedValue({
      found: [
        {
          base_url: "http://192.168.137.2:8000",
          host: "192.168.137.2",
          source: "wired",
          identity: IDENTITY,
        },
      ],
      pi: null,
    });
    mocks.selectPi.mockResolvedValue({ pi: {} });
    render(<CompanionConfigCard companion={COMPANION} token="tok" />);
    await screen.findByText("No Pi linked");

    fireEvent.click(screen.getByRole("button", { name: "Find Pi" }));
    await waitFor(() => expect(mocks.discoverPis).toHaveBeenCalledWith("tok", false));
    expect(await screen.findByText("http://192.168.137.2:8000")).toBeInTheDocument();
    expect(screen.getByText("wired")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Use" }));
    await waitFor(() =>
      expect(mocks.selectPi).toHaveBeenCalledWith("tok", "http://192.168.137.2:8000"),
    );
  });

  it("asks for a subnet sweep only when the box is ticked", async () => {
    mocks.getCompanionStatus.mockResolvedValue(status());
    mocks.discoverPis.mockResolvedValue({ found: [], pi: null });
    render(<CompanionConfigCard companion={COMPANION} token="tok" />);
    await screen.findByText("No Pi linked");
    fireEvent.click(screen.getByLabelText(/scan the whole subnet/));
    fireEvent.click(screen.getByRole("button", { name: "Find Pi" }));
    await waitFor(() => expect(mocks.discoverPis).toHaveBeenCalledWith("tok", true));
  });

  it("shows the search state as words and disables Find Pi while it runs", async () => {
    mocks.getCompanionStatus.mockResolvedValue(
      status({ discovery: { state: "running", ran_at: null, found: [], error: null } }),
    );
    render(<CompanionConfigCard companion={COMPANION} token="tok" />);
    expect(await screen.findByText("Searching the network…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find Pi" })).toBeDisabled();
  });

  it("reports a finished search and a search error", async () => {
    mocks.getCompanionStatus.mockResolvedValue(
      status({
        discovery: {
          state: "done",
          ran_at: "2026-10-16T12:00:00Z",
          found: [],
          error: "multicast blocked",
        },
      }),
    );
    render(<CompanionConfigCard companion={COMPANION} token="tok" />);
    expect(await screen.findByText(/Last search .*: 0 found/)).toBeInTheDocument();
    expect(screen.getByText(/Search failed: multicast blocked/)).toBeInTheDocument();
  });

  it("keeps a 400 refusal from Use on screen", async () => {
    mocks.getCompanionStatus.mockResolvedValue(
      status({
        discovery: {
          state: "done",
          ran_at: "2026-10-16T12:00:00Z",
          found: [
            {
              base_url: "http://10.0.0.9:8000",
              host: "10.0.0.9",
              source: "sweep",
              identity: IDENTITY,
            },
          ],
          error: null,
        },
      }),
    );
    mocks.selectPi.mockRejectedValue(
      httpError(400, '{"detail":"nothing answered at that address"}'),
    );
    render(<CompanionConfigCard companion={COMPANION} token="tok" />);
    fireEvent.click(await screen.findByRole("button", { name: "Use" }));
    expect(
      await screen.findByText("Rejected by the server: nothing answered at that address"),
    ).toBeInTheDocument();
  });

  it("disables the Pi actions, and says why, without a control token", async () => {
    mocks.getCompanionStatus.mockResolvedValue(status());
    render(<CompanionConfigCard companion={COMPANION} token="" />);
    expect(await screen.findByRole("button", { name: "Find Pi" })).toBeDisabled();
    expect(screen.getByText(/needs the control token/)).toBeInTheDocument();
  });
});
