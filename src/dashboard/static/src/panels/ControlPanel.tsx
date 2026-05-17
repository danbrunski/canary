import { useCallback, useEffect, useMemo, useState } from "react";

import {
  cancelJob,
  deleteSession,
  downloadSession,
  getControlVersion,
  getCurrentCapture,
  getJobLogs,
  getPreflight,
  listJobs,
  listSessions,
  pinSession,
  startCapture,
  stopCapture,
  submitJob,
  tagCapture,
  unpinSession,
  updateSessionMetadata,
  uploadModelArtifact,
} from "../api/http";
import { useControlStream } from "../hooks/useControlStream";
import type {
  CaptureState,
  JobRecord,
  PreflightResponse,
  SessionSummary,
  VersionResponse,
} from "../types";

interface ControlPanelProps {
  token: string;
  onTokenChange: (token: string) => void;
}

const JOB_KINDS = [
  "corpus_report",
  "retrain_if",
  "retrain_lstm",
  "retrain_all",
  "refit_behavior_models",
];

export function ControlPanel({ token, onTokenChange }: ControlPanelProps) {
  const controlStream = useControlStream(token);
  const [preflight, setPreflight] = useState<PreflightResponse | null>(null);
  const [version, setVersion] = useState<VersionResponse | null>(null);
  const [capture, setCapture] = useState<CaptureState | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
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

  const selectedSession = useMemo(
    () => sessions.find((session) => session.session_id === selectedSessionId) ?? null,
    [selectedSessionId, sessions],
  );

  const refreshControl = useCallback(async () => {
    if (!token) return;
    const [nextPreflight, nextVersion, nextCapture, nextSessions, nextJobs] =
      await Promise.all([
        getPreflight(token),
        getControlVersion(token),
        getCurrentCapture(token),
        listSessions(token),
        listJobs(token),
      ]);
    setPreflight(nextPreflight);
    setVersion(nextVersion);
    setCapture(nextCapture.capture);
    setSessions(nextSessions.sessions);
    setJobs(nextJobs.jobs.slice().reverse());
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

  const run = async (label: string, action: () => Promise<void>) => {
    if (!token) {
      setNotice({ kind: "error", text: "Enter the control token first." });
      return;
    }
    setBusy(label);
    setNotice(null);
    try {
      await action();
      await refreshControl();
      setNotice({ kind: "ok", text: `${label} completed.` });
    } catch (err) {
      setNotice({ kind: "error", text: errorText(err) });
    } finally {
      setBusy(null);
    }
  };

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
    <section className="control-workspace">
      <div className="control-toolbar">
        <label>
          Control token
          <input
            type="password"
            value={token}
            onChange={(event) => onTokenChange(event.target.value)}
            placeholder="CANARY_CONTROL_TOKEN"
            autoComplete="off"
          />
        </label>
        <button type="button" onClick={() => refreshControl()} disabled={!token || !!busy}>
          Refresh
        </button>
        {version && (
          <div className="control-version">
            API v{version.api_version}
            {version.git_sha ? ` / ${version.git_sha}` : ""}
          </div>
        )}
      </div>

      {notice && <div className={`notice ${notice.kind}`}>{notice.text}</div>}
      {busy && <div className="notice busy">Working: {busy}</div>}

      <div className="control-grid">
        <section className="panel preflight-panel">
          <header>
            <h2>Preflight</h2>
            <span className={preflight?.ok ? "status-ok" : "status-bad"}>
              {preflight ? (preflight.ok ? "ready" : "attention") : "locked"}
            </span>
          </header>
          {!token ? (
            <p className="placeholder">Enter the token to unlock controls.</p>
          ) : (
            <ul className="check-list">
              {(preflight?.checks ?? []).map((check) => (
                <li key={check.name} className={check.ok ? "ok" : check.severity}>
                  <span>{check.name.replaceAll("_", " ")}</span>
                  <b>{check.detail}</b>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="panel capture-panel">
          <header>
            <h2>Capture</h2>
            <span className={capture ? "status-ok" : "dim"}>
              {capture ? "active" : "idle"}
            </span>
          </header>
          <div className="form-grid">
            <input
              value={purpose}
              onChange={(event) => setPurpose(event.target.value)}
              placeholder="purpose"
            />
            <input
              value={protocol}
              onChange={(event) => setProtocol(event.target.value)}
              placeholder="protocol"
            />
            <input
              value={vehicleState}
              onChange={(event) => setVehicleState(event.target.value)}
              placeholder="vehicle state"
            />
            <button type="button" onClick={start} disabled={!token || !!capture || !!busy}>
              Start
            </button>
          </div>
          {capture && (
            <div className="active-capture">
              <b>{capture.purpose || capture.protocol || "capture"}</b>
              <span>{capture.session_id}</span>
              <time>{formatTime(capture.started_at)}</time>
            </div>
          )}
          <div className="form-grid compact">
            <input
              value={tagLabel}
              onChange={(event) => setTagLabel(event.target.value)}
              placeholder="tag label"
            />
            <button type="button" onClick={addTag} disabled={!token || !capture || !tagLabel}>
              Tag
            </button>
            <input
              value={stopReason}
              onChange={(event) => setStopReason(event.target.value)}
              placeholder="stop reason"
            />
            <button type="button" onClick={stop} disabled={!token || !capture || !!busy}>
              Stop
            </button>
          </div>
        </section>

        <section className="panel sessions-panel">
          <header>
            <h2>Sessions</h2>
            <span className="count">{sessions.length}</span>
          </header>
          <div className="session-list">
            {sessions.slice().reverse().map((session) => (
              <button
                key={session.session_id}
                type="button"
                className={session.session_id === selectedSessionId ? "selected" : ""}
                onClick={() => setSelectedSessionId(session.session_id)}
              >
                <span>{session.session_id}</span>
                <small>
                  {session.telemetry_chunks} chunks / {session.tag_count} tags
                  {session.pinned ? " / pinned" : ""}
                </small>
              </button>
            ))}
          </div>
          {selectedSession && (
            <div className="session-actions">
              <textarea
                value={metadataText}
                onChange={(event) => setMetadataText(event.target.value)}
                rows={3}
                spellCheck={false}
              />
              <div className="button-row">
                <button type="button" onClick={saveMetadata}>Metadata</button>
                <button type="button" onClick={exportSelected}>Export</button>
                <button type="button" onClick={pinSelected}>Pin</button>
                <button type="button" onClick={unpinSelected}>Unpin</button>
                <button type="button" className="danger" onClick={deleteSelected}>
                  Delete
                </button>
              </div>
              <input
                value={pinReason}
                onChange={(event) => setPinReason(event.target.value)}
                placeholder="pin reason"
              />
            </div>
          )}
        </section>

        <section className="panel jobs-panel">
          <header>
            <h2>Jobs</h2>
            <span className="count">{jobs.length}</span>
          </header>
          <div className="form-grid compact">
            <select value={jobKind} onChange={(event) => setJobKind(event.target.value)}>
              {JOB_KINDS.map((kind) => (
                <option key={kind} value={kind}>{kind}</option>
              ))}
            </select>
            <input
              value={jobDays}
              onChange={(event) => setJobDays(event.target.value)}
              disabled={useAllSessions}
              inputMode="numeric"
              placeholder="days"
            />
            <label className="check-field">
              <input
                type="checkbox"
                checked={useAllSessions}
                onChange={(event) => setUseAllSessions(event.target.checked)}
              />
              all
            </label>
            <button type="button" onClick={startJob} disabled={!token || !!busy}>
              Start
            </button>
          </div>
          <div className="job-list">
            {jobs.map((job) => (
              <div key={job.job_id} className={`job-row ${job.status}`}>
                <div>
                  <b>{job.kind}</b>
                  <small>{job.status} / {job.job_id.slice(0, 8)}</small>
                </div>
                <div className="button-row tight">
                  <button type="button" onClick={() => showLogs(job.job_id)}>Logs</button>
                  <button
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
          {jobLog && <pre className="job-log">{jobLog}</pre>}
        </section>

        <section className="panel models-control-panel">
          <header>
            <h2>Model Upload</h2>
          </header>
          <div className="form-grid model-upload">
            <select value={modelType} onChange={(event) => setModelType(event.target.value)}>
              <option value="isolation_forest">isolation_forest</option>
              <option value="lstm_autoencoder">lstm_autoencoder</option>
            </select>
            <input
              type="file"
              onChange={(event) => setArtifactFile(event.target.files?.[0] ?? null)}
            />
            <input
              type="file"
              accept="application/json,.json"
              onChange={(event) => setManifestFile(event.target.files?.[0] ?? null)}
            />
            <button type="button" onClick={uploadModel} disabled={!token || !!busy}>
              Publish
            </button>
          </div>
        </section>

        <section className="panel control-events-panel">
          <header>
            <h2>Control Events</h2>
            <span className={`conn-text ${controlStream.connection}`}>
              {controlStream.connection}
            </span>
          </header>
          <ol className="control-events">
            {controlStream.events.slice(0, 12).map((event, index) => (
              <li key={`${event.timestamp ?? event.type ?? "event"}-${index}`}>
                <time>{event.timestamp ? formatTime(event.timestamp) : "live"}</time>
                <b>{event.action ?? event.type ?? "event"}</b>
                <span>{event.result ?? event.job?.status ?? ""}</span>
              </li>
            ))}
          </ol>
        </section>
      </div>
    </section>
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

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
