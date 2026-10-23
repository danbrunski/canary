import type { DeletedVehicleProfile, VehicleProfile, VehicleProfileDeleteResponse } from "../types";
import { buttonClass, cn, dangerButtonClass, placeholderClass } from "../styles";

/** The in-page confirmation for deleting a profile (never `window.confirm`).
 *  The wording depends on what the delete does: a tracked profile is hidden on
 *  this machine only, a machine-only profile is removed. Either way, recorded
 *  data is not deleted. */
export function DeleteConfirm({
  profile,
  busy,
  onConfirm,
  onCancel,
}: {
  profile: VehicleProfile;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const label = profile.name || profile.slug;
  const tracked = profile.tracked !== false;
  return (
    <div
      role="group"
      aria-label={`Confirm delete ${profile.slug}`}
      className="mt-2 space-y-2 rounded-lg border border-bad/60 p-3 text-sm"
    >
      <p className="font-semibold">Delete {label} from this machine?</p>
      <ul className="list-disc space-y-1 pl-5">
        {tracked ? (
          <>
            <li>
              It is hidden on this machine only. It stays in git, so other machines still have it.
            </li>
            <li>This machine&apos;s edits to it (name, VIN, parameters) are dropped.</li>
            <li>You can restore it afterwards, from the list of hidden profiles.</li>
          </>
        ) : (
          <li>
            It exists only on this machine and is removed from this machine&apos;s registry. It
            cannot be restored.
          </li>
        )}
        <li>
          Recorded data (sessions, models, ledgers) is not deleted or moved. The folders that hold
          it are listed afterwards.
        </li>
      </ul>
      <div className="flex flex-wrap gap-2">
        <button type="button" className={dangerButtonClass} disabled={busy} onClick={onConfirm}>
          Delete profile
        </button>
        <button type="button" className={buttonClass} disabled={busy} onClick={onCancel}>
          Not now
        </button>
      </div>
    </div>
  );
}

/** What a delete left behind: the data stays where it was recorded. */
export function DeletedResult({
  result,
  onDismiss,
}: {
  result: VehicleProfileDeleteResponse;
  onDismiss: () => void;
}) {
  return (
    <div
      className="mb-2 rounded-[8px] border border-border-soft px-3 py-2 text-[0.82rem]"
      data-testid="profile-deleted-result"
    >
      <div className="flex items-start justify-between gap-2">
        <strong>
          {result.tracked
            ? `Deleted ${result.deleted} on this machine (still in git).`
            : `Deleted ${result.deleted} from this machine's registry.`}
        </strong>
        <button type="button" className="text-[0.72rem] text-fg-dim underline" onClick={onDismiss}>
          Dismiss
        </button>
      </div>
      {result.data_left.length > 0 ? (
        <>
          <p className="mt-1">Its recorded data was not deleted. It is still in:</p>
          <ul className="mt-1 list-disc pl-5 font-num text-[0.76rem]">
            {result.data_left.map((path) => (
              <li key={path} className="break-all">
                {path}
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className="mt-1">No recorded data was found for it.</p>
      )}
    </div>
  );
}

/** Tracked profiles this machine deleted, each with a Restore button. */
export function HiddenProfiles({
  profiles,
  busy,
  onRestore,
}: {
  profiles: DeletedVehicleProfile[];
  busy: boolean;
  onRestore: (slug: string) => void;
}) {
  if (profiles.length === 0) return null;
  return (
    <section className="mt-3" aria-label="Hidden on this machine" data-testid="profiles-hidden">
      <h3 className="text-[0.72rem] uppercase tracking-[0.06em] text-fg-faint">
        Hidden on this machine
      </h3>
      <ul className="mt-1 space-y-1">
        {profiles.map((profile) => (
          <li
            key={profile.slug}
            className="flex items-center justify-between gap-2 rounded border border-fg-dim/30 p-2"
            data-testid={`hidden-${profile.slug}`}
          >
            <div className="min-w-0">
              <div className="truncate text-[0.82rem]">{profile.name || profile.slug}</div>
              <div className={cn(placeholderClass, "truncate font-num text-[0.72rem]")}>
                {[profile.year, profile.make, profile.model].filter(Boolean).join(" ") || "—"} ·{" "}
                {profile.slug}
              </div>
            </div>
            <button
              type="button"
              className={cn(buttonClass, "shrink-0 text-xs")}
              disabled={busy}
              onClick={() => onRestore(profile.slug)}
              aria-label={`Restore ${profile.slug}`}
            >
              Restore
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
