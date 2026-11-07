import { useState } from "react";

import { getJobLogs, listJobs, submitJob } from "../api/http";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { buttonClass, cn, inputClass, placeholderClass } from "../styles";
import type { JobRecord } from "../types";
import { TOKEN_FIRST } from "../lib/tokenMessages";

const FORMATS = ["csv", "jsonl", "parquet", "feature_matrix"] as const;

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

/** Dataset export (Task 5.6b): wires the "Export" nav placeholder (Task
 *  6.20) to the existing `scripts/export_dataset.py` /
 *  `src/datasets/export.py` backend via the `export_dataset` background job
 *  kind — the CSV→Excel/MegaLogViewer path. Mark training candidates, pick
 *  sessions/channels, resample, include/exclude tag windows, and export
 *  CSV/JSONL/Parquet/feature-matrix with a provenance manifest. */
export function ExportPanel({ token }: { token: string }) {
  const [sessionIds, setSessionIds] = useState("");
  const [channels, setChannels] = useState("");
  const [resampleHz, setResampleHz] = useState("");
  const [includeTags, setIncludeTags] = useState("");
  const [excludeTags, setExcludeTags] = useState("");
  const [formats, setFormats] = useState<string[]>(["csv"]);
  const [purpose, setPurpose] = useState("");
  const [zip, setZip] = useState(true);
  const [job, setJob] = useState<JobRecord | null>(null);
  const [log, setLog] = useState("");
  const { notice, busy, run } = useAsyncAction();

  const toggleFormat = (fmt: string) =>
    setFormats((prev) => (prev.includes(fmt) ? prev.filter((f) => f !== fmt) : [...prev, fmt]));

  const submit = () =>
    run(
      "export",
      async () => {
        if (!token) throw new Error(TOKEN_FIRST);
        const ids = splitCsv(sessionIds);
        if (ids.length === 0) throw new Error("Enter at least one session id.");
        const params: Record<string, unknown> = {
          session_ids: ids,
          formats: formats.length ? formats : ["csv"],
          zip,
        };
        const chans = splitCsv(channels);
        if (chans.length) params.channels = chans;
        if (resampleHz.trim()) params.resample_hz = Number(resampleHz);
        const include = splitCsv(includeTags);
        if (include.length) params.include_tags = include;
        const exclude = splitCsv(excludeTags);
        if (exclude.length) params.exclude_tags = exclude;
        if (purpose.trim()) params.purpose = purpose.trim();

        const created = await submitJob(token, "export_dataset", params);
        setJob(created.job);
        setLog("");
      },
      { okText: "export job submitted." },
    );

  const refreshJob = () =>
    run(
      "refresh",
      async () => {
        if (!job) return;
        const jobs = await listJobs(token);
        const latest = jobs.jobs.find((j) => j.job_id === job.job_id) ?? null;
        setJob(latest);
        if (latest && latest.status !== "queued" && latest.status !== "running") {
          const logs = await getJobLogs(token, job.job_id);
          setLog(logs.log);
        }
      },
      { okText: null },
    );

  return (
    <Panel title="Export" count={job ? `job ${job.status}` : undefined}>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-2">
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          session ids (comma-separated)
          <input
            className={inputClass}
            value={sessionIds}
            onChange={(e) => setSessionIds(e.target.value)}
            placeholder="2026-05-11T222447,2026-05-13T004818"
          />
        </label>
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          channels (optional, comma-separated)
          <input
            className={inputClass}
            value={channels}
            onChange={(e) => setChannels(e.target.value)}
            placeholder="all channels if blank"
          />
        </label>
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          resample hz (optional)
          <input
            className={inputClass}
            value={resampleHz}
            onChange={(e) => setResampleHz(e.target.value)}
            placeholder="raw if blank"
          />
        </label>
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          include tags (optional)
          <input
            className={inputClass}
            value={includeTags}
            onChange={(e) => setIncludeTags(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          exclude tags (optional)
          <input
            className={inputClass}
            value={excludeTags}
            onChange={(e) => setExcludeTags(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          purpose
          <input
            className={inputClass}
            value={purpose}
            onChange={(e) => setPurpose(e.target.value)}
            placeholder="lstm baseline"
          />
        </label>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-3 text-[0.78rem]">
        {FORMATS.map((fmt) => (
          <label key={fmt} className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={formats.includes(fmt)}
              onChange={() => toggleFormat(fmt)}
            />
            {fmt}
          </label>
        ))}
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={zip} onChange={(e) => setZip(e.target.checked)} />
          zip
        </label>
      </div>

      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          className={cn(buttonClass, "px-3 py-1.5")}
          disabled={Boolean(busy)}
          onClick={submit}
        >
          {busy === "export" ? "submitting…" : "export"}
        </button>
        {job && (
          <button
            type="button"
            className={cn(buttonClass, "px-3 py-1.5")}
            disabled={Boolean(busy)}
            onClick={refreshJob}
          >
            refresh status
          </button>
        )}
      </div>

      {notice && <Notice notice={notice} className="mt-2" />}

      {job && (
        <div className="mt-3 text-[0.78rem]">
          <p className="text-fg-dim">
            job <span className="text-fg">{job.job_id}</span> — status{" "}
            <span className="text-fg">{job.status}</span>
          </p>
          {log && (
            <pre className="mt-1 max-h-56 overflow-auto rounded border border-border-soft bg-elev-2 p-2 text-[0.7rem]">
              {log}
            </pre>
          )}
        </div>
      )}
      {!job && (
        <p className={cn(placeholderClass, "mt-3")}>
          pick sessions/channels/formats above and submit — the job's log shows the exported files
          and any data-quality warnings.
        </p>
      )}
    </Panel>
  );
}
