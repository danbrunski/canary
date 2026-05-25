import { useCallback, useEffect, useMemo, useState } from "react";

import {
  cancelJob,
  deleteSession,
  downloadSession,
  getControlVersion,
  getCurrentCapture,
  getJobLogs,
  getPreflight,
  getSequenceDetector,
  listJobs,
  listSessions,
  pinSession,
  startCapture,
  stopCapture,
  submitJob,
  switchSequenceDetector,
  syncSessionToCompanion,
  tagCapture,
  unpinSession,
  updateSessionMetadata,
  uploadModelArtifact,
} from "../api/http";
import { Notice } from "../components/Notice";
import { useAsyncAction } from "../hooks/useAsyncAction";
import type { CompanionSettings } from "../hooks/useCompanionSettings";
import { useControlStream } from "../hooks/useControlStream";
import { describeError } from "../hooks/useControlResource";
import type {
  CaptureState,
  JobRecord,
  PreflightResponse,
  SequenceDetectorStatus,
  SessionSummary,
  VersionResponse,
} from "../types";
import {
  buttonClass,
  cn,
  dangerButtonClass,
  inputClass,
  panelClass,
  panelCountClass,
  panelHeaderClass,
  panelTitleClass,
  selectClass,
} from "../styles";
import { formatTimeOfDay } from "../util/time";
import { JOB_KINDS } from "../lib/jobKinds";
import { ControlEventsCard, PreflightCard } from "./ControlPanelSections";

const TOKEN_GATE = "Enter the control token first.";

interface ControlPanelProps {
  token: string;
  onTokenChange: (token: string) => void;
  companion: CompanionSettings;
}


