import { useCallback, useState } from "react";

import { deleteVehicleEvent, getVehicleLedger, saveVehicleEvent } from "../api/http";
import { Panel } from "../components/Panel";
import { useControlResource } from "../hooks/useControlResource";
import {
  buttonClass,
  cn,
  dangerButtonClass,
  inputClass,
  placeholderClass,
  selectClass,
} from "../styles";
import type {
  PhysicalInterventionKind,
  VehicleEventKind,
  VehicleLedgerDraft,
  VehicleLedgerEvent,
  VehicleLedgerTires,
} from "../types";
import { TokenRequired } from "../components/TokenRequired";

const KINDS: VehicleEventKind[] = ["maintenance", "mod", "repair", "observation", "dtc"];

interface VehicleLedgerPageProps {
  token: string;
}

/** The form's state: every field as typed, so a half-typed number is kept. */
export interface LedgerForm {
  eventId?: string;
  occurredAt: string;
  kind: VehicleEventKind;
  title: string;
  details: string;
  odometerKm: string;
  affectedChannels: string;
  corpusBoundary: boolean;
  tires: VehicleLedgerTires | null;
  tireValue: string;
  /** E01.4 classification, carried through an edit (no form control yet). */
  intervention?: PhysicalInterventionKind | null;
}

export function emptyForm(today: string): LedgerForm {
  return {
    occurredAt: today,
    kind: "maintenance",
    title: "",
    details: "",
    odometerKm: "",
    affectedChannels: "",
    corpusBoundary: false,
    tires: null,
    tireValue: "",
  };
}

export function formFromEvent(event: VehicleLedgerEvent): LedgerForm {
  const tires = event.tires;
  const tireValue =
    tires?.action === "tread_measured"
      ? String(tires.tread_depth_mm ?? "")
      : tires?.action === "pressure_changed"
        ? String(tires.pressure_psi ?? "")
        : "";
  return {
    eventId: event.id,
    occurredAt: event.occurred_at,
    kind: event.kind,
    title: event.title,
    details: event.details,
    odometerKm: event.odometer_km === null ? "" : String(event.odometer_km),
    affectedChannels: event.affected_channels.join(", "),
    corpusBoundary: event.corpus_boundary,
    tires,
    tireValue,
    intervention: event.intervention ?? null,
  };
}

/** The request body, or the reason the form cannot be saved yet. */
export function draftFromForm(form: LedgerForm): VehicleLedgerDraft | string {
  if (!form.title.trim()) return "a title is required";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(form.occurredAt)) return "the date must be YYYY-MM-DD";
  const odometer = form.odometerKm.trim() === "" ? null : Number(form.odometerKm);
  if (odometer !== null && !(odometer >= 0)) return "the odometer must be a number";
  let tires = form.tires;
  if (tires && (tires.action === "tread_measured" || tires.action === "pressure_changed")) {
    const value = Number(form.tireValue);
    if (!(value > 0)) {
      return tires.action === "tread_measured"
        ? "the tread depth must be a number of mm"
        : "the pressure must be a number of psi";
    }
    tires =
      tires.action === "tread_measured"
        ? { ...tires, tread_depth_mm: value }
        : { ...tires, pressure_psi: value };
  }
  return {
    ...(form.eventId ? { event_id: form.eventId } : {}),
    occurred_at: form.occurredAt,
    kind: form.kind,
    title: form.title.trim(),
    details: form.details,
    odometer_km: odometer,
    affected_channels: form.affectedChannels
      .split(",")
      .map((c) => c.trim())
      .filter(Boolean),
    corpus_boundary: form.corpusBoundary,
    tires,
    ...(form.intervention ? { intervention: form.intervention } : {}),
  };
}

/** A tire fact in words, for the list. */
export function tireFact(tires: VehicleLedgerTires | null): string | null {
  if (!tires) return null;
  switch (tires.action) {
    case "tread_measured":
      return `tread ${tires.tread_depth_mm} mm`;
    case "pressure_changed":
      return `pressure ${tires.pressure_psi} psi`;
    case "rotated":
      return "tires rotated";
    case "replaced":
      return "tires replaced";
  }
}

/** Every vehicle ledger entry, newest first, each editable and deletable.
 *  An edit replaces the entry on disk and a delete removes it, so a typo or
 *  a wrong reading does not linger under its correction (2026-10-04). */
