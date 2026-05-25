// Background job kinds the control plane accepts. Declared once so the
// Control and Training panels can't silently drift (they previously kept
// two hand-edited arrays — Training's just omitted `corpus_report`).

/** Every job kind the control plane can submit. */
export const JOB_KINDS = [
  "corpus_report",
  "retrain_if",
  "retrain_lstm",
  "retrain_tcn",
  "retrain_all",
  "refit_behavior_models",
];

/** The subset offered in the Training panel — `corpus_report` is a report,
 *  not a training run. */
export const TRAINING_JOB_KINDS = JOB_KINDS.filter((k) => k !== "corpus_report");
