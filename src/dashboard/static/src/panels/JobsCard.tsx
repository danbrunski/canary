import { useState } from "react";

import { cancelJob, getJobLogs, submitJob } from "../api/http";
import type { RunAction } from "../hooks/useAsyncAction";
import { JOB_KINDS } from "../lib/jobKinds";
import {
  buttonClass,
  cn,
  inputClass,
  panelClass,
  panelCountClass,
  panelHeaderClass,
  panelTitleClass,
  selectClass,
} from "../styles";
import type { JobRecord } from "../types";

interface JobsCardProps {
  token: string;
  busy: string | null;
  run: RunAction;
  jobs: JobRecord[];
}

function jobRowClass(status: string): string {
  return cn(
    "grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 rounded-[8px] border border-border-soft bg-[linear-gradient(180deg,var(--color-elev-2),var(--color-elev))] px-2.5 py-2 transition-colors hover:border-border max-[1100px]:grid-cols-1",
    status === "running" && "border-accent",
    status === "succeeded" && "border-ok/35",
    status === "failed" && "border-bad/55",
  );
}

/** Job submission + log viewer, extracted from ControlPanel's god-panel
 *  (Task 3.2). Owns its own submit-form/log state; the parent feeds it the
 *  polled job list. */
export function JobsCard({ token, busy, run, jobs }: JobsCardProps) {
  const [jobKind, setJobKind] = useState("corpus_report");
  const [jobDays, setJobDays] = useState("14");
  const [useAllSessions, setUseAllSessions] = useState(false);
  const [jobLog, setJobLog] = useState("");

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

  return (
    <section className={cn(panelClass, "col-span-2 max-[1100px]:col-span-1")}>
      <header className={panelHeaderClass}>
        <h2 className={panelTitleClass}>Jobs</h2>
        <span className={panelCountClass}>{jobs.length}</span>
      </header>
      <div className="mt-3 grid grid-cols-[1fr_auto_1fr_auto] gap-2 max-[1100px]:grid-cols-1">
        <select
          className={selectClass}
          value={jobKind}
          onChange={(event) => setJobKind(event.target.value)}
        >
          {JOB_KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {kind}
            </option>
          ))}
        </select>
        <input
          className={inputClass}
          value={jobDays}
          onChange={(event) => setJobDays(event.target.value)}
          disabled={useAllSessions}
          inputMode="numeric"
          placeholder="days"
        />
        <label className="flex min-h-9 flex-row items-center justify-center gap-2 text-[0.75rem] text-fg-dim">
          <input
            type="checkbox"
            checked={useAllSessions}
            onChange={(event) => setUseAllSessions(event.target.checked)}
          />
          all
        </label>
        <button
          className={buttonClass}
          type="button"
          onClick={startJob}
          disabled={!token || !!busy}
        >
          Start
        </button>
      </div>
      <div className="mt-3 flex max-h-80 flex-col gap-1.5 overflow-y-auto">
        {jobs.map((job) => (
          <div key={job.job_id} className={cn(jobRowClass(job.status))}>
            <div className="flex min-w-0 flex-col">
              <b>{job.kind}</b>
              <small className="text-fg-dim">
                {job.status} / {job.job_id.slice(0, 8)}
              </small>
            </div>
            <div className="flex flex-wrap justify-end gap-1.5">
              <button className={buttonClass} type="button" onClick={() => showLogs(job.job_id)}>
                Logs
              </button>
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
  );
}
