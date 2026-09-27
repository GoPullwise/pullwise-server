-- Optional suggestion calls and outcomes. These tables never post an expense.
CREATE TABLE expense_suggestion_budget (
  owner_id TEXT NOT NULL,
  day TEXT NOT NULL,
  attempts INTEGER NOT NULL CHECK (attempts BETWEEN 0 AND 20),
  PRIMARY KEY (owner_id, day)
);

CREATE TABLE expense_suggestion_events (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  question_version TEXT NOT NULL,
  draft_target_kind TEXT NOT NULL,
  draft_project_id TEXT,
  model_version TEXT,
  outcome TEXT NOT NULL CHECK (outcome IN ('unavailable', 'uncertain', 'available')),
  category_id TEXT,
  target_kind TEXT,
  project_id TEXT,
  category_probabilities_json TEXT,
  target_probabilities_json TEXT,
  accepted_category_id TEXT,
  accepted_target_kind TEXT,
  decided_at TEXT
);
CREATE INDEX expense_suggestion_events_owner_time
  ON expense_suggestion_events(owner_id, created_at);
