// `GET`/`POST /control/update` (phone-ui P3.2). The contract is
// `src/control/updater.py`; the status is written by the root runner
// (`src/control/update_runner.py`) and only read by the daemon.

export type UpdateAction = "check" | "update";

/** `lost`: the runner's heartbeat stopped while it was working. `unknown`:
 *  the status file could not be read. */
export type UpdateState =
  "idle" | "checking" | "updating" | "done" | "failed" | "refused" | "lost" | "unknown";

export interface UpdateCommit {
  sha: string;
  subject: string;
}

export interface UpdateCheck {
  ok: boolean;
  /** `no_internet`, `remote_refused`, `no_upstream` when `ok` is false. */
  reason: string | null;
  message: string | null;
  checked_at: string;
  remote?: string;
  branch?: string;
  current?: string;
  upstream?: string;
  behind?: number;
  ahead?: number;
  commits?: UpdateCommit[];
  commits_truncated?: boolean;
  dirty?: boolean;
  dirty_files?: string[];
}

export interface UpdatePhase {
  name: string;
  at: string;
}

export interface UpdateStatus {
  state: UpdateState;
  action?: UpdateAction | null;
  request_id?: string | null;
  started_at?: string | null;
  updated_at?: string | null;
  finished_at?: string | null;
  phase?: string | null;
  phases?: UpdatePhase[];
  before?: string | null;
  /** What is installed after the run (a rollback puts `before` back). */
  after?: string | null;
  /** The commit the run fetched and tried. */
  target?: string | null;
  exit_code?: number | null;
  exit_meaning?: string | null;
  /** `updated`, `already_current`, `rolled_back`, `rollback_failed`,
   *  `left_broken`, `fetch_failed`, `refused`, `checked`, `failed`, … */
  outcome?: string | null;
  reason?: string | null;
  message?: string | null;
  summary?: Record<string, string>;
  log_tail?: string[];
  error?: string;
}

export interface UpdatePendingRequest {
  action: UpdateAction | null;
  request_id: string | null;
  age_s: number | null;
  /** Not picked up within a minute: `canary-update.path` is not running. */
  stuck: boolean;
  error?: string;
}

export interface UpdateSnapshot {
  installed: { version: string; sha: string | null };
  status: UpdateStatus;
  last_check: UpdateCheck | null;
  pending_request: UpdatePendingRequest | null;
  trigger_installed: boolean;
  internet: { default_route: boolean | null };
  /** What an update would end right now; non-empty refuses an update. */
  blocked_by: string[];
}

export interface UpdateResponse {
  update: UpdateSnapshot;
}

export interface UpdateRequestResponse extends UpdateResponse {
  request: {
    action: UpdateAction;
    request_id: string;
    requested_at: string;
    options: { full: boolean };
  };
}
