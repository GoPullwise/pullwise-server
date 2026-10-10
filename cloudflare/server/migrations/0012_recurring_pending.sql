-- Add recoverable scheduled-expense failures without rewriting financial rows.
-- The frozen template is the failed occurrence's values, never a key grant.
CREATE TABLE expense_recurring_pending (
  rule_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  period_key TEXT NOT NULL CHECK (length(period_key) BETWEEN 1 AND 16),
  scheduled_on TEXT NOT NULL CHECK (
    length(scheduled_on) = 10
    AND scheduled_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
  ),
  template_json TEXT NOT NULL CHECK (
    json_valid(template_json) AND json_type(template_json) = 'object'
    AND length(CAST(template_json AS BLOB)) <= 8192
  ),
  failed_code TEXT NOT NULL CHECK (length(failed_code) BETWEEN 1 AND 80),
  rule_revision INTEGER NOT NULL CHECK (rule_revision BETWEEN 1 AND 9007199254740991),
  created_at TEXT NOT NULL,
  recipient_user_id TEXT NOT NULL CHECK (length(CAST(recipient_user_id AS BLOB)) BETWEEN 1 AND 120),
  notification_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (notification_state IN ('pending', 'sending', 'email', 'inbox')),
  PRIMARY KEY (rule_id, period_key),
  FOREIGN KEY (owner_id, rule_id) REFERENCES expense_recurring_rules(owner_id, id) ON DELETE RESTRICT
);
CREATE INDEX expense_recurring_pending_recipient
  ON expense_recurring_pending(recipient_user_id, notification_state, created_at, rule_id, period_key);
-- The indexed rule prefix makes this scan at most ten rows. Application
-- admission also fences the count in the atomic advance-and-retain batch.
CREATE TRIGGER expense_recurring_pending_insert_cap
BEFORE INSERT ON expense_recurring_pending
WHEN (SELECT COUNT(*) FROM expense_recurring_pending WHERE rule_id=NEW.rule_id) >= 10
BEGIN
  SELECT RAISE(ABORT, 'recurring_pending_limit');
END;
CREATE TRIGGER expense_recurring_pending_move_cap
BEFORE UPDATE OF rule_id ON expense_recurring_pending
WHEN NEW.rule_id<>OLD.rule_id
  AND (SELECT COUNT(*) FROM expense_recurring_pending WHERE rule_id=NEW.rule_id) >= 10
BEGIN
  SELECT RAISE(ABORT, 'recurring_pending_limit');
END;
INSERT INTO d1_command_guard(ok)
  VALUES(CASE WHEN NOT EXISTS(SELECT 1 FROM pragma_foreign_key_check) THEN 1 ELSE 0 END);
DELETE FROM d1_command_guard;
