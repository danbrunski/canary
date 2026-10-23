import { useCallback, useEffect, useState } from "react";

import { cancelRomBackup, getRomBackup, startRomBackup } from "../../api/workbench";
import { Notice } from "../../components/Notice";
import { Panel } from "../../components/Panel";
import { useControlResource } from "../../hooks/useControlResource";
import {
  backupPreconditions,
  backupStartBlocker,
  entryTitle,
  formatBytes,
  otherBackupRefusals,
  refusalText,
} from "../../lib/romLibrary";
import { buttonClass, dangerButtonClass, type NoticeState } from "../../styles";
import type { StatusResponse } from "../../types";
import type { RomBackupState, RomLibraryEntry } from "../../types/romLibrary";
import { formatClockDuration } from "../../util/time";
import { ChecksumChip, PreconditionChip, RevisionChip, UnverifiedChip } from "./romLibraryParts";

/** Poll fast enough for a live block counter while a read runs; slowly
 *  otherwise, so a backup started elsewhere still shows up. */
const RUNNING_POLL_MS = 1000;
const IDLE_POLL_MS = 5000;

/** The library sha of the read's image, only when it was stored: a read that
 *  finished but could not be stored has nothing to link to. */
function storedSha(job: RomBackupState): string | null {
  return job.stored === true && job.sha256 ? job.sha256 : null;
}

function refused(reason: string): RomBackupState {
  return {
    state: "refused",
    reason,
    blocks_done: 0,
    blocks_total: 0,
    bytes: 0,
    elapsed_s: 0,
    eta_s: null,
    retries: 0,
  };
}

/** Back up the ECU's flash into the library (W08.8): the W08.7 refusals as
 *  live chips, an in-page confirmation, progress with Cancel, and the result
 *  linked to its library entry. Read-only: nothing is written to the ECU. */