export function ControlPanel({ token, onTokenChange, companion }: ControlPanelProps) {
  const controlStream = useControlStream(token);
  const [preflight, setPreflight] = useState<PreflightResponse | null>(null);
  const [version, setVersion] = useState<VersionResponse | null>(null);
  const [capture, setCapture] = useState<CaptureState | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [sequenceDetector, setSequenceDetector] = useState<SequenceDetectorStatus | null>(null);
  const { notice, setNotice, busy, run: runAction } = useAsyncAction();
  const [purpose, setPurpose] = useState("");
  const [protocol, setProtocol] = useState("drive");
  const [vehicleState, setVehicleState] = useState("driving");
  const [tagLabel, setTagLabel] = useState("");
  const [stopReason, setStopReason] = useState("");
  const [selectedSessionId, setSelectedSessionId] = useState("");
  const [metadataText, setMetadataText] = useState("{}");
  const [pinReason, setPinReason] = useState("");
  const [jobKind, setJobKind] = useState("corpus_report");
  const [jobDays, setJobDays] = useState("14");
  const [useAllSessions, setUseAllSessions] = useState(false);
  const [jobLog, setJobLog] = useState("");
  const [modelType, setModelType] = useState("isolation_forest");
  const [artifactFile, setArtifactFile] = useState<File | null>(null);
  const [manifestFile, setManifestFile] = useState<File | null>(null);
  const [syncPin, setSyncPin] = useState(true);

  const selectedSession = useMemo(
    () => sessions.find((session) => session.session_id === selectedSessionId) ?? null,
    [selectedSessionId, sessions],
  );

  const refreshControl = useCallback(async () => {
    if (!token) return;
    const [nextPreflight, nextVersion, nextCapture, nextSessions, nextJobs, nextSequence] =
      await Promise.all([
        getPreflight(token),
        getControlVersion(token),
        getCurrentCapture(token),
        listSessions(token),
        listJobs(token),
        getSequenceDetector(token),
      ]);
    setPreflight(nextPreflight);
    setVersion(nextVersion);
    setCapture(nextCapture.capture);
    setSessions(nextSessions.sessions);
    setJobs(nextJobs.jobs.slice().reverse());
    setSequenceDetector(nextSequence.sequence_detector);
    setSelectedSessionId((current) => {
      if (current && nextSessions.sessions.some((s) => s.session_id === current)) {
        return current;
      }
      return nextSessions.sessions.at(-1)?.session_id ?? "";
    });
  }, [token]);

  useEffect(() => {
    if (!token) {
      setPreflight(null);
      setVersion(null);
      setCapture(null);
      setSessions([]);
      setJobs([]);
      setSequenceDetector(null);
      return;
    }
    const controller = new AbortController();
    refreshControl().catch((err: unknown) => {
      setNotice({ kind: "error", text: errorText(err) });
    });
    const timer = window.setInterval(() => {
      refreshControl().catch(() => undefined);
    }, 5000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [refreshControl, token]);

  // Every mutating control gates on the token and refreshes afterwards;
  // wrapping the shared runner here keeps all call sites a bare
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

  const saveMetadata = () =>
    run("metadata update", async () => {
      if (!selectedSessionId) throw new Error("Select a session first.");
      await updateSessionMetadata(
        token,
        selectedSessionId,
        parseJsonObject(metadataText),
        true,
      );
    });

  const pinSelected = () =>
    run("session pin", async () => {
      if (!selectedSessionId) throw new Error("Select a session first.");
      await pinSession(token, selectedSessionId, pinReason || undefined);
    });

  const unpinSelected = () =>
    run("session unpin", async () => {
      if (!selectedSessionId) throw new Error("Select a session first.");
      await unpinSession(token, selectedSessionId);
    });

  const deleteSelected = () =>
    run("session delete", async () => {
      if (!selectedSessionId) throw new Error("Select a session first.");
      if (!window.confirm(`Delete session ${selectedSessionId}?`)) return;
      await deleteSession(token, selectedSessionId, selectedSession?.pinned ?? false);
    });

  const exportSelected = () =>
    run("session export", async () => {
      if (!selectedSessionId) throw new Error("Select a session first.");
      const blob = await downloadSession(token, selectedSessionId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${selectedSessionId}.zip`;
      anchor.click();
      URL.revokeObjectURL(url);
    });

  const syncSelectedToLaptop = () =>
    run("session sync", async () => {
      if (!selectedSessionId) throw new Error("Select a session first.");
      if (!companion.baseUrl) throw new Error("Enter the companion API URL.");
      await syncSessionToCompanion(companion.baseUrl, companion.token, selectedSessionId, {
        pi_host: currentPiHost(),
        token: token || null,
        pin: syncPin,
        index_with_config: true,
      });
    });

  const startJob = () =>
    run("job submit", async () => {
      const params: Record<string, unknown> = {};
      if (useAllSessions) {
        params.all = true;
      } else if (jobDays.trim()) {
        params.days = Number(jobDays);
      }
      await submitJob(token, jobKind, params);
    });

  const showLogs = (jobId: string) =>
    run("job logs", async () => {
      const logs = await getJobLogs(token, jobId);
      setJobLog(logs.log);
    });

  const switchBackbone = (backbone: "lstm" | "tcn") =>
    run(`sequence switch → ${backbone}`, async () => {
      const next = await switchSequenceDetector(token, backbone);
      setSequenceDetector(next.sequence_detector);
    });

  const uploadModel = () =>
    run("model upload", async () => {
      if (!artifactFile || !manifestFile) {
        throw new Error("Select both an artifact and manifest file.");
      }
      const artifactB64 = await fileToBase64(artifactFile);
      const manifest = parseJsonObject(await manifestFile.text());
      await uploadModelArtifact(token, modelType, artifactB64, manifest);
    });

  return (
    <section className="flex flex-col gap-3">
      <div className="grid grid-cols-[minmax(18rem,1fr)_auto_auto] items-end gap-3 rounded-[8px] border border-border bg-elev p-3 max-[1100px]:grid-cols-1">
        <label className="flex flex-col gap-1 text-[0.75rem] text-fg-dim">
          Control token
          <input
            className={inputClass}
            type="password"
            value={token}
            onChange={(event) => onTokenChange(event.target.value)}
            placeholder="CANARY_CONTROL_TOKEN"
            autoComplete="off"
          />
        </label>
        <button className={buttonClass} type="button" onClick={() => refreshControl()} disabled={!token || !!busy}>
          Refresh
        </button>
        {version && (
          <div className="font-num text-[0.82rem] tabular-nums text-fg-dim">
            API v{version.api_version}
            {version.git_sha ? ` / ${version.git_sha}` : ""}
          </div>
        )}
      </div>

      <div
        className={cn(
          "flex items-center gap-2 rounded-[8px] border px-3 py-1.5 text-[0.8rem]",
          token
            ? "border-ok/45 bg-ok/10 text-ok"
            : "border-border-soft bg-elev text-fg-dim",
        )}
      >
        <span
          className={cn(
            "inline-block h-2 w-2 rounded-full",
            token ? "bg-ok" : "bg-fg-faint",
          )}
        />
        {token
          ? "Control unlocked — mutating actions enabled."
          : "Read-only — enter the control token to enable captures, jobs, sessions and uploads."}
      </div>

      <div className="grid grid-cols-[minmax(18rem,1fr)_minmax(12rem,0.45fr)] items-end gap-3 rounded-[8px] border border-border bg-elev p-3 max-[1100px]:grid-cols-1">
        <label className="flex flex-col gap-1 text-[0.75rem] text-fg-dim">
          Companion API
          <input
            className={inputClass}
            value={companion.baseUrl}
            onChange={(event) => companion.setBaseUrl(event.target.value)}
            placeholder="http://127.0.0.1:8765"
          />
        </label>
        <label className="flex flex-col gap-1 text-[0.75rem] text-fg-dim">
          Local token
          <input
            className={inputClass}
            type="password"
            value={companion.token}
            onChange={(event) => companion.setToken(event.target.value)}
            placeholder="optional"
            autoComplete="off"
          />
        </label>
      </div>

      <Notice notice={notice} />
      <Notice notice={busy ? { kind: "busy", text: `Working: ${busy}` } : null} />

      <div className="grid grid-cols-3 gap-4 max-[1100px]:grid-cols-1">
        <PreflightCard hasToken={Boolean(token)} preflight={preflight} />

        <section className={panelClass}>
          <header className={panelHeaderClass}>
            <h2 className={panelTitleClass}>Capture</h2>
            <span className={capture ? "text-[0.8rem] text-ok" : "text-fg-dim"}>
              {capture ? "active" : "idle"}
            </span>
          </header>
          <div className="grid grid-cols-4 gap-2 max-[1100px]:grid-cols-1">
            <input className={inputClass} value={purpose} onChange={(event) => setPurpose(event.target.value)} placeholder="purpose" />
            <input className={inputClass} value={protocol} onChange={(event) => setProtocol(event.target.value)} placeholder="protocol" />
            <input className={inputClass} value={vehicleState} onChange={(event) => setVehicleState(event.target.value)} placeholder="vehicle state" />
            <button className={buttonClass} type="button" onClick={start} disabled={!token || !!capture || !!busy}>
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
            <input className={inputClass} value={tagLabel} onChange={(event) => setTagLabel(event.target.value)} placeholder="tag label" />
            <button className={buttonClass} type="button" onClick={addTag} disabled={!token || !capture || !tagLabel}>
              Tag
            </button>
            <input className={inputClass} value={stopReason} onChange={(event) => setStopReason(event.target.value)} placeholder="stop reason" />
            <button className={buttonClass} type="button" onClick={stop} disabled={!token || !capture || !!busy}>
              Stop
            </button>
          </div>
        </section>

        <section className={cn(panelClass, "col-span-2 max-[1100px]:col-span-1")}>
          <header className={panelHeaderClass}>
            <h2 className={panelTitleClass}>Sessions</h2>
            <span className={panelCountClass}>{sessions.length}</span>
          </header>
          <div className="flex max-h-80 flex-col gap-1.5 overflow-y-auto">
            {sessions.slice().reverse().map((session) => (
              <button
                key={session.session_id}
                type="button"
                className={cn(
                  "grid w-full grid-cols-[1fr_auto] gap-2 rounded-[8px] border border-border bg-elev-2 px-2.5 py-2 text-left text-fg transition-colors hover:border-border-soft max-[1100px]:grid-cols-1",
                  session.session_id === selectedSessionId && "border-accent",
                )}
                onClick={() => setSelectedSessionId(session.session_id)}
              >
                <span>{session.session_id}</span>
                <small className="text-fg-dim">
                  {session.telemetry_chunks} chunks / {session.tag_count} tags
                  {session.pinned ? " / pinned" : ""}
                </small>
              </button>
            ))}
          </div>
          {selectedSession && (
            <div className="mt-3 flex flex-col gap-2">
              <textarea
                className={cn(inputClass, "min-h-20 w-full resize-y font-num text-[0.8rem]")}
                value={metadataText}
                onChange={(event) => setMetadataText(event.target.value)}
                rows={3}
                spellCheck={false}
              />
              <div className="flex flex-wrap gap-1.5">
                <button className={buttonClass} type="button" onClick={saveMetadata}>Metadata</button>
                <button className={buttonClass} type="button" onClick={exportSelected}>Export</button>
                <button className={buttonClass} type="button" onClick={syncSelectedToLaptop}>Sync to laptop</button>
                <button className={buttonClass} type="button" onClick={pinSelected}>Pin</button>
                <button className={buttonClass} type="button" onClick={unpinSelected}>Unpin</button>
                <button type="button" className={dangerButtonClass} onClick={deleteSelected}>
                  Delete
                </button>
              </div>
              <label className="flex min-h-9 flex-row items-center gap-2 text-[0.75rem] text-fg-dim">
                <input
                  type="checkbox"
                  checked={syncPin}
                  onChange={(event) => setSyncPin(event.target.checked)}
                />
                pin on Pi during sync
              </label>
              <input className={inputClass} value={pinReason} onChange={(event) => setPinReason(event.target.value)} placeholder="pin reason" />
            </div>
          )}
        </section>

        <section className={cn(panelClass, "col-span-2 max-[1100px]:col-span-1")}>
          <header className={panelHeaderClass}>
            <h2 className={panelTitleClass}>Jobs</h2>
            <span className={panelCountClass}>{jobs.length}</span>
          </header>
          <div className="mt-3 grid grid-cols-[1fr_auto_1fr_auto] gap-2 max-[1100px]:grid-cols-1">
            <select className={selectClass} value={jobKind} onChange={(event) => setJobKind(event.target.value)}>
              {JOB_KINDS.map((kind) => (
                <option key={kind} value={kind}>{kind}</option>
              ))}
            </select>
            <input className={inputClass} value={jobDays} onChange={(event) => setJobDays(event.target.value)} disabled={useAllSessions} inputMode="numeric" placeholder="days" />
            <label className="flex min-h-9 flex-row items-center justify-center gap-2 text-[0.75rem] text-fg-dim">
              <input
                type="checkbox"
                checked={useAllSessions}
                onChange={(event) => setUseAllSessions(event.target.checked)}
              />
              all
            </label>
            <button className={buttonClass} type="button" onClick={startJob} disabled={!token || !!busy}>
              Start
            </button>
          </div>
          <div className="mt-3 flex max-h-80 flex-col gap-1.5 overflow-y-auto">
            {jobs.map((job) => (
              <div key={job.job_id} className={cn(jobRowClass(job.status))}>
                <div className="flex min-w-0 flex-col">
                  <b>{job.kind}</b>
                  <small className="text-fg-dim">{job.status} / {job.job_id.slice(0, 8)}</small>
                </div>
                <div className="flex flex-wrap justify-end gap-1.5">
                  <button className={buttonClass} type="button" onClick={() => showLogs(job.job_id)}>Logs</button>
                  <button
                    className={buttonClass}
                    type="button"
                    onClick={() => run("job cancel", () => cancelJob(token, job.job_id).then())}
                    disabled={!["queued", "running"].includes(job.status)}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ))}
          </div>
          {jobLog && (
            <pre className="mt-3 max-h-64 overflow-auto rounded-[8px] border border-border bg-bg p-2.5 text-[0.78rem] text-fg">
              {jobLog}
            </pre>
          )}
        </section>

        <section className={panelClass}>
          <header className={panelHeaderClass}>
            <h2 className={panelTitleClass}>Sequence Detector</h2>
            <span className="text-[0.8rem] text-fg-dim">{sequenceDetector?.active ?? "—"}</span>
          </header>
          <p className="mb-2 text-[0.75rem] text-fg-dim">
            Live backbone for the sequence slot. A switch takes effect immediately and is not
            persisted — set the durable default via <code>models.sequence_detector</code>.
          </p>
          <div className="grid grid-cols-2 gap-2 max-[1100px]:grid-cols-1">
            {(["lstm", "tcn"] as const).map((backbone) => {
              const info = sequenceDetector?.available[`${backbone}_autoencoder`];
              const isActive = info?.active ?? false;
              return (
                <button
                  key={backbone}
                  type="button"
                  className={cn(buttonClass, isActive && "border-accent")}
                  onClick={() => switchBackbone(backbone)}
                  disabled={!token || !!busy || isActive}
                  title={
                    info?.artifact_present
                      ? `artifact: ${info.version ?? "present"}`
                      : "no trained artifact yet"
                  }
                >
                  {backbone.toUpperCase()}
                  {isActive ? " (active)" : ""}
                  {info && !info.artifact_present ? " · no artifact" : ""}
                </button>
              );
            })}
          </div>
        </section>

        <section className={panelClass}>
          <header className={panelHeaderClass}>
            <h2 className={panelTitleClass}>Model Upload</h2>
          </header>
          <div className="grid grid-cols-1 gap-2">
            <select className={selectClass} value={modelType} onChange={(event) => setModelType(event.target.value)}>
              <option value="isolation_forest">isolation_forest</option>
              <option value="lstm_autoencoder">lstm_autoencoder</option>
              <option value="tcn_autoencoder">tcn_autoencoder</option>
            </select>
            <input className={inputClass} type="file" onChange={(event) => setArtifactFile(event.target.files?.[0] ?? null)} />
            <input className={inputClass} type="file" accept="application/json,.json" onChange={(event) => setManifestFile(event.target.files?.[0] ?? null)} />
            <button className={buttonClass} type="button" onClick={uploadModel} disabled={!token || !!busy}>
              Publish
            </button>
          </div>
        </section>

        <ControlEventsCard
          connection={controlStream.connection}
          events={controlStream.events}
        />
      </div>
    </section>
  );
}

function jobRowClass(status: string): string {
  return cn(
    "grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 rounded-[8px] border border-border-soft bg-[linear-gradient(180deg,var(--color-elev-2),var(--color-elev))] px-2.5 py-2 transition-colors hover:border-border max-[1100px]:grid-cols-1",
    status === "running" && "border-accent",
    status === "succeeded" && "border-ok/35",
    status === "failed" && "border-bad/55",
  );
}

function parseJsonObject(text: string): Record<string, unknown> {
  const parsed = JSON.parse(text || "{}") as unknown;
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error("Expected a JSON object.");
  }
  return parsed as Record<string, unknown>;
}

async function fileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  const chunkSize = 8192;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.slice(index, index + chunkSize));
  }
  return btoa(binary);
}

function currentPiHost(): string | null {
  const host = window.location.hostname;
  if (host === "localhost" || host === "127.0.0.1" || host === "::1") {
    return null;
  }
  return window.location.origin;
}

// Status-aware messaging (Task 6.14): 401/403 → token problem, 503 →
// feature not wired, etc. — never a raw stack-ish string.
const errorText = describeError;
