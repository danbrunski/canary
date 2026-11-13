import { Fragment, useMemo, useState } from "react";

import { HttpError, readVin, updateProfile } from "../api/http";
import type {
  ProfileResolution,
  VehicleParameterField,
  VehicleProfile,
  VehicleProfileEditResponse,
  VehicleProfileUpdate,
  VinReadResult,
} from "../types";
import { buttonClass, cn, inputClass, placeholderClass } from "../styles";
import { COMMON_TIRE_SIZES, parseTireSize } from "../util/tireSize";
import { checkVin } from "../util/vin";

/** The examples the daemon's evidence refusal quotes (src/vehicle/profiles/). */
const EVIDENCE_EXAMPLES: Record<string, string> = {
  mass_evidence: "scale ticket 2026-10-10, CAT scale, full fuel, no occupants",
  driveline_evidence: "final drive from the axle tag; ratios from the gearbox manual",
};

/** A parameter's value as the form edits it: text, parsed on save. */
export function parameterText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

/** Parse one form field back to the value the API takes, or an error. */
export function parseParameter(
  field: VehicleParameterField,
  text: string,
): { value: unknown } | { error: string } {
  const trimmed = text.trim();
  if (field.kind === "evidence") return { value: trimmed || null };
  if (field.kind === "choice") return { value: trimmed };
  if (field.kind === "ratios") {
    if (!trimmed) return { value: [] };
    const parts = trimmed
      .split(/[,\s]+/)
      .filter(Boolean)
      .map(Number);
    if (parts.some((n) => !Number.isFinite(n) || n <= 0)) {
      return { error: `${field.label}: positive numbers separated by commas` };
    }
    return { value: parts };
  }
  if (!trimmed) return { value: null };
  const n = Number(trimmed);
  if (!Number.isFinite(n)) return { error: `${field.label}: not a number` };
  return { value: n };
}

function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/** The refusal text inside an `HttpError`'s JSON body, in full (the shared
 *  notice truncates it, and an evidence refusal is the whole instruction). */
export function refusalText(err: unknown): string | null {
  if (!(err instanceof HttpError)) return null;
  try {
    const parsed: unknown = JSON.parse(err.body);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = parsed.detail;
      if (typeof detail === "string") return detail;
    }
  } catch {
    /* not JSON */
  }
  return err.body || null;
}

interface ProfileEditorProps {
  token: string;
  profile: VehicleProfile;
  fields: VehicleParameterField[];
  active: boolean;
  resolution: ProfileResolution | null;
  onSaved: (result: VehicleProfileEditResponse) => void;
  onCancel: () => void;
}

/** Edit one profile's identity and vehicle parameters. A changed vehicle
 *  fact asks for new evidence before it can be saved, the same rule the
 *  daemon enforces (tire size is not one: a tire swap saves on its own); the
 *  VIN can be read off the connected vehicle or typed. A tire size picked or
 *  typed as on the sidewall fills in the tire diameter. */
