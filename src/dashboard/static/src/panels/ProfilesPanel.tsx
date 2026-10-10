import { useCallback, useState } from "react";

import { getProfiles, revertProfile, updateProfileName } from "../api/http";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { ResourceGate } from "../components/ResourceGate";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { useControlResource } from "../hooks/useControlResource";
import type {
  ProfileResolution,
  VehicleParameterField,
  VehicleProfile,
  VehicleProfileEditResponse,
} from "../types";
import { buttonClass, cn, inputClass, placeholderClass } from "../styles";
import { ProfileEditor, parameterText } from "./ProfileEditor";

interface ProfilesPanelProps {
  token: string;
}

/** Fields a revert can put back (the VIN, calibration ID and CVN are what the
 *  machine read off the vehicle). */
function revertable(field: string): boolean {
  return ["name", "make", "model", "year"].includes(field) || field.startsWith("parameters.");
}

const RESOLUTION_TEXT: Record<string, (r: ProfileResolution) => string> = {
  vin_matched: (r) => `this start read VIN ${r.vin_read ?? ""} and matched it to this profile`,
  vin_registered: (r) =>
    `this start read VIN ${r.vin_read ?? ""}, found no profile for it, and registered this one`,
  no_vin_read: () =>
    "this start read no VIN (key off, parked or bench) and fell back to the vehicle: block's profile",
  override: () => "this start was told the profile on the command line (--profile)",
  profiles_disabled: () => "profiles are disabled; the vehicle: block is used",
};

