// Preflight + control-event log — the two ControlPanel cards that only
// ever needed read-only data, so they were the first split out. The rest
// (Sessions/Jobs/SequenceDetector/ModelUpload/CompanionConfig) followed in
// Task 3.2, each owning its own local UI state so the parent only passes
// down the polled data + the shared `run` action, not two dozen individual
// fields — see SessionsCard.tsx etc.

import { Panel } from "../components/Panel";
import { cn } from "../styles";
import type { ConnectionState, ControlEvent, PreflightResponse } from "../types";
import { formatTimeOfDay } from "../util/time";
import { TokenRequired } from "../components/TokenRequired";

function checkClass(ok: boolean, severity: string): string {
  if (ok) return "text-ok";
  if (severity === "warning") return "text-warn";
  return "text-bad";
}

export function PreflightCard({
  hasToken,
  preflight,
}: {
  hasToken: boolean;
  preflight: PreflightResponse | null;
}) {
  return (
    <Panel
      title="Preflight"
      actions={
        <span className={preflight?.ok ? "text-[0.8rem] text-ok" : "text-[0.8rem] text-bad"}>
          {preflight ? (preflight.ok ? "ready" : "attention") : "locked"}
        </span>
      }
    >
      {!hasToken ? (
        <TokenRequired purpose="run the pre-flight checks" />
      ) : (
        <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
          {(preflight?.checks ?? []).map((check) => (
            <li
              key={check.name}
              className="grid grid-cols-[9rem_1fr] items-baseline gap-2 border-b border-border pb-1.5 text-[0.82rem] text-fg-dim last:border-b-0"
            >
              <span>{check.name.replaceAll("_", " ")}</span>
              <b className={checkClass(check.ok, check.severity)}>{check.detail}</b>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function connectionTextClass(connection: ConnectionState): string {
  return cn(
    "text-fg-dim",
    connection === "open" && "text-ok",
    (connection === "error" || connection === "closed") && "text-bad",
  );
}

export function ControlEventsCard({
  connection,
  events,
}: {
  connection: ConnectionState;
  events: ControlEvent[];
}) {
  return (
    <Panel
      title="Control Events"
      actions={<span className={connectionTextClass(connection)}>{connection}</span>}
    >
      <ol className="m-0 flex max-h-[22rem] list-none flex-col gap-1.5 overflow-y-auto p-0">
        {events.slice(0, 12).map((event, index) => (
          <li
            key={`${event.timestamp ?? event.type ?? "event"}-${index}`}
            className="grid grid-cols-[4.8rem_1fr_auto] items-baseline gap-2 border-b border-border pb-1.5 text-[0.8rem]"
          >
            <time className="font-num text-fg-dim">
              {event.timestamp ? formatTimeOfDay(event.timestamp) : "live"}
            </time>
            <b>{event.action ?? event.type ?? "event"}</b>
            <span className="text-fg-dim">{event.result ?? event.job?.status ?? ""}</span>
          </li>
        ))}
      </ol>
    </Panel>
  );
}
