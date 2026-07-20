import { useCallback, useRef, useState } from "react";

import { addCustomPid, deleteCustomPid, getCustomPids, importCustomPidsCsv } from "../api/http";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { useControlResource } from "../hooks/useControlResource";
import { buttonClass, cn, dangerButtonClass, inputClass, placeholderClass, selectClass } from "../styles";
import type { CustomPid } from "../types";

interface CustomPidsPanelProps {
  token: string;
}

const POLL_TIERS: CustomPid["poll_tier"][] = ["fast", "medium", "slow", "on_demand"];

/** Parse a hex byte/word field (``"22"``, ``"0x22"``) — Mode/PID/header are
 *  always written in hex in Torque/service-manual convention, so a bare
 *  decimal-looking string like "22" must still mean 0x22, not 22. */
function parseHex(text: string): number | null {
  const cleaned = text.trim().replace(/^0x/i, "");
  if (!cleaned) return null;
  const n = Number.parseInt(cleaned, 16);
  return Number.isFinite(n) ? n : null;
}

const EMPTY_FORM = {
  name: "",
  displayName: "",
  mode: "22",
  pid: "",
  formula: "",
  unit: "",
  pollTier: "medium" as CustomPid["poll_tier"],
};

/** Custom-PID management (Task 2.8c) — the write-side companion to Task
 *  2.6's read-only startup loader. List/add/delete `data/custom_pids.yaml`
 *  entries plus a Torque Pro CSV import. Edits here land on disk immediately
 *  but only take effect for the live scheduler on the *next* daemon restart
 *  (2.6 loads the file once at startup; no hot-reload path exists for it,
 *  unlike `behavior_models.yaml`). */
