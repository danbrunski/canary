import { useCallback, useEffect, useState } from "react";

import {
  getControlVersion,
  getCurrentCapture,
  getPreflight,
  getSequenceDetector,
  listJobs,
  startCapture,
  stopCapture,
  tagCapture,
} from "../api/http";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { useAsyncAction } from "../hooks/useAsyncAction";
import type { CompanionSettings } from "../hooks/useCompanionSettings";
import { useControlStream } from "../hooks/useControlStream";
import { describeError } from "../hooks/useControlResource";
import type {
  CaptureState,
  JobRecord,
  PreflightResponse,
  SequenceDetectorStatus,
  VersionResponse,
} from "../types";
import { buttonClass, cn, inputClass } from "../styles";
import { formatTimeOfDay } from "../util/time";
import { CompanionConfigCard } from "./CompanionConfigCard";
import { ControlEventsCard, PreflightCard } from "./ControlPanelSections";
import { JobsCard } from "./JobsCard";
import { ModelUploadCard } from "./ModelUploadCard";
import { SequenceDetectorCard } from "./SequenceDetectorCard";

const TOKEN_GATE = "Enter the control token first.";

interface ControlPanelProps {
  token: string;
  companion: CompanionSettings;
}

/** Capture, jobs, detector switch, artifact upload, preflight, companion
 *  config (Task 3.1 — session operations moved to `SessionsPanel`). Token
 *  entry itself now lives in the shell header (Task 5.1); this panel only
 *  reads `token` to gate its own actions. */