function ParameterRows({
  profile,
  fields,
  pending,
  busy,
  onRevert,
}: {
  profile: VehicleProfile;
  fields: VehicleParameterField[];
  pending: string[];
  busy: boolean;
  onRevert: (field: string) => void;
}) {
  const overrides = profile.overrides ?? [];
  return (
    <dl className="mt-2 grid grid-cols-[auto_1fr_auto] items-baseline gap-x-3 gap-y-1 text-[0.8rem]">
      {fields.map((field) => {
        const key = `parameters.${field.name}`;
        const overridden = overrides.includes(key);
        const restart = pending.includes(key);
        const text = parameterText(profile.parameters[field.name]);
        const evidence = field.kind === "evidence";
        return (
          <div key={field.name} className="contents" data-testid={`param-${field.name}`}>
            <dt className="text-[0.72rem] uppercase tracking-[0.06em] text-fg-faint">
              {field.label}
            </dt>
            <dd
              className={cn(
                "m-0",
                evidence ? "text-[0.76rem] text-fg-dim" : "font-num tabular-nums",
              )}
            >
              {text || "—"}
              {!evidence && text && field.unit ? ` ${field.unit}` : ""}
            </dd>
            <dd className="m-0 flex items-center gap-1 justify-self-end text-[0.68rem]">
              {profile.tracked !== false && (
                <span className={overridden ? "text-accent" : "text-fg-faint"}>
                  {overridden ? "this machine" : "tracked"}
                </span>
              )}
              {restart && <span className="text-warn">restart pending</span>}
              {overridden && profile.tracked !== false && (
                <button
                  type="button"
                  className="text-[0.68rem] text-fg-dim underline"
                  onClick={() => onRevert(key)}
                  disabled={busy}
                  aria-label={`Revert ${field.label}`}
                >
                  revert
                </button>
              )}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

/** Vehicle profiles (multi-vehicle support). Each vehicle the Pi has been
 *  plugged into is identified by its VIN and registered automatically with a
 *  blank display name; the operator names it here ("2008 Xterra") and edits
 *  its identity and vehicle parameters (2026-10-10). The `slug` keys the data
 *  tree, so nothing here moves data. Edits land in this machine's overlay over
 *  the tracked `profiles.yaml`, and reach the daemon at its next start. */
export function ProfilesPanel({ token }: ProfilesPanelProps) {
  const fetcher = useCallback((signal: AbortSignal) => getProfiles(token, signal), [token]);
  const { data, error, loading, refresh } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 30000,
  });

  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");
  const [editorSlug, setEditorSlug] = useState<string | null>(null);
  const { notice, setNotice, busy, run } = useAsyncAction();

  const startEdit = (slug: string, name: string) => {
    setEditingSlug(slug);
    setDraftName(name);
  };

  const onSave = (slug: string) =>
    run("save", async () => {
      await updateProfileName(token, slug, draftName.trim());
      setEditingSlug(null);
      refresh();
    });

  const describeEdit = (result: VehicleProfileEditResponse, what: string): string => {
    const parts = [`${what}: ${result.changed.length ? result.changed.join(", ") : "no change"}.`];
    parts.push(...result.warnings);
    if (result.vin_resolution) parts.push(result.vin_resolution);
    parts.push(result.restart_required ? result.applies : result.active ? "" : result.applies);
    return parts.filter(Boolean).join(" ");
  };

  const onRevert = (slug: string, fields?: string[]) =>
    run(
      "revert",
      async () => {
        const result = await revertProfile(token, slug, fields);
        setNotice({ kind: "ok", text: describeEdit(result, "Reverted") });
        refresh();
      },
      { okText: null },
    );

  const onEdited = (result: VehicleProfileEditResponse) => {
    setEditorSlug(null);
    setNotice({ kind: "ok", text: describeEdit(result, "Saved") });
    refresh();
  };

  return (
    <Panel
      title="Profiles"
      count={data ? `${data.count} vehicle${data.count === 1 ? "" : "s"}` : undefined}
    >
      <ResourceGate
        enabled={Boolean(token)}
        enabledMessage="Enter the control token (Control panel) to view and edit vehicle profiles."
        loading={loading}
        loadingMessage="loading profiles…"
        error={error}
        data={data}
      >
        {(data) => {
          const fields = data.parameter_fields ?? [];
          const pending = data.pending_restart ?? [];
          return (
            <>
              <Notice notice={notice} className="mb-2" />
              {data.restart_required && (
                <div
                  className="mb-2 rounded-[8px] border border-warn/50 bg-warn/10 px-3 py-2 text-[0.82rem]"
                  data-testid="profiles-restart-pending"
                >
                  <strong>Saved, not yet running:</strong> {pending.join(", ")}. {data.applies}
                </div>
              )}
              {data.profiles.length === 0 ? (
                <p className={placeholderClass}>
                  No vehicles registered yet. Plug the Pi into a vehicle with the engine on; the
                  daemon reads its VIN and registers a profile automatically.
                </p>
              ) : (
                <ul className="space-y-2">
                  {data.profiles.map((profile) => {
                    const active = profile.slug === data.active_slug;
                    const editing = editingSlug === profile.slug;
                    const inEditor = editorSlug === profile.slug;
                    const overrides = (profile.overrides ?? []).filter(revertable);
                    const resolution =
                      active && data.resolution ? RESOLUTION_TEXT[data.resolution.how] : undefined;
                    return (
                      <li
                        key={profile.slug}
                        className="rounded border border-fg-dim/40 p-2"
                        data-testid={`profile-${profile.slug}`}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <div className="min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="truncate font-semibold">
                                {profile.name || (
                                  <span className={placeholderClass}>(unnamed)</span>
                                )}
                              </span>
                              {active && (
                                <span className="rounded bg-ok/20 px-1 text-[0.7rem] text-ok">
                                  active
                                </span>
                              )}
                              {profile.tracked === false && (
                                <span className="rounded bg-accent/15 px-1 text-[0.7rem] text-accent">
                                  this machine only
                                </span>
                              )}
                            </div>
                            <div
                              className={cn(placeholderClass, "truncate font-num text-[0.72rem]")}
                            >
                              {[profile.year, profile.make, profile.model]
                                .filter(Boolean)
                                .join(" ") || "—"}
                            </div>
                            <div
                              className={cn(placeholderClass, "truncate font-num text-[0.72rem]")}
                            >
                              VIN {profile.vin || "—"} · {profile.slug}
                            </div>
                            <div
                              className={cn(placeholderClass, "truncate font-num text-[0.72rem]")}
                            >
                              Cal ID {profile.calibration_id || "—"} · CVN {profile.cvn || "—"}
                            </div>
                            {resolution && data.resolution && (
                              <div className={cn(placeholderClass, "text-[0.72rem]")}>
                                Active because {resolution(data.resolution)}.
                              </div>
                            )}
                          </div>
                          {!editing && !inEditor && (
                            <div className="flex shrink-0 gap-1">
                              <button
                                type="button"
                                className={cn(buttonClass, "text-xs")}
                                onClick={() => startEdit(profile.slug, profile.name)}
                                disabled={busy !== null}
                              >
                                {profile.name ? "Rename" : "Name it"}
                              </button>
                              {fields.length > 0 && (
                                <button
                                  type="button"
                                  className={cn(buttonClass, "text-xs")}
                                  onClick={() => setEditorSlug(profile.slug)}
                                  disabled={busy !== null}
                                >
                                  Edit
                                </button>
                              )}
                              {overrides.length > 0 && profile.tracked !== false && (
                                <button
                                  type="button"
                                  className={cn(buttonClass, "text-xs")}
                                  onClick={() => void onRevert(profile.slug)}
                                  disabled={busy !== null}
                                  title={`Back to the tracked values: ${overrides.join(", ")}`}
                                >
                                  Revert all
                                </button>
                              )}
                            </div>
                          )}
                        </div>
                        {editing && (
                          <div className="mt-2 flex items-center gap-2">
                            <input
                              type="text"
                              className={cn(inputClass, "flex-1")}
                              value={draftName}
                              placeholder="e.g. 2008 Xterra"
                              aria-label={`Display name for ${profile.slug}`}
                              autoFocus
                              onChange={(e) => setDraftName(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter") void onSave(profile.slug);
                                if (e.key === "Escape") setEditingSlug(null);
                              }}
                            />
                            <button
                              type="button"
                              className={cn(buttonClass, "text-xs")}
                              onClick={() => onSave(profile.slug)}
                              disabled={busy !== null}
                            >
                              Save
                            </button>
                            <button
                              type="button"
                              className={cn(buttonClass, "text-xs")}
                              onClick={() => setEditingSlug(null)}
                              disabled={busy !== null}
                            >
                              Cancel
                            </button>
                          </div>
                        )}
                        {inEditor ? (
                          <ProfileEditor
                            token={token}
                            profile={profile}
                            fields={fields}
                            active={active}
                            resolution={data.resolution ?? null}
                            onSaved={onEdited}
                            onCancel={() => setEditorSlug(null)}
                          />
                        ) : (
                          fields.length > 0 && (
                            <ParameterRows
                              profile={profile}
                              fields={fields}
                              pending={active ? pending : []}
                              busy={busy !== null}
                              onRevert={(field) => void onRevert(profile.slug, [field])}
                            />
                          )
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </>
          );
        }}
      </ResourceGate>
    </Panel>
  );
}