export function ProfileEditor({
  token,
  profile,
  fields,
  active,
  resolution,
  onSaved,
  onCancel,
}: ProfileEditorProps) {
  const [make, setMake] = useState(profile.make);
  const [model, setModel] = useState(profile.model);
  const [year, setYear] = useState(profile.year === null ? "" : String(profile.year));
  const [vin, setVin] = useState(profile.vin);
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(fields.map((f) => [f.name, parameterText(profile.parameters[f.name])])),
  );
  const [tireSize, setTireSize] = useState("");
  const tire = parseTireSize(tireSize);
  const [vinRead, setVinRead] = useState<VinReadResult | null>(null);
  const [reading, setReading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const parsed = useMemo(() => {
    const out: Record<string, unknown> = {};
    const errors: string[] = [];
    for (const field of fields) {
      const result = parseParameter(field, values[field.name] ?? "");
      if ("error" in result) errors.push(result.error);
      else out[field.name] = result.value;
    }
    return { out, errors };
  }, [fields, values]);

  const changedParams = useMemo(
    () =>
      fields
        .filter((f) => !sameValue(parsed.out[f.name], profile.parameters[f.name]))
        .map((f) => f.name),
    [fields, parsed, profile.parameters],
  );

  /** Evidence fields owed: a fact in its unit changed, and the text did not. */
  const evidenceOwed = useMemo(() => {
    const owed: Record<string, string[]> = {};
    for (const field of fields) {
      if (field.kind !== "evidence") continue;
      const moved = field.evidence_for.filter((name) => changedParams.includes(name));
      if (!moved.length) continue;
      const clearedDriveline =
        field.name === "driveline_evidence" &&
        moved.every((name) => {
          const v = parsed.out[name];
          return v === null || (Array.isArray(v) && v.length === 0);
        });
      if (clearedDriveline) continue;
      const text = parsed.out[field.name];
      const fresh = typeof text === "string" && text && text !== profile.parameters[field.name];
      if (!fresh) owed[field.name] = moved;
    }
    return owed;
  }, [fields, changedParams, parsed, profile.parameters]);

  const vinStatus = checkVin(vin);
  const vinChanged = vin.trim().toUpperCase() !== profile.vin;
  const vinBlocker =
    vinChanged && (vinStatus.kind === "malformed" || vinStatus.kind === "refused")
      ? vinStatus.message
      : null;
  const readLocked = active && resolution?.vin_read ? resolution.vin_read : null;

  const blockers = [
    ...parsed.errors,
    ...Object.entries(evidenceOwed).map(
      ([evidence, moved]) => `${moved.join(", ")} changed: give a new ${evidence}.`,
    ),
    ...(vinBlocker ? [vinBlocker] : []),
  ];

  const buildUpdate = (): VehicleProfileUpdate => {
    const update: VehicleProfileUpdate = {};
    if (make.trim() !== profile.make) update.make = make.trim();
    if (model.trim() !== profile.model) update.model = model.trim();
    const yearValue = year.trim() ? Number(year) : null;
    if (yearValue !== profile.year) update.year = yearValue;
    if (vinChanged) update.vin = vin.trim().toUpperCase();
    if (changedParams.length) {
      update.parameters = Object.fromEntries(changedParams.map((n) => [n, parsed.out[n]]));
    }
    return update;
  };

  const onRead = async () => {
    setReading(true);
    setVinRead(null);
    try {
      const { vin_read } = await readVin(token);
      setVinRead(vin_read);
      if (vin_read.ok && vin_read.vin) setVin(vin_read.vin);
    } catch (err) {
      setVinRead({
        ok: false,
        vin: null,
        refusal: "error",
        detail: refusalText(err) ?? (err instanceof Error ? err.message : String(err)),
        raw: null,
        negative_response_code: null,
      });
    } finally {
      setReading(false);
    }
  };

  const onSave = async () => {
    const update = buildUpdate();
    if (!Object.keys(update).length) {
      onCancel();
      return;
    }
    setSaving(true);
    setError(null);
    try {
      onSaved(await updateProfile(token, profile.slug, update));
    } catch (err) {
      setError(refusalText(err) ?? (err instanceof Error ? err.message : String(err)));
    } finally {
      setSaving(false);
    }
  };

  const evidenceFor = (name: string) => evidenceOwed[name];

  const onTireSize = (text: string) => {
    setTireSize(text);
    const parsed = parseTireSize(text);
    if (parsed.kind === "ok") {
      setValues((v) => ({ ...v, tire_diameter_m: String(parsed.diameterM) }));
    }
  };

  const tireSizePicker = (
    <label key="tire-size" className="flex flex-col gap-1">
      Tire size (fills in the diameter)
      <input
        className={cn(inputClass, "font-num")}
        aria-label="Tire size"
        list={`tire-sizes-${profile.slug}`}
        value={tireSize}
        placeholder="e.g. 285/75R16 or 33x12.50R15"
        onChange={(e) => onTireSize(e.target.value)}
      />
      <datalist id={`tire-sizes-${profile.slug}`}>
        {COMMON_TIRE_SIZES.map((size) => (
          <option key={size} value={size} />
        ))}
      </datalist>
      {tire.kind === "ok" && (
        <span className="text-fg-dim" data-testid="tire-size-result">
          {tire.size}: {tire.diameterM.toFixed(3)} m diameter, {tire.circumferenceM.toFixed(3)} m
          circumference, new and unloaded. The loaded circumference is a little smaller; each drive
          measures it from GPS.
        </span>
      )}
      {tire.kind === "invalid" && <span className="text-warn">{tire.message}</span>}
    </label>
  );

  return (
    <div className="mt-2 space-y-3 text-[0.82rem]" data-testid={`profile-editor-${profile.slug}`}>
      <div className="grid grid-cols-3 gap-2 max-[600px]:grid-cols-1">
        <label className="flex flex-col gap-1">
          Make
          <input className={inputClass} value={make} onChange={(e) => setMake(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1">
          Model
          <input className={inputClass} value={model} onChange={(e) => setModel(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1">
          Year
          <input
            className={inputClass}
            inputMode="numeric"
            value={year}
            onChange={(e) => setYear(e.target.value)}
          />
        </label>
      </div>

      <div className="space-y-1">
        <label className="flex flex-col gap-1">
          VIN
          <div className="flex gap-2">
            <input
              className={cn(inputClass, "flex-1 font-num uppercase")}
              aria-label={`VIN for ${profile.slug}`}
              value={vin}
              maxLength={17}
              placeholder="17 characters, from the plate or the read button"
              onChange={(e) => setVin(e.target.value)}
            />
            <button
              type="button"
              className={cn(buttonClass, "shrink-0 text-xs")}
              onClick={() => void onRead()}
              disabled={reading || saving}
            >
              {reading ? "Reading…" : "Read VIN from vehicle"}
            </button>
          </div>
        </label>
        {vinStatus.kind !== "empty" && (
          <p
            className={cn(
              "m-0",
              vinStatus.kind === "pass" && "text-ok",
              vinStatus.kind === "warning" && "text-warn",
              (vinStatus.kind === "malformed" || vinStatus.kind === "refused") && "text-bad",
            )}
            data-testid="vin-check"
          >
            {vinStatus.message}
          </p>
        )}
        {vinRead && (
          <p className={cn("m-0", vinRead.ok ? "text-ok" : "text-bad")} data-testid="vin-read">
            {vinRead.ok
              ? `Read ${vinRead.vin ?? ""} from the connected vehicle — save to keep it.`
              : `Could not read the VIN (${vinRead.refusal ?? "error"}): ${vinRead.detail}. Type it in instead.`}
            {vinRead.held_by && vinRead.held_by !== profile.slug
              ? ` That VIN already belongs to profile ${vinRead.held_by}.`
              : ""}
          </p>
        )}
        {readLocked ? (
          <p className={cn(placeholderClass, "not-italic")}>
            This profile is active because the vehicle reported {readLocked} at startup. Its VIN
            stays {readLocked}: another VIN would make the next start register a new, empty profile
            for this vehicle, so the daemon refuses it.
          </p>
        ) : (
          vinChanged && (
            <p className={cn(placeholderClass, "not-italic")}>
              The VIN chooses the profile at the next daemon start: this profile is used only when
              the vehicle reports this VIN. A vehicle reporting another VIN gets that VIN&apos;s
              profile, or a new one. Nothing switches until a restart.
            </p>
          )
        )}
      </div>

      <div className="grid grid-cols-2 gap-2 max-[600px]:grid-cols-1">
        {fields.map((field) => {
          const owed = evidenceFor(field.name);
          const label = field.unit ? `${field.label} (${field.unit})` : field.label;
          const common = {
            "aria-label": field.label,
            value: values[field.name] ?? "",
          };
          const control = (
            <label
              key={field.name}
              className={cn("flex flex-col gap-1", field.kind === "evidence" && "col-span-full")}
            >
              {label}
              {field.kind === "evidence" ? (
                <textarea
                  {...common}
                  className={cn(inputClass, "min-h-16", owed && "border-warn")}
                  onChange={(e) => setValues((v) => ({ ...v, [field.name]: e.target.value }))}
                />
              ) : field.kind === "choice" ? (
                <select
                  {...common}
                  className={inputClass}
                  onChange={(e) => setValues((v) => ({ ...v, [field.name]: e.target.value }))}
                >
                  {(field.choices ?? []).map((choice) => (
                    <option key={choice} value={choice}>
                      {choice}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  {...common}
                  className={inputClass}
                  inputMode={field.kind === "number" ? "decimal" : "text"}
                  onChange={(e) => setValues((v) => ({ ...v, [field.name]: e.target.value }))}
                />
              )}
              {owed && (
                <span className="text-warn">
                  {owed.join(", ")} changed: say how the new value was arrived at (for example
                  &ldquo;{EVIDENCE_EXAMPLES[field.name] ?? "how it was measured"}&rdquo;).
                </span>
              )}
            </label>
          );
          return field.name === "tire_diameter_m" ? (
            <Fragment key={field.name}>
              {tireSizePicker}
              {control}
            </Fragment>
          ) : (
            control
          );
        })}
      </div>

      {blockers.length > 0 && (
        <ul className="m-0 list-disc pl-5 text-bad" data-testid="profile-editor-blockers">
          {blockers.map((b) => (
            <li key={b}>{b}</li>
          ))}
        </ul>
      )}
      {error && (
        <p className="m-0 text-bad" role="alert">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          className={cn(buttonClass, "text-xs")}
          onClick={() => void onSave()}
          disabled={saving || blockers.length > 0}
        >
          {saving ? "Saving…" : "Save changes"}
        </button>
        <button
          type="button"
          className={cn(buttonClass, "text-xs")}
          onClick={onCancel}
          disabled={saving}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
