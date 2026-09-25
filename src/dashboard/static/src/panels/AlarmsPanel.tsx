import { useEffect, useMemo, useRef, useState } from "react";

import { Panel } from "../components/Panel";
import { useLocalStorageJson } from "../hooks/useLocalStorage";
import { useNow } from "../hooks/useNow";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { buttonClass, cn, dangerButtonClass, inputClass, placeholderClass } from "../styles";

interface AlarmsPanelProps {
  channels: Map<string, ChannelSnapshot>;
}

type Op = ">" | "<" | ">=" | "<=";
type Severity = "warn" | "critical";

interface AlarmRule {
  id: string;
  channel: string;
  op: Op;
  value: number;
  severity: Severity;
  debounce: number; // consecutive breaches before firing
  cooldownS: number; // mute window after firing
  enabled: boolean;
}

interface AlarmEvent {
  ruleId: string;
  channel: string;
  value: number;
  severity: Severity;
  at: number;
}

const STORAGE_KEY = "canary.alarms";

const COMPARE: Record<Op, (a: number, b: number) => boolean> = {
  ">": (a, b) => a > b,
  "<": (a, b) => a < b,
  ">=": (a, b) => a >= b,
  "<=": (a, b) => a <= b,
};

/** User threshold alarms (Task 6.11). Operator-defined per-channel limits
 *  with debounce + cool-down, evaluated client-side against the live
 *  telemetry stream. Deliberately *visually distinct* from ML anomalies
 *  (these are "you told me to watch this", not "the model is surprised") —
 *  rules + history persist per browser via localStorage. */
