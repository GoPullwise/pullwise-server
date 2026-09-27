-- Ledger-owned data only. Existing identity and platform billing data stay separate.
-- All timestamps are UTC ISO 8601 text; occurred_on is a user-supplied business date.
CREATE TABLE ledger_projects (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  github_repo_id INTEGER NOT NULL CHECK (github_repo_id > 0),
  github_full_name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 2000),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (owner_id, github_repo_id),
  UNIQUE (owner_id, id)
);
CREATE INDEX ledger_projects_owner_status ON ledger_projects(owner_id, status);

CREATE TABLE expense_categories (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  color TEXT CHECK (color IS NULL OR length(color) <= 32),
  archived_at TEXT,
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (owner_id, id)
);
CREATE UNIQUE INDEX expense_categories_active_name
  ON expense_categories(owner_id, lower(name)) WHERE archived_at IS NULL;

CREATE TABLE expenses (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  target_kind TEXT NOT NULL CHECK (target_kind IN ('project', 'shared')),
  project_id TEXT,
  category_id TEXT NOT NULL,
  occurred_on TEXT NOT NULL CHECK (
    length(occurred_on) = 10 AND occurred_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
  ),
  amount_minor INTEGER NOT NULL CHECK (amount_minor BETWEEN 0 AND 9007199254740991),
  currency TEXT NOT NULL CHECK (length(currency) = 3 AND currency GLOB '[A-Z][A-Z][A-Z]'),
  purpose TEXT NOT NULL CHECK (length(purpose) BETWEEN 1 AND 500),
  note TEXT CHECK (note IS NULL OR length(note) <= 4000),
  quantity_decimal TEXT CHECK (quantity_decimal IS NULL OR length(quantity_decimal) BETWEEN 1 AND 40),
  unit TEXT CHECK (unit IS NULL OR length(unit) <= 40),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT,
  CHECK ((target_kind = 'shared' AND project_id IS NULL) OR
         (target_kind = 'project' AND project_id IS NOT NULL)),
  FOREIGN KEY (owner_id, project_id) REFERENCES ledger_projects(owner_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (owner_id, category_id) REFERENCES expense_categories(owner_id, id) ON DELETE RESTRICT,
  UNIQUE (owner_id, id)
);
CREATE INDEX expenses_owner_target_date
  ON expenses(owner_id, target_kind, project_id, occurred_on, category_id, id)
  WHERE deleted_at IS NULL;
CREATE INDEX expenses_owner_category_date
  ON expenses(owner_id, category_id, occurred_on, id)
  WHERE deleted_at IS NULL;

CREATE TABLE expense_events (
  id TEXT PRIMARY KEY,
  expense_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  actor_kind TEXT NOT NULL CHECK (actor_kind IN ('session', 'api_key')),
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('create', 'update', 'delete', 'restore')),
  before_json TEXT,
  after_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (owner_id, expense_id) REFERENCES expenses(owner_id, id) ON DELETE RESTRICT
);
CREATE INDEX expense_events_owner_expense ON expense_events(owner_id, expense_id, created_at, id);

-- An owner and key identify one creation request. The stored response allows exact replay.
CREATE TABLE expense_create_idempotency (
  owner_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  expense_id TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (owner_id, idempotency_key),
  FOREIGN KEY (owner_id, expense_id) REFERENCES expenses(owner_id, id) ON DELETE RESTRICT
);
