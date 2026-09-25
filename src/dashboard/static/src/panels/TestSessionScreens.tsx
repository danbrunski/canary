import type { QueuedTag } from "../hooks/useTagQueue";
import type { CaptureState, PreflightResponse, TestEvent, TestProfile } from "../types";
import {
  buttonClass,
  cn,
  dangerButtonClass,
  inputClass,
  panelClass,
  panelHeaderClass,
  panelTitleClass,
} from "../styles";
import { formatClockTime, formatElapsed } from "../util/time";

interface RecentLogProps {
  recent: QueuedTag[];
  orphans: number;
  onClearOrphans: () => void;
}

function RecentLog({ recent, orphans, onClearOrphans }: RecentLogProps) {
  return (
    <section className={panelClass}>
      <header className={panelHeaderClass}>
        <h2 className={panelTitleClass}>Recent events</h2>
        {orphans > 0 && (
          <button
            type="button"
            onClick={onClearOrphans}
            className="text-[0.74rem] text-bad underline"
          >
            clear {orphans} orphaned
          </button>
        )}
      </header>
      {recent.length === 0 ? (
        <p className="m-0 text-fg-faint italic">No events tapped yet.</p>
      ) : (
        <ol className="m-0 flex max-h-72 list-none flex-col gap-1 overflow-y-auto p-0">
          {recent.map((r) => (
            <li
              key={r.id}
              className="grid grid-cols-[5rem_1fr_auto] items-baseline gap-2 border-b border-border pb-1 text-[0.84rem] last:border-b-0"
            >
              <time className="font-num tabular-nums text-fg-dim">{formatClockTime(r.t)}</time>
              <b className="text-fg">{r.label}</b>
              <span
                className={cn(
                  "text-[0.72rem] font-bold uppercase tracking-[0.06em]",
                  r.status === "sent" && "text-ok",
                  r.status === "pending" && "text-fg-faint",
                  r.status === "failed" && "text-warn",
                )}
              >
                {r.status === "failed" ? `retry ${r.attempts}` : r.status}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

interface TestSessionSetupScreenProps {
  profiles: TestProfile[];
  profilesLoaded: boolean;
  selectedProfileId: string;
  onSelectProfile: (id: string) => void;
  selectedProfile: TestProfile | null;
  authRejected: boolean;
  preflight: PreflightResponse | null;
  purpose: string;
  onPurposeChange: (purpose: string) => void;
  busy: boolean;
  onStart: () => void;
  onRerunPreflight: () => void;
  recent: QueuedTag[];
  orphans: number;
  onClearOrphans: () => void;
}

/** Pick a profile, check preflight, start. The pre-capture half of the
 *  in-vehicle test-session screen (Task 3.2 split of TestSessionPanel). */
export function TestSessionSetupScreen({
  profiles,
  profilesLoaded,
  selectedProfileId,
  onSelectProfile,
  selectedProfile,
  authRejected,
  preflight,
  purpose,
  onPurposeChange,
  busy,
  onStart,
  onRerunPreflight,
  recent,
  orphans,
  onClearOrphans,
}: TestSessionSetupScreenProps) {
  return (
    <>
      <section className={panelClass}>
        <header className={panelHeaderClass}>
          <h2 className={panelTitleClass}>Test profile</h2>
        </header>
        {!profilesLoaded ? (
          <p className="m-0 text-fg-faint italic">Loading profiles…</p>
        ) : profiles.length === 0 ? (
          <p className="m-0 text-fg-dim">
            No profiles found. Add a <code>*.yaml</code> file under <code>data/test_profiles/</code>{" "}
            (see the README there).
          </p>
        ) : (
          <div className="grid grid-cols-2 gap-2.5 max-[680px]:grid-cols-1">
            {profiles.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => onSelectProfile(p.id)}
                className={cn(
                  "rounded-[10px] border bg-elev-2 p-3 text-left transition-colors",
                  p.id === selectedProfileId
                    ? "border-accent-2 bg-[linear-gradient(180deg,var(--color-elev-3),var(--color-elev-2))]"
                    : "border-border-soft hover:border-border",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <b className="text-fg">{p.label}</b>
                  <span className="font-num text-[0.68rem] uppercase tracking-[0.08em] text-fg-faint">
                    {p.vehicle_state}
                  </span>
                </div>
                {p.description && (
                  <p className="mt-1 text-[0.82rem] leading-snug text-fg-dim">{p.description}</p>
                )}
                <p className="mt-1.5 text-[0.72rem] text-fg-faint">
                  {p.events.length} event buttons
                </p>
              </button>
            ))}
          </div>
        )}
      </section>

      {selectedProfile && (
        <section className={panelClass}>
          <header className={panelHeaderClass}>
            <h2 className={panelTitleClass}>Preflight</h2>
            <span
              className={cn(
                "text-[0.8rem] font-bold",
                authRejected
                  ? "text-bad"
                  : preflight
                    ? preflight.ok
                      ? "text-ok"
                      : "text-bad"
                    : "text-fg-dim",
              )}
            >
              {authRejected
                ? "token required"
                : preflight
                  ? preflight.ok
                    ? "GO"
                    : "NO-GO"
                  : "checking…"}
            </span>
          </header>
          {preflight && !preflight.ok && (
            <ul className="m-0 mb-3 flex list-none flex-col gap-1 p-0">
              {preflight.checks
                .filter((c) => !c.ok)
                .map((c) => (
                  <li key={c.name} className="grid grid-cols-[9rem_1fr] gap-2 text-[0.82rem]">
                    <span className="text-fg-dim">{c.name.replaceAll("_", " ")}</span>
                    <b className={c.severity === "warning" ? "text-warn" : "text-bad"}>
                      {c.detail}
                    </b>
                  </li>
                ))}
            </ul>
          )}
          <label className="mb-3 flex flex-col gap-1 text-[0.75rem] text-fg-dim">
            Purpose (logged on the capture)
            <input
              className={cn(inputClass, "w-full")}
              value={purpose}
              onChange={(e) => onPurposeChange(e.target.value)}
            />
          </label>
          <button
            type="button"
            onClick={onStart}
            disabled={busy}
            className={cn(
              buttonClass,
              "min-h-[3.5rem] w-full text-[1.05rem] font-bold",
              preflight?.ok
                ? "border-ok bg-ok/15 text-ok"
                : "border-accent-2 bg-accent-2/15 text-fg",
            )}
          >
            {busy ? "Starting…" : `Start session — ${selectedProfile.label}`}
          </button>
          <button
            type="button"
            onClick={onRerunPreflight}
            className={cn(buttonClass, "mt-2 w-full")}
          >
            Re-run preflight
          </button>
        </section>
      )}

      {(recent.length > 0 || orphans > 0) && (
        <RecentLog recent={recent} orphans={orphans} onClearOrphans={onClearOrphans} />
      )}
    </>
  );
}

interface TestSessionRecordingScreenProps {
  activeProfile: TestProfile | null;
  capture: CaptureState;
  now: number;
  pendingCount: number;
  tapCounts: Map<string, number>;
  onTapEvent: (ev: TestEvent) => void;
  onTapCustom: () => void;
  busy: boolean;
  endArmed: boolean;
  onEnd: () => void;
  recent: QueuedTag[];
  orphans: number;
  onClearOrphans: () => void;
}

/** The big-button "tap events as they happen" drive screen — the
 *  in-capture half of the test-session screen (Task 3.2 split of
 *  TestSessionPanel). */
export function TestSessionRecordingScreen({
  activeProfile,
  capture,
  now,
  pendingCount,
  tapCounts,
  onTapEvent,
  onTapCustom,
  busy,
  endArmed,
  onEnd,
  recent,
  orphans,
  onClearOrphans,
}: TestSessionRecordingScreenProps) {
  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-[10px] border border-ok/40 bg-ok/10 px-4 py-3">
        <div className="flex flex-col">
          <b className="text-fg">{activeProfile?.label ?? "Test session"}</b>
          <span className="text-[0.78rem] text-fg-dim">{capture.session_id}</span>
        </div>
        <div className="flex items-center gap-4">
          <div className="text-right">
            <div className="text-[0.6rem] uppercase tracking-[0.16em] text-fg-faint">elapsed</div>
            <div className="font-num text-[1.4rem] tabular-nums text-fg">
              {formatElapsed(capture.started_at, now)}
            </div>
          </div>
          <div className="text-right">
            <div className="text-[0.6rem] uppercase tracking-[0.16em] text-fg-faint">queued</div>
            <div
              className={cn(
                "font-num text-[1.4rem] tabular-nums",
                pendingCount > 0 ? "text-warn" : "text-ok",
              )}
            >
              {pendingCount}
            </div>
          </div>
        </div>
      </div>

      {pendingCount > 0 && (
        <div className="rounded-[8px] border border-warn/45 bg-warn/10 px-3 py-2 text-[0.82rem] text-warn">
          {pendingCount} event{pendingCount > 1 ? "s" : ""} waiting to send — taps are saved locally
          and retried automatically; they keep their real time even if signal is down.
        </div>
      )}

      <div className="grid grid-cols-2 gap-2.5 max-[420px]:grid-cols-1">
        {(activeProfile?.events ?? []).map((ev) => {
          const tapCount = tapCounts.get(ev.tag) ?? 0;
          return (
            <button
              key={ev.tag}
              type="button"
              onClick={() => onTapEvent(ev)}
              className={cn(
                "relative flex min-h-[5.5rem] flex-col items-center justify-center gap-1 rounded-[12px] border px-3 py-3 text-center transition-all active:scale-[0.97]",
                tapCount > 0
                  ? "border-ok/55 bg-ok/10 hover:border-ok/90"
                  : "border-border bg-elev-2 hover:border-fg-dim hover:bg-elev-3",
              )}
            >
              {tapCount > 0 && (
                <span className="absolute right-2 top-2 min-w-[1.25rem] rounded-full bg-ok/25 px-1 text-center text-[0.65rem] font-bold leading-tight text-ok">
                  {tapCount}
                </span>
              )}
              <span className="text-[1.05rem] font-bold leading-tight text-fg">{ev.label}</span>
              {ev.hint && (
                <span className="text-[0.72rem] leading-tight text-fg-dim">{ev.hint}</span>
              )}
            </button>
          );
        })}
        <button
          type="button"
          onClick={onTapCustom}
          className="flex min-h-[5.5rem] flex-col items-center justify-center gap-1 rounded-[12px] border border-dashed border-border bg-transparent px-3 py-3 text-center transition-transform active:scale-[0.97] hover:border-fg-faint"
        >
          <span className="text-[1.05rem] font-bold text-fg-dim">+ Custom…</span>
          <span className="text-[0.72rem] text-fg-faint">Something unplanned</span>
        </button>
      </div>

      <button
        type="button"
        onClick={onEnd}
        disabled={busy}
        className={cn(
          dangerButtonClass,
          "min-h-[3.5rem] w-full text-[1.05rem] font-bold",
          endArmed && "bg-bad/20",
        )}
      >
        {busy
          ? "Closing…"
          : endArmed
            ? `Tap again to END${pendingCount ? ` (${pendingCount} queued)` : ""}`
            : "End session"}
      </button>

      <RecentLog recent={recent} orphans={orphans} onClearOrphans={onClearOrphans} />
    </>
  );
}