export function CustomPidsPanel({ token }: CustomPidsPanelProps) {
  const fetcher = useCallback((signal: AbortSignal) => getCustomPids(token, signal), [token]);
  const { data, error, loading, refresh } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 15000,
  });

  const { notice, setNotice, busy, run } = useAsyncAction();
  const [form, setForm] = useState(EMPTY_FORM);
  const [importing, setImporting] = useState(false);
  const [importText, setImportText] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  if (!token) {
    return (
      <Panel title="Custom PIDs">
        <p className={placeholderClass}>
          Enter the control token (Control panel) to manage custom PIDs.
        </p>
      </Panel>
    );
  }
  if (loading) {
    return (
      <Panel title="Custom PIDs">
        <p className={placeholderClass}>loading custom PIDs…</p>
      </Panel>
    );
  }
  if (error || !data) {
    return (
      <Panel title="Custom PIDs">
        <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>
      </Panel>
    );
  }

  const addPid = () =>
    run(
      "add custom PID",
      async () => {
        const mode = parseHex(form.mode);
        const pid = parseHex(form.pid);
        if (!form.name.trim() || !form.displayName.trim() || !form.formula.trim()) {
          throw new Error("name, display name, and formula are required.");
        }
        if (mode == null || pid == null) {
          throw new Error("mode and PID must be hex (e.g. 22, 0C40).");
        }
        const pidDef: CustomPid = {
          name: form.name.trim(),
          display_name: form.displayName.trim(),
          mode,
          pid,
          formula: form.formula.trim(),
          unit: form.unit.trim(),
          category: "custom",
          minimum: null,
          maximum: null,
          poll_tier: form.pollTier,
          dashboard_hint: "graph",
          header: null,
        };
        await addCustomPid(token, pidDef);
        setForm(EMPTY_FORM);
      },
      { then: refresh, okText: `Added ${form.name.trim()}.` },
    );

  const removePid = (name: string) =>
    run(
      "delete custom PID",
      async () => {
        if (!window.confirm(`Delete custom PID ${name}?`)) return;
        await deleteCustomPid(token, name);
      },
      { then: refresh, okText: `Deleted ${name}.` },
    );

  const doImport = () =>
    run(
      "import CSV",
      async () => {
        const res = await importCustomPidsCsv(token, importText);
        setImporting(false);
        setImportText("");
        const summary = `Imported ${res.added_count} PID${res.added_count === 1 ? "" : "s"}.`;
        setNotice({
          kind: "ok",
          text:
            res.warnings.length > 0
              ? `${summary} ${res.warnings.length} skipped: ${res.warnings.join("; ")}`
              : summary,
        });
      },
      { then: refresh, okText: null },
    );

  return (
    <Panel title="Custom PIDs" count={`${data.count} defined`}>
      <Notice notice={notice} className="mb-3" />

      {data.custom_pids.length === 0 ? (
        <p className={placeholderClass}>
          No custom PIDs defined yet — add one below or import a Torque Pro CSV.
        </p>
      ) : (
        <ul className="mb-4 flex flex-col gap-1.5">
          {data.custom_pids.map((p) => (
            <li
              key={p.name}
              className="flex items-center justify-between gap-2 rounded border border-border-soft px-2.5 py-1.5 text-[0.84rem]"
              data-testid={`custom-pid-${p.name}`}
            >
              <div className="min-w-0">
                <div className="truncate font-semibold text-fg">{p.display_name}</div>
                <div className="truncate font-num text-[0.72rem] text-fg-faint">
                  {p.name} · mode 0x{p.mode.toString(16).toUpperCase()} pid 0x
                  {p.pid.toString(16).toUpperCase()} · {p.formula}
                  {p.unit ? ` (${p.unit})` : ""}
                </div>
              </div>
              <button
                type="button"
                className={cn(dangerButtonClass, "shrink-0 px-2.5 py-1 text-[0.74rem]")}
                onClick={() => removePid(p.name)}
                disabled={busy !== null}
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}

      <h3 className="mb-1.5 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
        Add a custom PID
      </h3>
      <div className="mb-4 grid grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] gap-2">
        <input
          className={inputClass}
          placeholder="Name (channel key)"
          value={form.name}
          onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
          aria-label="Custom PID name"
        />
        <input
          className={inputClass}
          placeholder="Display name"
          value={form.displayName}
          onChange={(e) => setForm((f) => ({ ...f, displayName: e.target.value }))}
          aria-label="Custom PID display name"
        />
        <input
          className={inputClass}
          placeholder="Mode (hex, e.g. 22)"
          value={form.mode}
          onChange={(e) => setForm((f) => ({ ...f, mode: e.target.value }))}
          aria-label="Custom PID mode"
        />
        <input
          className={inputClass}
          placeholder="PID (hex, e.g. 0C40)"
          value={form.pid}
          onChange={(e) => setForm((f) => ({ ...f, pid: e.target.value }))}
          aria-label="Custom PID number"
        />
        <input
          className={cn(inputClass, "col-span-2")}
          placeholder="Formula (A, B, … over response bytes)"
          value={form.formula}
          onChange={(e) => setForm((f) => ({ ...f, formula: e.target.value }))}
          aria-label="Custom PID formula"
        />
        <input
          className={inputClass}
          placeholder="Unit"
          value={form.unit}
          onChange={(e) => setForm((f) => ({ ...f, unit: e.target.value }))}
          aria-label="Custom PID unit"
        />
        <select
          className={selectClass}
          value={form.pollTier}
          onChange={(e) =>
            setForm((f) => ({ ...f, pollTier: e.target.value as CustomPid["poll_tier"] }))
          }
          aria-label="Custom PID poll tier"
        >
          {POLL_TIERS.map((tier) => (
            <option key={tier} value={tier}>
              {tier}
            </option>
          ))}
        </select>
      </div>
      <button
        type="button"
        className={cn(buttonClass, "mb-4")}
        onClick={addPid}
        disabled={busy !== null}
      >
        {busy === "add custom PID" ? "Adding…" : "Add custom PID"}
      </button>

      <h3 className="mb-1.5 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
        Torque Pro CSV import
      </h3>
      {!importing ? (
        <button
          type="button"
          className={buttonClass}
          onClick={() => setImporting(true)}
        >
          Import CSV…
        </button>
      ) : (
        <div className="flex flex-col gap-2">
          <textarea
            className={cn(inputClass, "h-28 w-full resize-y font-num text-[0.78rem]")}
            value={importText}
            placeholder="paste a Torque Pro custom-PID CSV export…"
            onChange={(e) => setImportText(e.target.value)}
            aria-label="Torque CSV text"
          />
          <div className="flex gap-2">
            <button
              type="button"
              className={buttonClass}
              onClick={doImport}
              disabled={busy !== null || !importText.trim()}
            >
              {busy === "import CSV" ? "Importing…" : "Import"}
            </button>
            <input
              ref={fileRef}
              type="file"
              accept=".csv,text/csv"
              className="hidden"
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (f) setImportText(await f.text());
              }}
            />
            <button type="button" className={buttonClass} onClick={() => fileRef.current?.click()}>
              From file…
            </button>
            <button type="button" className={buttonClass} onClick={() => setImporting(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </Panel>
  );
}
