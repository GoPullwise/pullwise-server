-- One atomic in-place upgrade. Preserve every invitation and audit row.
-- New links identify their applicant through the authenticated Pullwise account.
CREATE TABLE workspace_invites_v8 (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  github_recipient_id INTEGER CHECK (github_recipient_id IS NULL OR github_recipient_id BETWEEN 1 AND 9007199254740991),
  github_login TEXT CHECK (github_login IS NULL OR length(github_login) BETWEEN 1 AND 39),
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
  accepted_at TEXT,
  CHECK ((github_recipient_id IS NULL AND github_login IS NULL)
      OR (github_recipient_id IS NOT NULL AND github_login IS NOT NULL))
);
INSERT INTO workspace_invites_v8
  (id,workspace_id,github_recipient_id,github_login,token_hash,role,status,revision,
   expires_at,created_by_user_id,created_by_revision,created_at,updated_at,
   accepted_by_user_id,accepted_at)
  SELECT id,workspace_id,github_recipient_id,github_login,token_hash,role,status,revision,
   expires_at,created_by_user_id,created_by_revision,created_at,updated_at,
   accepted_by_user_id,accepted_at FROM workspace_invites;
DROP TABLE workspace_invites;
ALTER TABLE workspace_invites_v8 RENAME TO workspace_invites;
CREATE INDEX workspace_invites_workspace_status
  ON workspace_invites(workspace_id, status, created_at, id);
CREATE INDEX workspace_invites_creator_status
  ON workspace_invites(created_by_user_id, status, created_at, id);

CREATE TABLE workspace_events_v8 (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('invite', 'revoke_invite', 'accept_invite', 'change_role', 'remove_member', 'request_join', 'approve_join', 'reject_join')),
  subject_id TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  created_at TEXT NOT NULL
);
INSERT INTO workspace_events_v8
  (id,workspace_id,actor_user_id,action,subject_id,before_json,after_json,created_at)
  SELECT id,workspace_id,actor_user_id,action,subject_id,before_json,after_json,created_at
  FROM workspace_events;
DROP TABLE workspace_events;
ALTER TABLE workspace_events_v8 RENAME TO workspace_events;
CREATE INDEX workspace_events_workspace_created
  ON workspace_events(workspace_id, created_at, id);

CREATE TABLE workspace_join_requests (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  invite_id TEXT NOT NULL,
  applicant_user_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision BETWEEN 1 AND 9007199254740991),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reviewed_by_user_id TEXT,
  reviewed_at TEXT,
  UNIQUE (invite_id, applicant_user_id),
  FOREIGN KEY (invite_id) REFERENCES workspace_invites(id) ON DELETE RESTRICT,
  CHECK ((status = 'pending' AND reviewed_by_user_id IS NULL AND reviewed_at IS NULL)
      OR (status IN ('approved', 'rejected') AND reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL))
);
CREATE INDEX workspace_join_requests_invite_status
  ON workspace_join_requests(invite_id, status, created_at, id);
CREATE INDEX workspace_join_requests_workspace_status
  ON workspace_join_requests(workspace_id, status, created_at, id);
CREATE INDEX workspace_join_requests_applicant_status
  ON workspace_join_requests(applicant_user_id, status, created_at, id);

-- Never publish a completed journal marker with broken references.
INSERT INTO d1_command_guard(ok)
  VALUES(CASE WHEN NOT EXISTS(SELECT 1 FROM pragma_foreign_key_check) THEN 1 ELSE 0 END);
DELETE FROM d1_command_guard;
