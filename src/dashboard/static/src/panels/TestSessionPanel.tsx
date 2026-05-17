// Test-session panel — the in-vehicle "tap an event as it happens" screen.
//
// Pick a profile, Start, then tap big buttons as events occur while driving.
// Every tap writes a tag at the *click time* (not receive time) and is
// flushed by a background retry queue that survives cell-signal dropouts and
// a page reload (localStorage), so a flaky connection never loses or
// mis-times an event.
//
// Profiles are NOT hardcoded here — they come from the scanned YAML catalog
// at GET /test-profiles (src/control/test_profiles.py, data/test_profiles/).

import { useCallback, useEffect, useRef, useState } from "react";

import {
  getCurrentCapture,
  getPreflight,
  getTestProfiles,
  startCapture,
  stopCapture,
  tagCapture,
} from "../api/http";
import { describeError } from "../hooks/useControlResource";
import type {
  CaptureState,
  PreflightResponse,
  TestEvent,
  TestProfile,
  TestProfileError,
} from "../types";
import {
  buttonClass,
  cn,
  dangerButtonClass,
  inputClass,
  noticeClass,
  panelClass,
  panelHeaderClass,
  panelTitleClass,
} from "../styles";

interface TestSessionPanelProps {
  token: string;
  onTokenChange: (token: string) => void;
}

type TagStatus = "pending" | "sent" | "failed";

interface QueuedTag {
  id: string;
  captureId: string;
  label: string;
  kind: string;
  /** Unix seconds at the moment the operator tapped. */
  t: number;
  attempts: number;
  status: TagStatus;
  error?: string;
}

const QUEUE_KEY = "canary.testQueue.v1";
const PROFILE_KEY = "canary.testProfile.v1";
const MAX_RECENT = 40;

interface PersistedQueue {
  queue: QueuedTag[];
  recent: QueuedTag[];
}

function loadQueue(): PersistedQueue {
  try {
    const raw = window.localStorage.getItem(QUEUE_KEY);
    if (!raw) return { queue: [], recent: [] };
    const parsed = JSON.parse(raw) as Partial<PersistedQueue>;
    return {
      queue: Array.isArray(parsed.queue) ? parsed.queue : [],
      recent: Array.isArray(parsed.recent) ? parsed.recent : [],
    };
  } catch {
    return { queue: [], recent: [] };
  }
}

function saveQueue(state: PersistedQueue): void {
  try {
    window.localStorage.setItem(
      QUEUE_KEY,
      JSON.stringify({ queue: state.queue, recent: state.recent.slice(0, MAX_RECENT) }),
    );
  } catch {
    /* localStorage full / unavailable — in-memory still works for this run */
  }
}

