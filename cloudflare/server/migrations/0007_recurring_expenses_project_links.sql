-- One atomic batch extends existing data. No account, journal or history reset.
ALTER TABLE ledger_projects ADD COLUMN development_url TEXT
  CHECK (development_url IS NULL OR length(CAST(development_url AS BLOB)) BETWEEN 1 AND 2048);
ALTER TABLE ledger_projects ADD COLUMN product_url TEXT
  CHECK (product_url IS NULL OR length(CAST(product_url AS BLOB)) BETWEEN 1 AND 2048);

-- Audit the actual scheduled actor separately from a live session or API key.
-- The old event rows and their references remain byte-for-byte intact.
CREATE TABLE expense_events_v7 (
  id TEXT PRIMARY KEY,
  expense_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  actor_kind TEXT NOT NULL CHECK (actor_kind IN ('session', 'api_key', 'schedule')),
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('create', 'update', 'delete', 'restore')),
  before_json TEXT,
  after_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (owner_id, expense_id) REFERENCES expenses(owner_id, id) ON DELETE RESTRICT
);
INSERT INTO expense_events_v7
  (id,expense_id,owner_id,actor_kind,actor_id,action,before_json,after_json,created_at)
  SELECT id,expense_id,owner_id,actor_kind,actor_id,action,before_json,after_json,created_at
  FROM expense_events;
DROP TABLE expense_events;
ALTER TABLE expense_events_v7 RENAME TO expense_events;
CREATE INDEX expense_events_owner_expense ON expense_events(owner_id, expense_id, created_at, id);

CREATE TABLE expense_recurring_rules (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  target_kind TEXT NOT NULL CHECK (target_kind IN ('project', 'shared')),
  project_id TEXT,
  template_json TEXT NOT NULL CHECK (
    json_valid(template_json) AND json_type(template_json) = 'object'
    AND length(CAST(template_json AS BLOB)) <= 8192
  ),
  schedule_json TEXT NOT NULL CHECK (
    json_valid(schedule_json) AND json_type(schedule_json) = 'object'
    AND length(CAST(schedule_json AS BLOB)) <= 2048
  ),
  status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'blocked', 'completed', 'canceled')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision BETWEEN 1 AND 9007199254740991),
  next_run_at INTEGER CHECK (next_run_at IS NULL OR next_run_at BETWEEN 0 AND 9007199254740991),
  next_occurrence_on TEXT CHECK (next_occurrence_on IS NULL OR (
    length(next_occurrence_on) = 10
    AND next_occurrence_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
  )),
  next_period_key TEXT CHECK (next_period_key IS NULL OR length(next_period_key) BETWEEN 1 AND 16),
  blocked_code TEXT CHECK (blocked_code IS NULL OR length(blocked_code) BETWEEN 1 AND 80),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  create_key TEXT NOT NULL CHECK (length(CAST(create_key AS BLOB)) BETWEEN 1 AND 128),
  create_sha256 TEXT NOT NULL CHECK (length(create_sha256) = 64),
  create_response_json TEXT NOT NULL CHECK (
    json_valid(create_response_json) AND json_type(create_response_json) = 'object'
    AND length(CAST(create_response_json AS BLOB)) <= 16384
  ),
  CHECK ((target_kind = 'shared' AND project_id IS NULL)
      OR (target_kind = 'project' AND project_id IS NOT NULL)),
  UNIQUE (owner_id, id),
  UNIQUE (owner_id, create_key),
  FOREIGN KEY (owner_id, project_id) REFERENCES ledger_projects(owner_id, id) ON DELETE RESTRICT
);
CREATE INDEX expense_recurring_rules_due ON expense_recurring_rules(status, next_run_at, id);
CREATE INDEX expense_recurring_rules_owner
  ON expense_recurring_rules(owner_id, status, target_kind, project_id, created_at, id);

-- One immutable occurrence per rule/period prevents duplicate creation even
-- when the corresponding financial record is later soft-deleted.
CREATE TABLE expense_recurring_occurrences (
  rule_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  period_key TEXT NOT NULL CHECK (length(period_key) BETWEEN 1 AND 16),
  scheduled_on TEXT NOT NULL CHECK (
    length(scheduled_on) = 10
    AND scheduled_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
  ),
  expense_id TEXT NOT NULL UNIQUE,
  rule_revision INTEGER NOT NULL CHECK (rule_revision BETWEEN 1 AND 9007199254740991),
  created_at TEXT NOT NULL,
  PRIMARY KEY (rule_id, period_key),
  FOREIGN KEY (owner_id, rule_id) REFERENCES expense_recurring_rules(owner_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (owner_id, expense_id) REFERENCES expenses(owner_id, id) ON DELETE RESTRICT
);
-- Assert the completed schema before publishing its journal marker.
INSERT INTO d1_command_guard(ok)
  VALUES(CASE WHEN NOT EXISTS(SELECT 1 FROM pragma_foreign_key_check) THEN 1 ELSE 0 END);
DELETE FROM d1_command_guard;