export function AlarmsPanel({ channels }: AlarmsPanelProps) {
  const [rules, setRules] = useLocalStorageJson<AlarmRule[]>(STORAGE_KEY, []);
  const [history, setHistory] = useState<AlarmEvent[]>([]);
  const [audible, setAudible] = useState(false);

  // Per-rule runtime: consecutive breach count + last fire timestamp.
  const runtime = useRef<Map<string, { streak: number; mutedUntil: number }>>(new Map());

  const [draft, setDraft] = useState({
    channel: "",
    op: ">" as Op,
    value: "",
    severity: "warn" as Severity,
    debounce: "3",
    cooldownS: "30",
  });

  const numericChannels = useMemo(
    () =>
      [...channels.entries()]
        .filter(([, s]) => typeof s.latest.value === "number")
        .map(([k]) => k)
        .sort(),
    [channels],
  );

  useEffect(() => {
    const tick = () => {
      const now = Date.now();
      for (const rule of rules) {
        if (!rule.enabled) continue;
        const snap = channels.get(rule.channel);
        const v = snap?.latest.value;
        if (typeof v !== "number" || !Number.isFinite(v)) continue;
        const rt = runtime.current.get(rule.id) ?? { streak: 0, mutedUntil: 0 };
        if (COMPARE[rule.op](v, rule.value)) {
          rt.streak += 1;
          if (rt.streak >= rule.debounce && now >= rt.mutedUntil) {
            rt.mutedUntil = now + rule.cooldownS * 1000;
            setHistory((h) =>
              [
                {
                  ruleId: rule.id,
                  channel: rule.channel,
                  value: v,
                  severity: rule.severity,
                  at: now,
                },
                ...h,
              ].slice(0, 50),
            );
            if (audible) beep(rule.severity);
          }
        } else {
          rt.streak = 0;
        }
        runtime.current.set(rule.id, rt);
      }
    };
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [rules, channels, audible]);

  const addRule = () => {
    const value = Number(draft.value);
    if (!draft.channel || !Number.isFinite(value)) return;
    setRules((r) => [
      ...r,
      {
        id: `${Date.now()}-${draft.channel}`,
        channel: draft.channel,
        op: draft.op,
        value,
        severity: draft.severity,
        debounce: Math.max(1, Number(draft.debounce) || 1),
        cooldownS: Math.max(0, Number(draft.cooldownS) || 0),
        enabled: true,
      },
    ]);
    setDraft((d) => ({ ...d, value: "" }));
  };

  const removeRule = (id: string) => {
    setRules((r) => r.filter((x) => x.id !== id));
    runtime.current.delete(id);
  };

  const toggleRule = (id: string) =>
    setRules((r) => r.map((x) => (x.id === id ? { ...x, enabled: !x.enabled } : x)));

  const now = useNow();
  const recent = history.filter((e) => now - e.at < 60_000);

  return (
    <Panel
      title="Alarms"
      count={`${rules.length} rules`}
      actions={
        <label className="flex items-center gap-1.5 text-[0.76rem] text-fg-dim">
          <input type="checkbox" checked={audible} onChange={(e) => setAudible(e.target.checked)} />
          audible
        </label>
      }
    >
      {recent.length > 0 && (
        <div className="mb-3 rounded-[8px] border border-warn/55 bg-warn/10 px-3 py-2 text-[0.85rem]">
          <b className="text-warn">⚠ {recent.length} threshold alarm(s) firing</b>{" "}
          <span className="text-fg-dim">(operator-set, not an ML anomaly)</span>
        </div>
      )}

      <div className="mb-3 grid grid-cols-[1fr_auto_auto_auto_auto] items-end gap-2 max-[1100px]:grid-cols-1">
        <label className="flex flex-col gap-0.5 text-[0.7rem] text-fg-faint">
          channel
          <input
            className={inputClass}
            list="alarm-channels"
            value={draft.channel}
            onChange={(e) => setDraft({ ...draft, channel: e.target.value })}
            placeholder="obd:COOLANT_TEMP"
          />
          <datalist id="alarm-channels">
            {numericChannels.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
        </label>
        <label className="flex flex-col gap-0.5 text-[0.7rem] text-fg-faint">
          op
          <select
            className={inputClass}
            value={draft.op}
            onChange={(e) => setDraft({ ...draft, op: e.target.value as Op })}
          >
            <option>{">"}</option>
            <option>{"<"}</option>
            <option>{">="}</option>
            <option>{"<="}</option>
          </select>
        </label>
        <label className="flex flex-col gap-0.5 text-[0.7rem] text-fg-faint">
          value
          <input
            className={cn(inputClass, "w-24")}
            value={draft.value}
            onChange={(e) => setDraft({ ...draft, value: e.target.value })}
            inputMode="decimal"
          />
        </label>
        <label className="flex flex-col gap-0.5 text-[0.7rem] text-fg-faint">
          severity
          <select
            className={inputClass}
            value={draft.severity}
            onChange={(e) => setDraft({ ...draft, severity: e.target.value as Severity })}
          >
            <option value="warn">warn</option>
            <option value="critical">critical</option>
          </select>
        </label>
        <button type="button" className={buttonClass} onClick={addRule}>
          Add
        </button>
      </div>

      {rules.length === 0 ? (
        <p className={placeholderClass}>
          no alarms yet — add a threshold above (e.g. obd:COOLANT_TEMP &gt; 110). These fire from
          the live stream, separately from ML anomalies.
        </p>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-1 p-0">
          {rules.map((rule) => {
            const live = channels.get(rule.channel)?.latest.value;
            const breaching = typeof live === "number" && COMPARE[rule.op](live, rule.value);
            return (
              <li
                key={rule.id}
                className={cn(
                  "flex flex-wrap items-center gap-2 rounded-[8px] border px-2.5 py-1.5 text-[0.82rem]",
                  rule.enabled ? "border-border-soft" : "border-border-soft opacity-50",
                  breaching && rule.enabled && rule.severity === "critical" && "border-bad",
                  breaching && rule.enabled && rule.severity === "warn" && "border-warn",
                )}
              >
                <button
                  type="button"
                  className="text-[0.7rem] uppercase tracking-[0.06em] text-fg-dim"
                  onClick={() => toggleRule(rule.id)}
                  aria-label={rule.enabled ? "disable rule" : "enable rule"}
                >
                  {rule.enabled ? "ON" : "OFF"}
                </button>
                <b className="font-num">{rule.channel}</b>
                <span className="font-num text-fg-dim">
                  {rule.op} {rule.value}
                </span>
                <span
                  className={cn(
                    "rounded px-1.5 text-[0.66rem] uppercase",
                    rule.severity === "critical" ? "text-bad" : "text-warn",
                  )}
                >
                  {rule.severity}
                </span>
                <span className="font-num text-[0.74rem] text-fg-faint">
                  debounce {rule.debounce} · cooldown {rule.cooldownS}s
                </span>
                <span className="ml-auto font-num tabular-nums text-fg-dim">
                  {typeof live === "number" ? live.toFixed(2) : "—"}
                  {breaching && rule.enabled && <span className="ml-1 text-bad">breach</span>}
                </span>
                <button
                  type="button"
                  className={cn(dangerButtonClass, "px-2 py-0.5 text-[0.7rem]")}
                  onClick={() => removeRule(rule.id)}
                >
                  ✕
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {history.length > 0 && (
        <>
          <h3 className="mb-1 mt-3 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
            History
          </h3>
          <ol className="m-0 flex max-h-40 list-none flex-col gap-0.5 overflow-y-auto p-0 text-[0.78rem]">
            {history.slice(0, 20).map((e, i) => (
              <li
                key={`${e.ruleId}-${e.at}-${i}`}
                className="flex gap-2 border-b border-border-soft pb-0.5 last:border-0"
              >
                <time className="font-num text-fg-dim">{new Date(e.at).toLocaleTimeString()}</time>
                <span className={e.severity === "critical" ? "text-bad" : "text-warn"}>
                  {e.channel}
                </span>
                <span className="font-num tabular-nums text-fg-dim">= {e.value.toFixed(2)}</span>
              </li>
            ))}
          </ol>
        </>
      )}
    </Panel>
  );
}

function beep(severity: Severity): void {
  try {
    const Ctx =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = severity === "critical" ? 880 : 560;
    gain.gain.value = 0.05;
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.18);
  } catch {
    /* audio not available (autoplay policy / no device) — visual still fires */
  }
}