function newId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function clockTime(t: number): string {
  return new Date(t * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function elapsed(startedAt: string, now: number): string {
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return "—";
  const secs = Math.max(0, Math.floor((now - start) / 1000));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function TestSessionPanel({ token, onTokenChange }: TestSessionPanelProps) {
  const [profiles, setProfiles] = useState<TestProfile[]>([]);
  const [profileLoadErrors, setProfileLoadErrors] = useState<TestProfileError[]>([]);
  const [profilesLoaded, setProfilesLoaded] = useState(false);
  const [capture, setCapture] = useState<CaptureState | null>(null);
  const [preflight, setPreflight] = useState<PreflightResponse | null>(null);
  const [selectedProfileId, setSelectedProfileId] = useState<string>(
    () => window.localStorage.getItem(PROFILE_KEY) ?? "",
  );
  const [purpose, setPurpose] = useState("");
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [endArmed, setEndArmed] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const persisted = useRef<PersistedQueue>(loadQueue());
  const [queue, setQueue] = useState<QueuedTag[]>(persisted.current.queue);
  const [recent, setRecent] = useState<QueuedTag[]>(persisted.current.recent);
  const flushing = useRef(false);

  const selectedProfile: TestProfile | null =
    profiles.find((p) => p.id === selectedProfileId) ?? profiles[0] ?? null;
  // The palette shown during an active capture: the started profile, kept in
  // localStorage so a reload mid-drive still resolves one.
  const activeProfile = selectedProfile;

  const pendingCount = queue.filter((q) => q.status !== "sent").length;

  const persist = useCallback((q: QueuedTag[], r: QueuedTag[]) => {
    persisted.current = { queue: q, recent: r };
    saveQueue(persisted.current);
  }, []);

  // --- profile catalog (unauthenticated; works before a token is set) ----
  useEffect(() => {
    const ctrl = new AbortController();
    getTestProfiles(ctrl.signal)
      .then((res) => {
        setProfiles(res.profiles);
        setProfileLoadErrors(res.errors);
        setProfilesLoaded(true);
      })
      .catch((err: unknown) => {
        if (!ctrl.signal.aborted) {
          setProfilesLoaded(true);
          setNotice({ kind: "error", text: `Profiles: ${describeError(err)}` });
        }
      });
    return () => ctrl.abort();
  }, []);

  // Settle the selection once profiles arrive.
  useEffect(() => {
    if (profiles.length === 0) return;
    setSelectedProfileId((cur) =>
      profiles.some((p) => p.id === cur) ? cur : profiles[0].id,
    );
  }, [profiles]);

  // --- capture + preflight polling ---------------------------------------
  const refresh = useCallback(async () => {
    if (!token) {
      setCapture(null);
      setPreflight(null);
      return;
    }
    const [cap, pre] = await Promise.allSettled([
      getCurrentCapture(token),
      getPreflight(token),
    ]);
    if (cap.status === "fulfilled") setCapture(cap.value.capture);
    if (pre.status === "fulfilled") setPreflight(pre.value);
  }, [token]);

  useEffect(() => {
    refresh().catch(() => undefined);
    const timer = window.setInterval(() => refresh().catch(() => undefined), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  // 1 Hz tick so the elapsed clock stays live.
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (selectedProfileId) window.localStorage.setItem(PROFILE_KEY, selectedProfileId);
  }, [selectedProfileId]);

  useEffect(() => {
    if (!capture && selectedProfile) setPurpose(selectedProfile.purpose);
  }, [selectedProfile, capture]);

  // --- the retry flusher --------------------------------------------------
  const flushOnce = useCallback(async (): Promise<void> => {
    if (flushing.current || !token) return;
    const cap = capture;
    if (!cap) return;
    flushing.current = true;
    try {
      // Oldest first, one at a time — preserves order, won't hammer a flaky link.
      const next = persisted.current.queue.find(
        (q) => q.status !== "sent" && q.captureId === cap.capture_id,
      );
      if (!next) return;
      try {
        await tagCapture(token, {
          label: next.label,
          kind: next.kind,
          timestamp: next.t,
          fields: {},
        });
        const work = persisted.current.queue.filter((q) => q.id !== next.id);
        const rec = [{ ...next, status: "sent" as const }, ...persisted.current.recent].slice(
          0,
          MAX_RECENT,
        );
        persist(work, rec);
        setQueue(work);
        setRecent(rec);
      } catch (err) {
        const work = persisted.current.queue.map((q) =>
          q.id === next.id
            ? {
                ...q,
                attempts: q.attempts + 1,
                status: "failed" as const,
                error: describeError(err),
              }
            : q,
        );
        persist(work, persisted.current.recent);
        setQueue(work);
      }
    } finally {
      flushing.current = false;
    }
  }, [token, capture, persist]);

  useEffect(() => {
    if (pendingCount === 0 || !capture) return;
    flushOnce().catch(() => undefined);
    const timer = window.setInterval(() => flushOnce().catch(() => undefined), 2500);
    return () => window.clearInterval(timer);
  }, [pendingCount, capture, flushOnce]);

  // --- actions ------------------------------------------------------------
  const enqueue = useCallback(
    (label: string, kind: string) => {
      if (!capture) return;
      const row: QueuedTag = {
        id: newId(),
        captureId: capture.capture_id,
        label,
        kind,
        t: Date.now() / 1000,
        attempts: 0,
        status: "pending",
      };
      const q = [...persisted.current.queue, row];
      const r = [row, ...persisted.current.recent].slice(0, MAX_RECENT);
      persist(q, r);
      setQueue(q);
      setRecent(r);
      flushOnce().catch(() => undefined);
    },
    [capture, persist, flushOnce],
  );

  const tapEvent = (ev: TestEvent) => enqueue(ev.tag, ev.kind);

  const tapCustom = () => {
    const label = window.prompt("Custom event label (becomes the tag):");
    const clean = label?.trim();
    if (clean) enqueue(clean, "event");
  };

  const start = async () => {
    if (!token) {
      setNotice({ kind: "error", text: "Enter the control token first." });
      return;
    }
    if (!selectedProfile) {
      setNotice({ kind: "error", text: "No test profile selected." });
      return;
    }
    if (preflight && !preflight.ok) {
      const bad = preflight.checks
        .filter((c) => !c.ok)
        .map((c) => c.name.replaceAll("_", " "))
        .join(", ");
      if (
        !window.confirm(
          `Preflight is NO-GO (${bad}). Recording anyway risks an unusable drive. Start regardless?`,
        )
      )
        return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const res = await startCapture(token, {
        purpose: purpose || selectedProfile.purpose,
        protocol: selectedProfile.protocol,
        vehicle_state: selectedProfile.vehicle_state,
        fields: { test_profile: selectedProfile.id },
      });
      setCapture(res.capture);
      setNotice({
        kind: "ok",
        text: `Recording — ${selectedProfile.label}. Tap events as they happen.`,
      });
    } catch (err) {
      setNotice({ kind: "error", text: describeError(err) });
    } finally {
      setBusy(false);
    }
  };

  // Reads the live persisted queue (React state is stale inside the loop).
  const stillPending = () =>
    persisted.current.queue.some((q) => q.status !== "sent");

  const end = async () => {
    if (!endArmed) {
      setEndArmed(true);
      window.setTimeout(() => setEndArmed(false), 4000);
      return;
    }
    setEndArmed(false);
    setBusy(true);
    setNotice(null);
    try {
      // Drain the queue before closing — tags can only attach to an active
      // capture, so anything unsent now would otherwise be lost.
      for (let i = 0; i < 6 && stillPending(); i += 1) {
        await flushOnce();
      }
      if (stillPending()) {
        const left = persisted.current.queue.filter((q) => q.status !== "sent").length;
        if (
          !window.confirm(
            `${left} event(s) still unsent (no signal?). End anyway? They will be LOST.`,
          )
        ) {
          setBusy(false);
          return;
        }
      }
      await stopCapture(token, {
        reason: `test session ended (${activeProfile?.label ?? "test"})`,
        fields: {},
      });
      const leftover = persisted.current.queue.filter((q) => q.status !== "sent");
      persist([], persisted.current.recent);
      setQueue([]);
      setCapture(null);
      setNotice({
        kind: leftover.length ? "error" : "ok",
        text: leftover.length
          ? `Session closed — but ${leftover.length} event(s) were not saved.`
          : "Session closed and recording finalized.",
      });
    } catch (err) {
      setNotice({ kind: "error", text: describeError(err) });
    } finally {
      setBusy(false);
    }
  };

  const clearOrphans = () => {
    const kept = persisted.current.queue.filter(
      (q) => capture && q.captureId === capture.capture_id && q.status !== "sent",
    );
    persist(kept, persisted.current.recent);
    setQueue(kept);
  };

  const orphans = queue.filter(
    (q) => q.status !== "sent" && (!capture || q.captureId !== capture.capture_id),
  ).length;

  // --- render -------------------------------------------------------------
  return (
    <section className="flex flex-col gap-3">
      {!token && (
        <section className={panelClass}>
          <header className={panelHeaderClass}>
            <h2 className={panelTitleClass}>Control token</h2>
          </header>
          <p className="mb-2 text-[0.84rem] text-fg-dim">
            Pick a profile below now; enter the token to run preflight and
            start recording.
          </p>
          <input
            className={cn(inputClass, "w-full")}
            type="password"
            autoComplete="off"
            placeholder="CANARY_CONTROL_TOKEN"
            onChange={(e) => onTokenChange(e.target.value)}
          />
        </section>
      )}

      {notice && <div className={noticeClass(notice.kind)}>{notice.text}</div>}

      {profileLoadErrors.length > 0 && (
        <div className={noticeClass("error")}>
          {profileLoadErrors.length} profile file(s) failed to load:{" "}
          {profileLoadErrors.map((e) => `${e.file} (${e.error})`).join("; ")}
        </div>
      )}

      {!capture ? (
        // ---- Setup: pick a profile, check preflight, start --------------
        <>
          <section className={panelClass}>
            <header className={panelHeaderClass}>
              <h2 className={panelTitleClass}>Test profile</h2>
            </header>
            {!profilesLoaded ? (
              <p className="m-0 text-fg-faint italic">Loading profiles…</p>
            ) : profiles.length === 0 ? (
              <p className="m-0 text-fg-dim">
                No profiles found. Add a <code>*.yaml</code> file under{" "}
                <code>data/test_profiles/</code> (see the README there).
              </p>
            ) : (
              <div className="grid grid-cols-2 gap-2.5 max-[680px]:grid-cols-1">
                {profiles.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => setSelectedProfileId(p.id)}
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
                      <p className="mt-1 text-[0.82rem] leading-snug text-fg-dim">
                        {p.description}
                      </p>
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
                    !token
                      ? "text-fg-dim"
                      : preflight
                        ? preflight.ok
                          ? "text-ok"
                          : "text-bad"
                        : "text-fg-dim",
                  )}
                >
                  {!token
                    ? "token needed"
                    : preflight
                      ? preflight.ok
                        ? "GO"
                        : "NO-GO"
                      : "checking…"}
                </span>
              </header>
              {token && preflight && !preflight.ok && (
                <ul className="m-0 mb-3 flex list-none flex-col gap-1 p-0">
                  {preflight.checks
                    .filter((c) => !c.ok)
                    .map((c) => (
                      <li
                        key={c.name}
                        className="grid grid-cols-[9rem_1fr] gap-2 text-[0.82rem]"
                      >
                        <span className="text-fg-dim">
                          {c.name.replaceAll("_", " ")}
                        </span>
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
                  onChange={(e) => setPurpose(e.target.value)}
                />
              </label>
              <button
                type="button"
                onClick={start}
                disabled={busy || !token}
                className={cn(
                  buttonClass,
                  "min-h-[3.5rem] w-full text-[1.05rem] font-bold",
                  token && preflight?.ok
                    ? "border-ok bg-ok/15 text-ok"
                    : "border-accent-2 bg-accent-2/15 text-fg",
                )}
              >
                {busy
                  ? "Starting…"
                  : !token
                    ? "Enter token to start"
                    : `Start session — ${selectedProfile.label}`}
              </button>
              <button
                type="button"
                onClick={() => refresh()}
                disabled={!token}
                className={cn(buttonClass, "mt-2 w-full")}
              >
                Re-run preflight
              </button>
            </section>
          )}

          {(recent.length > 0 || orphans > 0) && (
            <RecentLog
              recent={recent}
              orphans={orphans}
              onClearOrphans={clearOrphans}
            />
          )}
        </>
      ) : (
        // ---- Active: the big-button drive screen ------------------------
        <>
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-[10px] border border-ok/40 bg-ok/10 px-4 py-3">
            <div className="flex flex-col">
              <b className="text-fg">{activeProfile?.label ?? "Test session"}</b>
              <span className="text-[0.78rem] text-fg-dim">{capture.session_id}</span>
            </div>
            <div className="flex items-center gap-4">
              <div className="text-right">
                <div className="text-[0.6rem] uppercase tracking-[0.16em] text-fg-faint">
                  elapsed
                </div>
                <div className="font-num text-[1.4rem] tabular-nums text-fg">
                  {elapsed(capture.started_at, now)}
                </div>
              </div>
              <div className="text-right">
                <div className="text-[0.6rem] uppercase tracking-[0.16em] text-fg-faint">
                  queued
                </div>
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
              {pendingCount} event{pendingCount > 1 ? "s" : ""} waiting to send —
              taps are saved locally and retried automatically; they keep their
              real time even if signal is down.
            </div>
          )}

          <div className="grid grid-cols-2 gap-2.5 max-[420px]:grid-cols-1">
            {(activeProfile?.events ?? []).map((ev) => (
              <button
                key={ev.tag}
                type="button"
                onClick={() => tapEvent(ev)}
                className={cn(
                  "flex min-h-[5.5rem] flex-col items-center justify-center gap-1 rounded-[12px] border px-3 py-3 text-center transition-transform active:scale-[0.97]",
                  ev.kind === "state"
                    ? "border-accent-2/55 bg-[linear-gradient(180deg,var(--color-elev-3),var(--color-elev-2))]"
                    : "border-border bg-elev-2 hover:border-fg-faint",
                )}
              >
                <span className="text-[1.05rem] font-bold leading-tight text-fg">
                  {ev.label}
                </span>
                {ev.hint && (
                  <span className="text-[0.72rem] leading-tight text-fg-dim">
                    {ev.hint}
                  </span>
                )}
              </button>
            ))}
            <button
              type="button"
              onClick={tapCustom}
              className="flex min-h-[5.5rem] flex-col items-center justify-center gap-1 rounded-[12px] border border-dashed border-border bg-transparent px-3 py-3 text-center transition-transform active:scale-[0.97] hover:border-fg-faint"
            >
              <span className="text-[1.05rem] font-bold text-fg-dim">+ Custom…</span>
              <span className="text-[0.72rem] text-fg-faint">Something unplanned</span>
            </button>
          </div>

          <button
            type="button"
            onClick={end}
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

          <RecentLog recent={recent} orphans={orphans} onClearOrphans={clearOrphans} />
        </>
      )}
    </section>
  );
}

function RecentLog({
  recent,
  orphans,
  onClearOrphans,
}: {
  recent: QueuedTag[];
  orphans: number;
  onClearOrphans: () => void;
}) {
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
              <time className="font-num tabular-nums text-fg-dim">{clockTime(r.t)}</time>
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
