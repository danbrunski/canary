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

import { useCallback, useEffect, useState } from "react";

import {
  HttpError,
  getCurrentCapture,
  getPreflight,
  getTestProfiles,
  startCapture,
  stopCapture,
} from "../api/http";
import { describeError } from "../hooks/useControlResource";
import { type QueuedTag, useTagQueue } from "../hooks/useTagQueue";
import type {
  CaptureState,
  PreflightResponse,
  TestEvent,
  TestProfile,
  TestProfileError,
} from "../types";
import { Notice } from "../components/Notice";
import {
  buttonClass,
  cn,
  dangerButtonClass,
  inputClass,
  noticeClass,
  type NoticeState,
  panelClass,
  panelHeaderClass,
  panelTitleClass,
} from "../styles";
import { formatClockTime, formatElapsed } from "../util/time";

interface TestSessionPanelProps {
  token: string;
  onTokenChange: (token: string) => void;
}

const PROFILE_KEY = "canary.testProfile.v1";

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
  const [notice, setNotice] = useState<NoticeState | null>(null);
  const [busy, setBusy] = useState(false);
  const [endArmed, setEndArmed] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const selectedProfile: TestProfile | null =
    profiles.find((p) => p.id === selectedProfileId) ?? profiles[0] ?? null;
  // The palette shown during an active capture: the started profile, kept in
  // localStorage so a reload mid-drive still resolves one.
  const activeProfile = selectedProfile;

  const {
    recent,
    pendingCount,
    tapCounts,
    orphans,
    enqueue,
    flushOnce,
    stillPending,
    unsentCount,
    clearOrphans,
    clearQueue,
  } = useTagQueue(token, capture);

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
  const [authRejected, setAuthRejected] = useState(false);
  const refresh = useCallback(async () => {
    // Always probe — a token may not be needed (server open on loopback for
    // dev/sim). An empty token just sends an empty bearer; the server either
    // allows it (loopback, unconfigured) or 401s (token required).
    const [cap, pre] = await Promise.allSettled([
      getCurrentCapture(token),
      getPreflight(token),
    ]);
    if (cap.status === "fulfilled") setCapture(cap.value.capture);
    if (pre.status === "fulfilled") setPreflight(pre.value);
    const isAuth = (r: PromiseSettledResult<unknown>) =>
      r.status === "rejected" &&
      r.reason instanceof HttpError &&
      (r.reason.status === 401 || r.reason.status === 403);
    if ([cap, pre].some(isAuth)) setAuthRejected(true);
    else if (cap.status === "fulfilled" || pre.status === "fulfilled")
      setAuthRejected(false);
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

  // --- actions ------------------------------------------------------------
  const tapEvent = (ev: TestEvent) => enqueue(ev.tag, ev.kind);

  const tapCustom = () => {
    const label = window.prompt("Custom event label (becomes the tag):");
    const clean = label?.trim();
    if (clean) enqueue(clean, "event");
  };

  const start = async () => {
    // No hard token gate — the server decides (open on loopback, or 401 if a
    // token is required); a rejection surfaces via the notice below.
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
        const left = unsentCount();
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
      const leftoverCount = unsentCount();
      clearQueue();
      setCapture(null);
      setNotice({
        kind: leftoverCount ? "error" : "ok",
        text: leftoverCount
          ? `Session closed — but ${leftoverCount} event(s) were not saved.`
          : "Session closed and recording finalized.",
      });
    } catch (err) {
      setNotice({ kind: "error", text: describeError(err) });
    } finally {
      setBusy(false);
    }
  };

  // Control reachable without a token (server open on loopback for dev/sim):
  // we got a preflight back despite sending no token and weren't 401'd.
  const openNoToken = !token && !authRejected && preflight !== null;
  const tokenLabel = token
    ? authRejected
      ? "rejected"
      : "unlocked"
    : authRejected
      ? "required"
      : openNoToken
        ? "not needed"
        : "checking…";
  const tokenLabelClass = cn(
    "text-[0.78rem] font-bold",
    (token && authRejected) || (!token && authRejected)
      ? "text-bad"
      : token || openNoToken
        ? "text-ok"
        : "text-fg-dim",
  );
  const tokenHelp = token
    ? authRejected
      ? "The server rejected this token (401) — wrong or stale value. Re-enter it or Clear and try again."
      : "Stored on this device; also unlocks the Control Plane panel."
    : authRejected
      ? "This server requires a token. Paste the one it was started with (Pi: /etc/canary/control.token)."
      : openNoToken
        ? "No token needed here — this server allows control from loopback (dev/sim). You'd only paste one if you point this browser at the Pi."
        : "Pick a profile; checking whether this server needs a token…";

  // --- render -------------------------------------------------------------
  return (
    <section className="flex flex-col gap-3">
      <section className={panelClass}>
        <header className={panelHeaderClass}>
          <h2 className={panelTitleClass}>Control token</h2>
          <span className={tokenLabelClass}>{tokenLabel}</span>
        </header>
        <p className="mb-2 text-[0.82rem] text-fg-dim">{tokenHelp}</p>
        <div className="flex gap-2">
          <input
            className={cn(inputClass, "w-full")}
            type="password"
            autoComplete="off"
            placeholder="CANARY_CONTROL_TOKEN"
            value={token}
            onChange={(e) => onTokenChange(e.target.value)}
          />
          {token && (
            <button
              type="button"
              className={buttonClass}
              onClick={() => onTokenChange("")}
            >
              Clear
            </button>
          )}
        </div>
      </section>

      <Notice notice={notice} />

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
                onClick={() => refresh()}
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
                  {formatElapsed(capture.started_at, now)}
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
            {(activeProfile?.events ?? []).map((ev) => {
              const tapCount = tapCounts.get(ev.tag) ?? 0;
              return (
                <button
                  key={ev.tag}
                  type="button"
                  onClick={() => tapEvent(ev)}
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
                  <span className="text-[1.05rem] font-bold leading-tight text-fg">
                    {ev.label}
                  </span>
                  {ev.hint && (
                    <span className="text-[0.72rem] leading-tight text-fg-dim">
                      {ev.hint}
                    </span>
                  )}
                </button>
              );
            })}
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
