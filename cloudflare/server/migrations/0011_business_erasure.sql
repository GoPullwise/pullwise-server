-- One atomic in-place extension. This migration preserves every business row;
-- project erasure and the separately authorized preview clear are explicit writes.
-- Native D1 caps GLOB patterns at 50 bytes; keep the UTC date/time checks split.
ALTER TABLE expense_categories ADD COLUMN deleted_at TEXT DEFAULT NULL CHECK (
  deleted_at IS NULL OR (
    typeof(deleted_at) = 'text' AND length(CAST(deleted_at AS BLOB)) = 20
    AND substr(deleted_at,1,10) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    AND substr(deleted_at,11,1) = 'T'
    AND substr(deleted_at,12,8) GLOB '[0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
    AND substr(deleted_at,20,1) = 'Z'
    AND archived_at IS NOT NULL
  )
);
-- Exact owner/expense FK probes must not scan every creation replay.
CREATE INDEX expense_create_idempotency_owner_expense
  ON expense_create_idempotency(owner_id, expense_id);
-- Saved-record inspection events retain server-selected source attribution.
ALTER TABLE expense_suggestion_events ADD COLUMN recorded_expense_id TEXT DEFAULT NULL CHECK (
  recorded_expense_id IS NULL OR (
    typeof(recorded_expense_id) = 'text'
    AND length(CAST(recorded_expense_id AS BLOB)) BETWEEN 1 AND 120
    AND recorded_expense_id NOT GLOB '*[^A-Za-z0-9_-]*'
  )
);
-- This table is only a child. Preserve consumed periods if their generated
-- expense is erased after being moved into a different project's target.
CREATE TABLE expense_recurring_occurrences_v11 (
  rule_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  period_key TEXT NOT NULL CHECK (length(period_key) BETWEEN 1 AND 16),
  scheduled_on TEXT NOT NULL CHECK (
    length(scheduled_on) = 10
    AND scheduled_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
  ),
  expense_id TEXT UNIQUE,
  rule_revision INTEGER NOT NULL CHECK (rule_revision BETWEEN 1 AND 9007199254740991),
  created_at TEXT NOT NULL,
  PRIMARY KEY (rule_id, period_key),
  FOREIGN KEY (owner_id, rule_id) REFERENCES expense_recurring_rules(owner_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (owner_id, expense_id) REFERENCES expenses(owner_id, id) ON DELETE RESTRICT
);
INSERT INTO expense_recurring_occurrences_v11
  (rule_id, owner_id, period_key, scheduled_on, expense_id, rule_revision, created_at)
  SELECT rule_id, owner_id, period_key, scheduled_on, expense_id, rule_revision, created_at
  FROM expense_recurring_occurrences;
DROP TABLE expense_recurring_occurrences;
ALTER TABLE expense_recurring_occurrences_v11 RENAME TO expense_recurring_occurrences;
