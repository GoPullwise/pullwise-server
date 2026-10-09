-- Rolling activity is a separate projection. Preserve the original financial,
-- expense and workspace audit history; no unbounded historical backfill.
CREATE TABLE ledger_activity_events (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL CHECK (length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 160),
  owner_id TEXT NOT NULL,
  target_kind TEXT NOT NULL CHECK (target_kind IN ('project', 'shared')),
  project_id TEXT,
  actor_json TEXT NOT NULL CHECK (
    json_valid(actor_json) AND json_type(actor_json) = 'object'
    AND length(CAST(actor_json AS BLOB)) <= 2048
  ),
  resource_kind TEXT NOT NULL CHECK (resource_kind IN ('expense', 'project', 'recurring_rule')),
  resource_id TEXT NOT NULL CHECK (length(CAST(resource_id AS BLOB)) BETWEEN 1 AND 160),
  action TEXT NOT NULL CHECK (action IN (
    'create', 'update', 'delete', 'archive', 'restore', 'pause', 'resume',
    'cancel', 'move', 'generate'
  )),
  before_json TEXT CHECK (before_json IS NULL OR (
    json_valid(before_json) AND json_type(before_json) = 'object'
    AND length(CAST(before_json AS BLOB)) <= 16384
  )),
  after_json TEXT CHECK (after_json IS NULL OR (
    json_valid(after_json) AND json_type(after_json) = 'object'
    AND length(CAST(after_json AS BLOB)) <= 16384
  )),
  created_at TEXT NOT NULL,
  CHECK ((target_kind = 'shared' AND project_id IS NULL) OR
         (target_kind = 'project' AND project_id IS NOT NULL))
);
CREATE INDEX ledger_activity_events_owner_target_time
  ON ledger_activity_events(owner_id, target_kind, project_id, created_at DESC, id DESC);
CREATE INDEX ledger_activity_events_time
  ON ledger_activity_events(created_at, id);