export function VehicleLedgerPage({ token }: VehicleLedgerPageProps) {
  const fetcher = useCallback((signal: AbortSignal) => getVehicleLedger(token, signal), [token]);
  const { data, error, loading, refresh } = useControlResource(fetcher, {
    enabled: Boolean(token),
  });
  const [form, setForm] = useState<LedgerForm | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  if (!token) {
    return (
      <Panel title="Vehicle ledger">
        <TokenRequired purpose="view the vehicle ledger" />
      </Panel>
    );
  }

  const run = async (work: () => Promise<unknown>) => {
    setBusy(true);
    setActionError(null);
    try {
      await work();
      setForm(null);
      refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    if (!form) return;
    const draft = draftFromForm(form);
    if (typeof draft === "string") {
      setActionError(draft);
      return;
    }
    void run(() => saveVehicleEvent(token, draft));
  };

  const remove = (event: VehicleLedgerEvent) => {
    if (!window.confirm(`Delete "${event.title}" (${event.occurred_at})? This cannot be undone.`)) {
      return;
    }
    void run(() => deleteVehicleEvent(token, event.id));
  };

  const events = [...(data?.events ?? [])].reverse();
  return (
    <Panel title="Vehicle ledger" count={data ? `${data.count} entries` : undefined}>
      <div className="mb-3 flex items-center gap-2">
        <button
          type="button"
          className={buttonClass}
          disabled={busy || form !== null}
          onClick={() => setForm(emptyForm(new Date().toISOString().slice(0, 10)))}
        >
          Add entry
        </button>
        {actionError && <span className="text-[0.82rem] text-bad">{actionError}</span>}
      </div>
      {form && !form.eventId && (
        <LedgerFormView
          form={form}
          busy={busy}
          onChange={setForm}
          onSave={save}
          onCancel={() => setForm(null)}
        />
      )}
      {loading && !data && <p className={placeholderClass}>loading…</p>}
      {error && <p className={cn(placeholderClass, "text-bad")}>{error}</p>}
      {data && events.length === 0 && <p className={placeholderClass}>no entries yet</p>}
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {events.map((event) =>
          form?.eventId === event.id ? (
            <li key={event.id}>
              <LedgerFormView
                form={form}
                busy={busy}
                onChange={setForm}
                onSave={save}
                onCancel={() => setForm(null)}
              />
            </li>
          ) : (
            <li
              key={event.id}
              className="flex items-start justify-between gap-3 border-b border-border pb-2"
            >
              <div className="min-w-0 text-[0.86rem]">
                <div>
                  <span className="font-num text-fg-dim">{event.occurred_at}</span>{" "}
                  <span className="text-[0.72rem] uppercase tracking-[0.08em] text-fg-faint">
                    {event.kind}
                  </span>{" "}
                  {event.title}
                  {event.corpus_boundary && (
                    <span className="text-[0.72rem] text-fg-faint"> · corpus boundary</span>
                  )}
                </div>
                {(event.details || event.tires || event.odometer_km !== null) && (
                  <div className="text-[0.8rem] text-fg-dim">
                    {[
                      tireFact(event.tires),
                      event.odometer_km !== null ? `${event.odometer_km} km` : null,
                      event.details || null,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </div>
                )}
              </div>
              <div className="flex shrink-0 gap-1.5">
                <button
                  type="button"
                  className={buttonClass}
                  disabled={busy || form !== null}
                  onClick={() => setForm(formFromEvent(event))}
                >
                  Edit
                </button>
                <button
                  type="button"
                  className={dangerButtonClass}
                  disabled={busy || form !== null}
                  onClick={() => remove(event)}
                >
                  Delete
                </button>
              </div>
            </li>
          ),
        )}
      </ul>
    </Panel>
  );
}

interface LedgerFormViewProps {
  form: LedgerForm;
  busy: boolean;
  onChange: (form: LedgerForm) => void;
  onSave: () => void;
  onCancel: () => void;
}

function LedgerFormView({ form, busy, onChange, onSave, onCancel }: LedgerFormViewProps) {
  const set = (patch: Partial<LedgerForm>) => onChange({ ...form, ...patch });
  const tireLabel =
    form.tires?.action === "tread_measured"
      ? "tread depth (mm)"
      : form.tires?.action === "pressure_changed"
        ? "pressure (psi)"
        : null;
  return (
    <div className="mb-3 grid gap-2 rounded border border-border p-2 text-[0.82rem]">
      <div className="flex flex-wrap gap-2">
        <input
          aria-label="date"
          type="date"
          className={inputClass}
          value={form.occurredAt}
          onChange={(e) => set({ occurredAt: e.target.value })}
        />
        <select
          aria-label="kind"
          className={selectClass}
          value={form.kind}
          onChange={(e) => set({ kind: e.target.value as VehicleEventKind })}
        >
          {KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {kind}
            </option>
          ))}
        </select>
        <input
          aria-label="odometer (km)"
          placeholder="odometer km"
          inputMode="decimal"
          className={cn(inputClass, "w-32")}
          value={form.odometerKm}
          onChange={(e) => set({ odometerKm: e.target.value })}
        />
        {tireLabel && (
          <input
            aria-label={tireLabel}
            placeholder={tireLabel}
            inputMode="decimal"
            className={cn(inputClass, "w-36")}
            value={form.tireValue}
            onChange={(e) => set({ tireValue: e.target.value })}
          />
        )}
      </div>
      <input
        aria-label="title"
        placeholder="title"
        className={inputClass}
        value={form.title}
        onChange={(e) => set({ title: e.target.value })}
      />
      <textarea
        aria-label="details"
        placeholder="details"
        rows={2}
        className={inputClass}
        value={form.details}
        onChange={(e) => set({ details: e.target.value })}
      />
      <input
        aria-label="affected channels"
        placeholder="affected channels, comma-separated (e.g. obd:MAF)"
        className={inputClass}
        value={form.affectedChannels}
        onChange={(e) => set({ affectedChannels: e.target.value })}
      />
      <label className="flex items-center gap-1.5">
        <input
          type="checkbox"
          checked={form.corpusBoundary}
          onChange={(e) => set({ corpusBoundary: e.target.checked })}
        />
        corpus boundary (normal behaviour changes here: a mod or repair)
      </label>
      <div className="flex gap-2">
        <button type="button" className={buttonClass} disabled={busy} onClick={onSave}>
          {busy ? "saving…" : form.eventId ? "Save changes" : "Add"}
        </button>
        <button type="button" className={buttonClass} disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