export function ControlPanel({ token, companion }: ControlPanelProps) {
  const controlStream = useControlStream(token);
  // One snapshot, written by one setter: every read replaces all five.
  const [control, setControl] = useState<ControlSnapshot | null>(null);
  const preflight = control?.preflight ?? null;
  const version = control?.version ?? null;
  const capture = control?.capture ?? null;
  const jobs = control?.jobs ?? NO_JOBS;
  const sequenceDetector = control?.sequenceDetector ?? null;
  const { notice, setNotice, busy, run: runAction } = useAsyncAction();
  const [purpose, setPurpose] = useState("");
  const [protocol, setProtocol] = useState("drive");
  const [vehicleState, setVehicleState] = useState("driving");
  const [tagLabel, setTagLabel] = useState("");
  const [stopReason, setStopReason] = useState("");

  const refreshControl = useCallback(async () => {
    if (!token) return;
    setControl(await readControl(token));
  }, [token]);

  // Clearing the token clears what it read: on the change, while rendering,
  // rather than in the effect a commit later.
  const [readToken, setReadToken] = useState(token);
  if (token !== readToken) {
    setReadToken(token);
    if (!token) setControl(null);
  }

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    // The first read reports its failure; the 5 s re-reads stay quiet.
    const load = async (report: boolean): Promise<void> => {
      try {
        const next = await readControl(token);
        if (!cancelled) setControl(next);
      } catch (err) {
        if (!cancelled && report) setNotice({ kind: "error", text: errorText(err) });
      }
    };
    void load(true);
    const timer = window.setInterval(() => void load(false), 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [setNotice, token]);

  // Every mutating control gates on the token and refreshes afterwards;
  // wrapping the shared runner here keeps all call sites (including the
  // extracted cards, which receive this as a prop) a bare
  // `run(label, action)`.
  const run = useCallback(
    (label: string, action: () => Promise<void>) =>
      runAction(label, action, {
        precheck: () => (token ? null : TOKEN_GATE),
        then: refreshControl,
      }),
    [runAction, token, refreshControl],
  );

  const start = () =>
    run("capture start", async () => {
      await startCapture(token, {
        purpose: purpose || null,
        protocol: protocol || null,
        vehicle_state: vehicleState || null,
        fields: {},
      });
    });

  const addTag = () =>
    run("tag", async () => {
      await tagCapture(token, {
        label: tagLabel.trim(),
        kind: "event",
        fields: {},
      });
      setTagLabel("");
    });

  const stop = () =>
    run("capture stop", async () => {
      await stopCapture(token, { reason: stopReason || null, fields: {} });
      setStopReason("");
    });

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3 rounded-[8px] border border-border bg-elev p-3">
        <button
          className={buttonClass}
          type="button"
          onClick={() => refreshControl()}
          disabled={!token || !!busy}
        >
          Refresh
        </button>
        {version && (
          <div className="font-num text-[0.82rem] tabular-nums text-fg-dim">
            API v{version.api_version}
            {version.git_sha ? ` / ${version.git_sha}` : ""}
          </div>
        )}
        {!token && (
          <span className="text-[0.78rem] text-fg-dim">
            Control token: use the lock icon in the header.
          </span>
        )}
      </div>

      <div
        className={cn(
          "flex items-center gap-2 rounded-[8px] border px-3 py-1.5 text-[0.8rem]",
          token ? "border-ok/45 bg-ok/10 text-ok" : "border-border-soft bg-elev text-fg-dim",
        )}
      >
        <span
          className={cn("inline-block h-2 w-2 rounded-full", token ? "bg-ok" : "bg-fg-faint")}
        />
        {token
          ? "Control unlocked — mutating actions enabled."
          : "Read-only — enter the control token to enable captures, jobs, sessions and uploads."}
      </div>

      <CompanionConfigCard companion={companion} />

      <Notice notice={notice} />
      <Notice notice={busy ? { kind: "busy", text: `Working: ${busy}` } : null} />

      <div className="grid grid-cols-3 gap-4 max-[1100px]:grid-cols-1">
        <PreflightCard hasToken={Boolean(token)} preflight={preflight} />

        <Panel
          title="Capture"
          actions={
            <span className={capture ? "text-[0.8rem] text-ok" : "text-fg-dim"}>
              {capture ? "active" : "idle"}
            </span>
          }
        >
          <div className="grid grid-cols-4 gap-2 max-[1100px]:grid-cols-1">
            <input
              className={inputClass}
              value={purpose}
              onChange={(event) => setPurpose(event.target.value)}
              placeholder="purpose"
            />
            <input
              className={inputClass}
              value={protocol}
              onChange={(event) => setProtocol(event.target.value)}
              placeholder="protocol"
            />
            <input
              className={inputClass}
              value={vehicleState}
              onChange={(event) => setVehicleState(event.target.value)}
              placeholder="vehicle state"
            />
            <button
              className={buttonClass}
              type="button"
              onClick={start}
              disabled={!token || !!capture || !!busy}
            >
              Start
            </button>
          </div>
          {capture && (
            <div className="mt-3 grid grid-cols-[1fr_auto_auto] gap-3 rounded-[8px] border border-ok/35 bg-ok/10 p-2.5 text-[0.85rem] max-[1100px]:grid-cols-1">
              <b>{capture.purpose || capture.protocol || "capture"}</b>
              <span className="text-fg-dim">{capture.session_id}</span>
              <time className="font-num text-fg-dim">{formatTimeOfDay(capture.started_at)}</time>
            </div>
          )}
          <div className="mt-3 grid grid-cols-[1fr_auto_1fr_auto] gap-2 max-[1100px]:grid-cols-1">
            <input
              className={inputClass}
              value={tagLabel}
              onChange={(event) => setTagLabel(event.target.value)}
              placeholder="tag label"
            />
            <button
              className={buttonClass}
              type="button"
              onClick={addTag}
              disabled={!token || !capture || !tagLabel}
            >
              Tag
            </button>
            <input
              className={inputClass}
              value={stopReason}
              onChange={(event) => setStopReason(event.target.value)}
              placeholder="stop reason"
            />
            <button
              className={buttonClass}
              type="button"
              onClick={stop}
              disabled={!token || !capture || !!busy}
            >
              Stop
            </button>
          </div>
        </Panel>

        <JobsCard token={token} busy={busy} run={run} jobs={jobs} />
        <SequenceDetectorCard
          token={token}
          busy={busy}
          run={run}
          sequenceDetector={sequenceDetector}
        />
        <ModelUploadCard token={token} busy={busy} run={run} />

        <ControlEventsCard connection={controlStream.connection} events={controlStream.events} />
      </div>
    </section>
  );
}

// Status-aware messaging (Task 6.14): 401/403 → token problem, 503 →
// feature not wired, etc. — never a raw stack-ish string.
const errorText = describeError;

const NO_JOBS: JobRecord[] = [];

interface ControlSnapshot {
  preflight: PreflightResponse;
  version: VersionResponse;
  capture: CaptureState | null;
  jobs: JobRecord[];
  sequenceDetector: SequenceDetectorStatus | null;
}

async function readControl(token: string): Promise<ControlSnapshot> {
  const [preflight, version, capture, jobs, sequence] = await Promise.all([
    getPreflight(token),
    getControlVersion(token),
    getCurrentCapture(token),
    listJobs(token),
    getSequenceDetector(token),
  ]);
  return {
    preflight,
    version,
    capture: capture.capture,
    jobs: jobs.jobs.slice().reverse(),
    sequenceDetector: sequence.sequence_detector,
  };
}
