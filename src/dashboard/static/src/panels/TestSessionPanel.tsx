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
  getCurrentCapture,
  getPreflight,
  getTestProfiles,
  startCapture,
  stopCapture,
} from "../api/http";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { describeError, useControlResource } from "../hooks/useControlResource";
import { useTagQueue } from "../hooks/useTagQueue";
import type { TestEvent, TestProfile, TestProfileError } from "../types";
import { Notice } from "../components/Notice";
import { noticeClass } from "../styles";
import { readString, writeString } from "../util/storage";
import { TestSessionRecordingScreen, TestSessionSetupScreen } from "./TestSessionScreens";

interface TestSessionPanelProps {
  token: string;
}

const PROFILE_KEY = "canary.testProfile.v1";

export function TestSessionPanel({ token }: TestSessionPanelProps) {
  const [profiles, setProfiles] = useState<TestProfile[]>([]);
  const [profileLoadErrors, setProfileLoadErrors] = useState<TestProfileError[]>([]);
  const [profilesLoaded, setProfilesLoaded] = useState(false);
  const [selectedProfileId, setSelectedProfileId] = useState<string>(() => readString(PROFILE_KEY));
  const [purpose, setPurpose] = useState("");
  const { notice, setNotice, busy, run } = useAsyncAction();
  const [endArmed, setEndArmed] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const selectedProfile: TestProfile | null =
    profiles.find((p) => p.id === selectedProfileId) ?? profiles[0] ?? null;
  // The palette shown during an active capture: the started profile, kept in
  // localStorage so a reload mid-drive still resolves one.
  const activeProfile = selectedProfile;

  // --- capture + preflight polling ---------------------------------------
  // Always probe — a token may not be needed (server open on loopback for
  // dev/sim). An empty token just sends an empty bearer; the server either
  // allows it (loopback, unconfigured) or 401s (token required). Two
  // independent resources (rather than one combined fetch) so a failure on
  // one side doesn't blank out the other's last-known-good value.
  const captureFetcher = useCallback(
    (signal: AbortSignal) => getCurrentCapture(token, signal),
    [token],
  );
  const preflightFetcher = useCallback(
    (signal: AbortSignal) => getPreflight(token, signal),
    [token],
  );
  const captureRes = useControlResource(captureFetcher, { intervalMs: 5000 });
  const preflightRes = useControlResource(preflightFetcher, { intervalMs: 5000 });
  const capture = captureRes.data?.capture ?? null;
  const preflight = preflightRes.data ?? null;
  const authRejected =
    captureRes.status === 401 ||
    captureRes.status === 403 ||
    preflightRes.status === 401 ||
    preflightRes.status === 403;
  const refreshBoth = () => {
    captureRes.refresh();
    preflightRes.refresh();
  };

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
  }, [setNotice]);

  // Settle the selection once profiles arrive (while rendering, on the new
  // list, so the picker and the persisted id agree in the same commit).
  const [settledProfiles, setSettledProfiles] = useState(profiles);
  if (settledProfiles !== profiles) {
    setSettledProfiles(profiles);
    if (profiles.length > 0 && !profiles.some((p) => p.id === selectedProfileId)) {
      setSelectedProfileId(profiles[0].id);
    }
  }

  // 1 Hz tick so the elapsed clock stays live.
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (selectedProfileId) writeString(PROFILE_KEY, selectedProfileId);
  }, [selectedProfileId]);

  // Prefill the purpose from the profile whenever the profile changes or a
  // capture ends. Keyed on whether a capture is open, not on the polled
  // object, which is new every 5 s and changes nothing while one is.
  const capturing = capture !== null;
  const [purposeFor, setPurposeFor] = useState<{
    profile: TestProfile | null;
    capturing: boolean;
  } | null>(null);
  if (purposeFor?.profile !== selectedProfile || purposeFor.capturing !== capturing) {
    setPurposeFor({ profile: selectedProfile, capturing });
    if (!capturing && selectedProfile) setPurpose(selectedProfile.purpose);
  }

  // --- actions ------------------------------------------------------------
  const tapEvent = (ev: TestEvent) => enqueue(ev.tag, ev.kind);

  const tapCustom = () => {
    const label = window.prompt("Custom event label (becomes the tag):");
    const clean = label?.trim();
    if (clean) enqueue(clean, "event");
  };

  const start = () => {
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
    void run(
      "test session start",
      async () => {
        await startCapture(token, {
          purpose: purpose || selectedProfile.purpose,
          protocol: selectedProfile.protocol,
          vehicle_state: selectedProfile.vehicle_state,
          fields: { test_profile: selectedProfile.id },
        });
      },
      {
        then: refreshBoth,
        okText: `Recording — ${selectedProfile.label}. Tap events as they happen.`,
      },
    );
  };

  const end = async () => {
    if (!endArmed) {
      setEndArmed(true);
      window.setTimeout(() => setEndArmed(false), 4000);
      return;
    }
    setEndArmed(false);
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
        return;
      }
    }
    void run(
      "test session end",
      async () => {
        await stopCapture(token, {
          reason: `test session ended (${activeProfile?.label ?? "test"})`,
          fields: {},
        });
        const leftoverCount = unsentCount();
        clearQueue();
        setNotice({
          kind: leftoverCount ? "error" : "ok",
          text: leftoverCount
            ? `Session closed — but ${leftoverCount} event(s) were not saved.`
            : "Session closed and recording finalized.",
        });
      },
      { then: refreshBoth, okText: null },
    );
  };

  // --- render -------------------------------------------------------------
  // Task 5.1: token entry itself now lives in the shell header (TokenGate);
  // `authRejected` (above) still drives the Preflight header's "token
  // required" reading and gates Start below.
  return (
    <section className="flex flex-col gap-3">
      <Notice notice={notice} />

      {profileLoadErrors.length > 0 && (
        <div className={noticeClass("error")}>
          {profileLoadErrors.length} profile file(s) failed to load:{" "}
          {profileLoadErrors.map((e) => `${e.file} (${e.error})`).join("; ")}
        </div>
      )}

      {!capture ? (
        <TestSessionSetupScreen
          profiles={profiles}
          profilesLoaded={profilesLoaded}
          selectedProfileId={selectedProfileId}
          onSelectProfile={setSelectedProfileId}
          selectedProfile={selectedProfile}
          authRejected={authRejected}
          preflight={preflight}
          purpose={purpose}
          onPurposeChange={setPurpose}
          busy={Boolean(busy)}
          onStart={start}
          onRerunPreflight={refreshBoth}
          recent={recent}
          orphans={orphans}
          onClearOrphans={clearOrphans}
        />
      ) : (
        <TestSessionRecordingScreen
          activeProfile={activeProfile}
          capture={capture}
          now={now}
          pendingCount={pendingCount}
          tapCounts={tapCounts}
          onTapEvent={tapEvent}
          onTapCustom={tapCustom}
          busy={Boolean(busy)}
          endArmed={endArmed}
          onEnd={end}
          recent={recent}
          orphans={orphans}
          onClearOrphans={clearOrphans}
        />
      )}
    </section>
  );
}
