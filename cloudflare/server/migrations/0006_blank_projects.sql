-- Run the whole migration as one atomic D1 batch. Keep the original parent
-- name while copying; renaming the old parent first would rewrite child FKs.
-- Deferred checks permit the temporary parent absence without disabling FKs.
PRAGMA defer_foreign_keys=ON;
CREATE TABLE ledger_projects_v6 (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  github_repo_id INTEGER CHECK (github_repo_id IS NULL OR github_repo_id > 0),
  github_full_name TEXT,
  description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 2000),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '' CHECK (length(name) <= 120),
  github_organization_id INTEGER
    CHECK (github_organization_id IS NULL OR github_organization_id BETWEEN 1 AND 9007199254740991),
  CHECK ((github_repo_id IS NULL AND github_full_name IS NULL AND github_organization_id IS NULL
          AND length(trim(name)) BETWEEN 1 AND 120)
      OR (github_repo_id IS NOT NULL AND github_full_name IS NOT NULL)),
  UNIQUE (owner_id, github_repo_id),
  UNIQUE (owner_id, id)
);
INSERT INTO ledger_projects_v6
  (id,owner_id,github_repo_id,github_full_name,description,status,revision,
   created_at,updated_at,name,github_organization_id)
  SELECT id,owner_id,github_repo_id,github_full_name,description,status,revision,
         created_at,updated_at,name,github_organization_id FROM ledger_projects;
DROP TABLE ledger_projects;
ALTER TABLE ledger_projects_v6 RENAME TO ledger_projects;
CREATE INDEX ledger_projects_owner_status ON ledger_projects(owner_id, status);
-- Assert final FK integrity before turning deferral off. A violation fails the
-- whole native batch instead of clearing SQLite's deferred-violation counter.
INSERT INTO d1_command_guard(ok)
  VALUES(CASE WHEN NOT EXISTS(SELECT 1 FROM pragma_foreign_key_check) THEN 1 ELSE 0 END);
DELETE FROM d1_command_guard;
PRAGMA defer_foreign_keys=OFF;
