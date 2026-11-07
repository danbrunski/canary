import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../../api/core";
import { ENTRY_TUNED, SHA_TUNED, backupState, readyStatus } from "../../test/romLibraryFixtures";
import type { StatusResponse } from "../../types";
import type { RomBackupState, RomLibraryEntry } from "../../types/romLibrary";
import { EcuBackupCard } from "./EcuBackupCard";

vi.mock("../../api/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/core")>()),
  controlJson: vi.fn(),
}));
const control = vi.mocked(controlJson);

/** Route the mocked control client by `METHOD path`; GET /control/rom/backup
 *  answers `polled` unless overridden. */
function route(polled: RomBackupState, overrides: Record<string, unknown> = {}) {
  control.mockImplementation((path: string, _token: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${path}`;
    if (key in overrides) {
      const value = overrides[key];
      return value instanceof Error ? Promise.reject(value) : Promise.resolve(value as never);
    }
    if (key === "GET /control/rom/backup") return Promise.resolve(polled as never);
    return Promise.reject(new Error(`unexpected ${key}`));
  });
}

function renderCard({
  status = readyStatus(),
  entries = [ENTRY_TUNED],
  token = "t",
}: { status?: StatusResponse | null; entries?: RomLibraryEntry[]; token?: string } = {}) {
  const onOpenEntry = vi.fn();
  const onLibraryChanged = vi.fn();
  render(
    <EcuBackupCard
      token={token}
      status={status}
      entries={entries}
      onOpenEntry={onOpenEntry}
      onLibraryChanged={onLibraryChanged}
    />,
  );
  return { onOpenEntry, onLibraryChanged };
}

afterEach(() => {
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("ECU backup card (W08.8)", () => {
  it("idle: every precondition met, Start enabled", async () => {
    route(backupState());
    renderCard();
    expect(await screen.findByText(/No backup has run since the daemon started/)).toBeVisible();
    const chips = within(screen.getByLabelText("Backup preconditions"));
    expect(chips.getByText("Vehicle: ignition_only")).toBeInTheDocument();
    expect(chips.getByText("Passive: off")).toBeInTheDocument();
    expect(chips.getByText("Foreign tester: none heard")).toBeInTheDocument();
    expect(chips.getByText("Capture: none")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back up ECU…" })).toBeEnabled();
    expect(screen.queryByText(/Start is disabled/)).not.toBeInTheDocument();
  });

  it("shows each blocking precondition as a live chip and disables Start with the reason", async () => {
    route(backupState());
    const blocked = readyStatus({
      vehicle_state: { ...readyStatus().vehicle_state!, state: "engine_running", rpm: 750 },
      passive: {
        ...readyStatus().passive!,
        enabled: true,
        source: "operator",
        ecm_session_owner: "foreign",
        ecm_session_claims: { foreign: 4.2 },
        foreign_tester: {
          tester_address: "F1",
          module_address: "10",
          last_heard_at: "2026-10-04T15:00:00Z",
          seconds_since_heard: 3,
        },
      },
      capture_profile: { active: true, name: "tuning" } as StatusResponse["capture_profile"],
    });
    renderCard({ status: blocked });
    await screen.findByText(/No backup has run/);
    const chips = within(screen.getByLabelText("Backup preconditions"));
    expect(chips.getByText("Vehicle: engine_running")).toBeInTheDocument();
    expect(chips.getByText("Passive: armed")).toBeInTheDocument();
    expect(chips.getByText("Foreign tester: holds the ECM")).toBeInTheDocument();
    expect(chips.getByText("Capture: tuning")).toBeInTheDocument();
    expect(document.querySelectorAll('[data-precondition="blocked"]')).toHaveLength(4);
    expect(screen.getByRole("button", { name: "Back up ECU…" })).toBeDisabled();
    expect(screen.getByText(/Start is disabled: The engine is running/)).toBeInTheDocument();
  });

  it("names the capture when it is the only blocker", async () => {
    route(backupState());
    renderCard({
      status: readyStatus({
        capture_profile: { active: true, name: "fuel-map" } as StatusResponse["capture_profile"],
      }),
    });
    expect(
      await screen.findByText(/Start is disabled: Capture 'fuel-map' is recording/),
    ).toBeInTheDocument();
  });

  it("treats an unreported /status as a blocker, not a pass", async () => {
    route(backupState());
    renderCard({ status: null });
    await screen.findByText(/No backup has run/);
    expect(document.querySelectorAll('[data-precondition="unknown"]')).toHaveLength(4);
    expect(screen.getByRole("button", { name: "Back up ECU…" })).toBeDisabled();
    expect(screen.getByText(/Waiting for \/status/)).toBeInTheDocument();
  });

  it("asks for an in-page confirmation, never window.confirm, then starts", async () => {
    const confirm = vi.spyOn(window, "confirm");
    route(backupState(), {
      "POST /control/rom/backup": backupState({ state: "running", blocks_total: 16645 }),
    });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: "Back up ECU…" }));
    const dialog = within(screen.getByRole("group", { name: "Confirm ECU backup" }));
    expect(dialog.getByText("It takes about 10 minutes.")).toBeInTheDocument();
    expect(dialog.getByText(/Key on, engine off for the whole read/)).toBeInTheDocument();
    expect(dialog.getByText("Read-only: nothing is written to the ECU.")).toBeInTheDocument();
    expect(control).not.toHaveBeenCalledWith(
      "/control/rom/backup",
      "t",
      expect.objectContaining({ method: "POST" }),
    );
    fireEvent.click(dialog.getByRole("button", { name: "Start backup" }));
    await waitFor(() =>
      expect(control).toHaveBeenCalledWith(
        "/control/rom/backup",
        "t",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(await screen.findByRole("progressbar", { name: "ECU backup" })).toBeInTheDocument();
    expect(confirm).not.toHaveBeenCalled();
  });

  it("'Not now' closes the confirmation without starting", async () => {
    route(backupState());
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: "Back up ECU…" }));
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(screen.queryByRole("group", { name: "Confirm ECU backup" })).not.toBeInTheDocument();
    expect(control).not.toHaveBeenCalledWith(
      "/control/rom/backup",
      "t",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("refused: shows the daemon's named reason from the job state", async () => {
    route(backupState({ state: "refused", reason: "passive mode is armed (operator)" }));
    renderCard();
    expect(await screen.findByText("Refused: passive mode is armed (operator)")).toBeVisible();
  });

  it("refused: an HTTP refusal of the start shows its reason verbatim", async () => {
    const detail =
      "vehicle_state is engine_running; the backup runs only key-on, engine-off (ignition_only)";
    route(backupState(), {
      "POST /control/rom/backup": new HttpError(409, JSON.stringify({ detail })),
    });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: "Back up ECU…" }));
    fireEvent.click(screen.getByRole("button", { name: "Start backup" }));
    expect(await screen.findByText(`Refused: ${detail}`)).toBeVisible();
  });

  it("running: blocks, percentage, ETA and retries; Cancel posts the cancel route", async () => {
    route(
      backupState({
        state: "running",
        blocks_done: 4161,
        blocks_total: 16645,
        bytes: 262143,
        elapsed_s: 156,
        eta_s: 468,
        retries: 2,
      }),
      { "POST /control/rom/backup/cancel": backupState({ state: "cancelled" }) },
    );
    renderCard();
    const bar = await screen.findByRole("progressbar", { name: "ECU backup" });
    expect(bar).toHaveAttribute("aria-valuenow", "4161");
    expect(bar).toHaveAttribute("aria-valuemax", "16645");
    expect(screen.getByText(/Block 4,161 of 16,645 · 25\.0 %/)).toBeInTheDocument();
    expect(screen.getByText(/ETA 07:48 · 2 retries/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Back up ECU…" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel backup" }));
    await waitFor(() =>
      expect(control).toHaveBeenCalledWith(
        "/control/rom/backup/cancel",
        "t",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("running with no rate yet reads 'estimating', not a made-up ETA", async () => {
    route(backupState({ state: "running", blocks_done: 0, blocks_total: 16645, eta_s: null }));
    renderCard();
    expect(await screen.findByText(/ETA estimating/)).toBeInTheDocument();
  });

  it("done: links the new library entry with its revision match", async () => {
    route(
      backupState({
        state: "done",
        blocks_done: 16645,
        blocks_total: 16645,
        bytes: 1048576,
        elapsed_s: 624,
        retries: 3,
        stored: true,
        sha256: SHA_TUNED,
      }),
    );
    const { onOpenEntry, onLibraryChanged } = renderCard();
    expect(await screen.findByText(/Backup complete: 1 MiB in 10:24, 3 retries/)).toBeVisible();
    expect(screen.getByText("ZS44A-20260920")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open Running tune" }));
    expect(onOpenEntry).toHaveBeenCalledWith(SHA_TUNED);
    expect(onLibraryChanged).toHaveBeenCalled();
  });

  it("done without a ROM map: complete, kept, and says what went unchecked", async () => {
    route(
      backupState({
        state: "done",
        reason: "unverified: no ROM map for this vehicle (rom_map.yaml): checksums not checked",
        bytes: 1048576,
        stored: true,
        sha256: SHA_TUNED,
        verified: false,
      }),
    );
    renderCard();
    expect(await screen.findByText(/Backup complete: 1 MiB/)).toBeVisible();
    expect(
      screen.getByText(
        "Kept, not fully checked: unverified: no ROM map for this vehicle (rom_map.yaml): checksums not checked",
      ),
    ).toBeVisible();
    expect(screen.queryByText(/Backup failed/)).not.toBeInTheDocument();
  });

  it("done with a map: shows the image's own size check once", async () => {
    const summary =
      "nothing points past 0x0FFFFF: 260 vector-table entries (tables at 0x000000, 0x020004) and 4,159 call targets land inside the image or in RAM";
    const extent = {
      verdict: "no_sign" as const,
      summary,
      image_size: 1048576,
      ram_from: "0xFFFF0000",
      tables: [],
      calls: null,
    };
    const integrity = {
      checksum: null,
      calibration_id: { image: "1ZS44A00000", ecu: "1ZS44A00000", ok: true },
      reread: { blocks: 64, mismatches: [] },
      extent,
    };
    route(
      backupState({ state: "done", stored: true, sha256: SHA_TUNED, verified: true, integrity }),
    );
    renderCard();
    expect(await screen.findByText(`Flash size: ${summary}`)).toBeVisible();
    cleanup();
    // Without a map the reason already carries the summary: not repeated.
    route(
      backupState({
        state: "done",
        stored: true,
        sha256: SHA_TUNED,
        verified: false,
        reason: `unverified: with no map, only the image's own pointers check its size: ${summary}; a sign, not proof`,
        integrity,
      }),
    );
    renderCard();
    expect(await screen.findByText(/Kept, not fully checked/)).toBeVisible();
    expect(screen.queryByText(/^Flash size:/)).not.toBeInTheDocument();
  });

  it("done before the library list has the entry: says so and asks for a refresh", async () => {
    route(backupState({ state: "done", stored: true, sha256: SHA_TUNED, bytes: 1048576 }));
    const { onLibraryChanged } = renderCard({ entries: [] });
    expect(await screen.findByText(/waiting for the library list/)).toBeInTheDocument();
    expect(onLibraryChanged).toHaveBeenCalled();
  });

  it("failed: shows the named reason", async () => {
    route(
      backupState({
        state: "failed",
        reason: "no response at 0x0A1B3F after 3 retries",
        blocks_done: 10400,
        blocks_total: 16645,
      }),
    );
    renderCard();
    expect(
      await screen.findByText("Backup failed: no response at 0x0A1B3F after 3 retries"),
    ).toBeVisible();
    expect(screen.queryByText(/The image was kept/)).not.toBeInTheDocument();
  });

  it("failed integrity: the kept image is linked as unverified", async () => {
    const unverified: RomLibraryEntry = {
      ...ENTRY_TUNED,
      revision_match: { revision: null, status: "unrecognised" },
      read_meta: { verified: false, unverified_reason: "checksum fails" },
    };
    route(
      backupState({
        state: "failed",
        reason: "unverified: checksum mismatch: stored sum word(s) differ from the recomputed",
        stored: true,
        sha256: SHA_TUNED,
        verified: false,
      }),
    );
    renderCard({ entries: [unverified] });
    expect(await screen.findByText(/The image was kept, marked unverified/)).toBeVisible();
    expect(screen.getByText("unverified")).toBeInTheDocument();
    expect(screen.getByText("unrecognised")).toBeInTheDocument();
  });

  it("failed store: nothing was kept, so nothing is linked", async () => {
    route(
      backupState({
        state: "failed",
        reason:
          "the read finished but the image could not be stored (OSError: [Errno 28] No space left on device); nothing was kept",
        stored: false,
        sha256: null,
        verified: true,
      }),
    );
    const { onLibraryChanged } = renderCard();
    expect(await screen.findByText(/nothing was kept/)).toBeVisible();
    expect(screen.queryByText(/The image was kept/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Open / })).not.toBeInTheDocument();
    expect(onLibraryChanged).not.toHaveBeenCalled();
  });

  it("a sha256 without stored is never shown as a kept image", async () => {
    route(backupState({ state: "failed", reason: "x", stored: false, sha256: SHA_TUNED }));
    renderCard();
    expect(await screen.findByText("Backup failed: x")).toBeVisible();
    expect(screen.queryByText(/The image was kept/)).not.toBeInTheDocument();
  });

  it("cancelled: says how far the read got", async () => {
    route(backupState({ state: "cancelled", blocks_done: 120, blocks_total: 16645 }));
    renderCard();
    expect(await screen.findByText(/Backup cancelled after 120 of 16,645 blocks/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Back up ECU…" })).toBeEnabled();
  });

  it("the daemon's own check blocks a chip /status shows as clear, with its reason", async () => {
    const detail =
      "another tester (K-line F1 -> 10) was heard 12 s ago; wait until it has been quiet 60 s";
    route(
      backupState({
        preconditions: [
          { id: "vehicle_state", ok: true, detail: "ignition_only (key on, engine off)" },
          { id: "foreign_tester", ok: false, detail },
          { id: "passive", ok: true, detail: "passive mode is off" },
          { id: "capture", ok: true, detail: "no capture active" },
        ],
        can_start: false,
        blocked_by: detail,
      }),
    );
    renderCard();
    expect(await screen.findByText("Foreign tester: refused by the daemon")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back up ECU…" })).toBeDisabled();
    expect(screen.getByText(`Start is disabled: ${detail}`)).toBeInTheDocument();
  });

  it("lists daemon refusals no chip covers, and they disable Start", async () => {
    const detail =
      "no fresh broadcast engine speed: OBD polling pauses for the read, so an engine start could not be seen";
    route(
      backupState({
        preconditions: [{ id: "engine_watch", ok: false, detail }],
        can_start: false,
        blocked_by: detail,
      }),
    );
    renderCard();
    const others = await screen.findByRole("list", { name: "Other refusals" });
    expect(within(others).getByText(detail)).toBeInTheDocument();
    expect(document.querySelectorAll('[data-precondition="blocked"]')).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Back up ECU…" })).toBeDisabled();
  });

  it("running: shows the phase the daemon reports", async () => {
    route(
      backupState({ state: "running", phase: "reading", blocks_done: 10, blocks_total: 16645 }),
    );
    renderCard();
    expect(await screen.findByText("reading")).toBeInTheDocument();
  });

  it("done: reports negative responses and when polling resumed", async () => {
    route(
      backupState({
        state: "done",
        stored: true,
        sha256: SHA_TUNED,
        bytes: 1048576,
        elapsed_s: 630,
        retries: 1,
        negative_responses: { "0x78": 2 },
        polling_resumed_s: 4.2,
      }),
    );
    renderCard();
    expect(await screen.findByText("Negative responses: 0x78 ×2")).toBeInTheDocument();
    expect(screen.getByText("Normal polling resumed 4 s after the read.")).toBeInTheDocument();
  });

  it("done before the list has the entry: shows the job's own revision match", async () => {
    route(
      backupState({
        state: "done",
        stored: true,
        sha256: SHA_TUNED,
        revision_match: { revision: "ZS44A-20260920", status: "matched" },
      }),
    );
    renderCard({ entries: [] });
    expect(await screen.findByText("ZS44A-20260920")).toBeInTheDocument();
  });

  it("without a token asks for one and never polls", () => {
    route(backupState());
    renderCard({ token: "" });
    expect(
      screen.getAllByText("Locked — needs the control token to back up the ECU.").length,
    ).toBeGreaterThan(0);
    expect(control).not.toHaveBeenCalled();
  });
});
