-- Existing owner IDs are immutable workspace IDs. Personal owners are implicit;
-- these rows grant access to the same historical ledger, not a copy of it.
CREATE TABLE workspace_members (
  workspace_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision BETWEEN 1 AND 9007199254740991),
  joined_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  removed_at TEXT,
  invited_by_user_id TEXT NOT NULL,
  PRIMARY KEY (workspace_id, user_id),
  CHECK (workspace_id <> user_id)
);
CREATE INDEX workspace_members_user_active
  ON workspace_members(user_id, removed_at, workspace_id);

-- Invitations store only a hash; their random bearer token is displayed once.
CREATE TABLE workspace_invites (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  github_recipient_id INTEGER NOT NULL CHECK (github_recipient_id BETWEEN 1 AND 9007199254740991),
  github_login TEXT NOT NULL CHECK (length(github_login) BETWEEN 1 AND 39),
  token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
  role TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'revoked')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision BETWEEN 1 AND 9007199254740991),
  expires_at INTEGER NOT NULL CHECK (expires_at BETWEEN 1 AND 9007199254740991),
  created_by_user_id TEXT NOT NULL,
  created_by_revision INTEGER NOT NULL CHECK (created_by_revision BETWEEN 1 AND 9007199254740991),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  accepted_by_user_id TEXT,
  accepted_at TEXT
);
CREATE INDEX workspace_invites_workspace_status
  ON workspace_invites(workspace_id, status, created_at, id);

CREATE TABLE workspace_events (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('invite', 'revoke_invite', 'accept_invite', 'change_role', 'remove_member')),
  subject_id TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX workspace_events_workspace_created
  ON workspace_events(workspace_id, created_at, id);

-- Keep the existing project and expense foreign keys and stable project IDs.
ALTER TABLE ledger_projects ADD COLUMN name TEXT NOT NULL DEFAULT '' CHECK (length(name) <= 120);
ALTER TABLE ledger_projects ADD COLUMN github_organization_id INTEGER
  CHECK (github_organization_id IS NULL OR github_organization_id BETWEEN 1 AND 9007199254740991);

CREATE TABLE ledger_project_repositories (
  owner_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  github_repo_id INTEGER NOT NULL CHECK (github_repo_id BETWEEN 1 AND 9007199254740991),
  github_full_name TEXT NOT NULL,
  installation_id INTEGER CHECK (installation_id IS NULL OR installation_id BETWEEN 1 AND 9007199254740991),
  github_account_id INTEGER CHECK (github_account_id IS NULL OR github_account_id BETWEEN 1 AND 9007199254740991),
  github_account_login TEXT,
  github_account_type TEXT CHECK (github_account_type IS NULL OR github_account_type IN ('User', 'Organization')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (project_id, github_repo_id),
  UNIQUE (owner_id, github_repo_id),
  FOREIGN KEY (owner_id, project_id) REFERENCES ledger_projects(owner_id, id) ON DELETE RESTRICT
);
INSERT INTO ledger_project_repositories(owner_id, project_id, github_repo_id, github_full_name, created_at)
  SELECT owner_id, id, github_repo_id, github_full_name, created_at FROM ledger_projects;