export function EcuBackupCard({
  token,
  status,
  entries,
  onOpenEntry,
  onLibraryChanged,
}: {
  token: string;
  status: StatusResponse | null;
  entries: RomLibraryEntry[];
  onOpenEntry: (sha: string) => void;
  onLibraryChanged: () => void;
}) {
  // The last POST's answer. It stands in for the poll only while the poll
  // still reads idle: a refusal the daemon does not keep must stay visible.
  const [posted, setPosted] = useState<RomBackupState | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<NoticeState | null>(null);
  const [fast, setFast] = useState(false);
  const polled = useControlResource(
    useCallback((signal: AbortSignal) => getRomBackup(token, signal), [token]),
    { enabled: Boolean(token), intervalMs: fast ? RUNNING_POLL_MS : IDLE_POLL_MS },
  );
  const job = posted && (!polled.data || polled.data.state === "idle") ? posted : polled.data;
  const running = job?.state === "running";
  if (running !== fast) setFast(running);

  // A finished read (or a failed one whose image was kept) adds a library
  // entry; show it without waiting for the operator to reload. Only a stored
  // image: a read that could not be stored has no entry to show.
  const resultSha = job && (job.state === "done" || job.state === "failed") ? storedSha(job) : null;
  useEffect(() => {
    if (resultSha) onLibraryChanged();
  }, [resultSha, onLibraryChanged]);

  // The daemon's own checks ride on the poll (absent while running); the
  // chips merge them so none reads green where Start would be refused.
  const checks = polled.data?.preconditions ?? null;
  const preconditions = backupPreconditions(status, checks);
  const others = otherBackupRefusals(checks);
  const blocker = backupStartBlocker(
    token,
    status,
    running ? { state: "running" } : (polled.data ?? null),
  );

  async function start() {
    setBusy(true);
    setNotice(null);
    try {
      setPosted(await startRomBackup(token));
    } catch (err) {
      setPosted(refused(refusalText(err)));
    } finally {
      setBusy(false);
      setConfirming(false);
      polled.refresh();
    }
  }

  async function cancel() {
    setBusy(true);
    setNotice(null);
    try {
      setPosted(await cancelRomBackup(token));
    } catch (err) {
      setNotice({ kind: "error", text: refusalText(err) });
    } finally {
      setBusy(false);
      polled.refresh();
    }
  }

  return (
    <Panel title="Back up ECU" count="Read-only">
      <div id="ecu-backup" className="space-y-3">
        <p className="text-sm text-fg-dim">
          Reads the ECU&apos;s whole flash over the diagnostic bus and stores the plain image in the
          library as an ECU read. Nothing is written to the ECU.
        </p>
        {!token && <p>Enter the control token to back up the ECU.</p>}
        {token && polled.error && !job && <Notice notice={{ kind: "error", text: polled.error }} />}
        <Notice notice={notice} />
        {job && (
          <BackupState
            job={job}
            entries={entries}
            busy={busy}
            onCancel={() => void cancel()}
            onOpenEntry={onOpenEntry}
          />
        )}
        <div className="flex flex-wrap gap-2" aria-label="Backup preconditions">
          {preconditions.map((item) => (
            <PreconditionChip
              key={item.key}
              label={item.label}
              value={item.value}
              state={item.state}
            />
          ))}
        </div>
        {!running && others.length > 0 && (
          <ul aria-label="Other refusals" className="list-disc space-y-1 pl-5 text-sm text-warn">
            {others.map((check) => (
              <li key={check.id}>{check.detail}</li>
            ))}
          </ul>
        )}
        {!running && (
          <>
            {confirming ? (
              <div
                role="group"
                aria-label="Confirm ECU backup"
                className="space-y-2 rounded-lg border border-accent/55 p-3 text-sm"
              >
                <p className="font-semibold">Back up the ECU now?</p>
                <ul className="list-disc space-y-1 pl-5">
                  <li>It takes about 10 minutes.</li>
                  <li>
                    Key on, engine off for the whole read. If the engine starts, the read aborts.
                  </li>
                  <li>CANary&apos;s own polling pauses until the read ends.</li>
                  <li>Read-only: nothing is written to the ECU.</li>
                </ul>
                {blocker && <p className="text-warn">Start is disabled: {blocker}</p>}
                <div className="flex flex-wrap gap-2">
                  <button
                    className={buttonClass}
                    disabled={!!blocker || busy}
                    onClick={() => void start()}
                  >
                    Start backup
                  </button>
                  <button className={buttonClass} onClick={() => setConfirming(false)}>
                    Not now
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex flex-wrap items-center gap-3">
                <button
                  className={buttonClass}
                  disabled={!!blocker || busy}
                  onClick={() => {
                    setPosted(null);
                    setConfirming(true);
                  }}
                >
                  Back up ECU…
                </button>
                {blocker && (
                  <span role="status" className="text-sm text-warn">
                    Start is disabled: {blocker}
                  </span>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </Panel>
  );
}

function BackupState({
  job,
  entries,
  busy,
  onCancel,
  onOpenEntry,
}: {
  job: RomBackupState;
  entries: RomLibraryEntry[];
  busy: boolean;
  onCancel: () => void;
  onOpenEntry: (sha: string) => void;
}) {
  const kept = storedSha(job);
  const entry = kept ? entries.find((item) => item.sha256 === kept) : undefined;
  const nrcs = Object.entries(job.negative_responses ?? {});
  const details = (
    <>
      {nrcs.length > 0 && (
        <p className="font-num text-xs text-fg-dim">
          Negative responses: {nrcs.map(([code, count]) => `${code} ×${count}`).join(", ")}
        </p>
      )}
      {(job.polling_resume_note || job.polling_resumed_s != null) && (
        <p className="text-xs text-fg-dim">
          {job.polling_resume_note ??
            `Normal polling resumed ${Math.round(job.polling_resumed_s ?? 0)} s after the read.`}
        </p>
      )}
    </>
  );
  const link = kept ? (
    entry ? (
      <span className="inline-flex flex-wrap items-center gap-2">
        <button className={buttonClass} onClick={() => onOpenEntry(entry.sha256)}>
          Open {entryTitle(entry)}
        </button>
        <RevisionChip match={entry.revision_match} />
        <ChecksumChip checksum={entry.checksum} />
        <UnverifiedChip entry={entry} />
      </span>
    ) : (
      <span className="inline-flex flex-wrap items-center gap-2 font-num text-xs">
        sha256 {kept.slice(0, 12)}… (waiting for the library list)
        {job.revision_match && <RevisionChip match={job.revision_match} />}
      </span>
    )
  ) : null;
  const percent = job.blocks_total > 0 ? (job.blocks_done / job.blocks_total) * 100 : 0;
  switch (job.state) {
    case "idle":
      return <p className="text-sm text-fg-dim">No backup has run since the daemon started.</p>;
    case "refused":
      return (
        <Notice notice={{ kind: "error", text: `Refused: ${job.reason ?? "no reason given."}` }} />
      );
    case "running":
      return (
        <div className="space-y-2" aria-label="Backup progress">
          <div
            role="progressbar"
            aria-label="ECU backup"
            aria-valuemin={0}
            aria-valuemax={job.blocks_total}
            aria-valuenow={job.blocks_done}
            aria-valuetext={`${percent.toFixed(1)} %`}
            className="h-2 overflow-hidden rounded bg-elev-3"
          >
            <div className="h-full bg-accent" style={{ width: `${percent}%` }} />
          </div>
          {job.phase && <p className="text-sm text-fg-dim">{job.phase}</p>}
          <p className="font-num text-sm">
            Block {job.blocks_done.toLocaleString()} of {job.blocks_total.toLocaleString()} ·{" "}
            {percent.toFixed(1)} % · {formatBytes(job.bytes)} read · elapsed{" "}
            {formatClockDuration(job.elapsed_s)} · ETA{" "}
            {formatClockDuration(job.eta_s, { missing: "estimating" })} · {job.retries} retries
          </p>
          <button className={dangerButtonClass} disabled={busy} onClick={onCancel}>
            Cancel backup
          </button>
        </div>
      );
    case "done":
      return (
        <div className="space-y-2 text-sm">
          <Notice
            notice={{
              kind: "ok",
              text: `Backup complete: ${formatBytes(job.bytes)} in ${formatClockDuration(job.elapsed_s)}, ${job.retries} retries.`,
            }}
          />
          {/* Done but unverified: a check had nothing to check against (no
              ROM map yet, no ECM-reported calibration ID). */}
          {job.verified === false && job.reason && (
            <p className="text-warn">Kept, not fully checked: {job.reason}</p>
          )}
          {link}
          {details}
        </div>
      );
    case "failed":
      return (
        <div className="space-y-2 text-sm">
          <Notice
            notice={{ kind: "error", text: `Backup failed: ${job.reason ?? "no reason given."}` }}
          />
          {link && (
            <p className="flex flex-wrap items-center gap-2">
              The image was kept, marked unverified: {link}
            </p>
          )}
          {details}
        </div>
      );
    case "cancelled":
      return (
        <p className="text-sm">
          Backup cancelled after {job.blocks_done.toLocaleString()} of{" "}
          {job.blocks_total.toLocaleString()} blocks{job.reason ? `: ${job.reason}` : "."}
        </p>
      );
    default:
      return <p className="text-sm text-fg-dim">Backup state: {String(job.state)}</p>;
  }
}
