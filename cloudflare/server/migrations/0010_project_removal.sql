-- One atomic parent replacement preserves every historical financial row and
-- child FK. Removal records a tombstone; it does not delete expenses or rules.
-- Native D1 caps LIKE/GLOB patterns at 50 bytes. Separate 42-byte date and
-- 32-byte time patterns preserve the exact 20-byte UTC timestamp shape.
PRAGMA defer_foreign_keys=ON;
CREATE TABLE ledger_projects_v10 (
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
  development_url TEXT
    CHECK (development_url IS NULL OR length(CAST(development_url AS BLOB)) BETWEEN 1 AND 2048),
  product_url TEXT
    CHECK (product_url IS NULL OR length(CAST(product_url AS BLOB)) BETWEEN 1 AND 2048),
  deleted_at TEXT DEFAULT NULL CHECK (deleted_at IS NULL OR (
    typeof(deleted_at) = 'text' AND length(CAST(deleted_at AS BLOB)) = 20
    AND substr(deleted_at,1,10) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    AND substr(deleted_at,11,1) = 'T'
    AND substr(deleted_at,12,8) GLOB '[0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
    AND substr(deleted_at,20,1) = 'Z'
  )),
  CHECK ((deleted_at IS NULL AND (
            (github_repo_id IS NULL AND github_full_name IS NULL AND github_organization_id IS NULL
             AND length(trim(name)) BETWEEN 1 AND 120)
            OR (github_repo_id IS NOT NULL AND github_full_name IS NOT NULL)))
      OR (deleted_at IS NOT NULL AND status = 'archived'
          AND github_repo_id IS NULL AND github_full_name IS NULL AND github_organization_id IS NULL)),
  UNIQUE (owner_id, github_repo_id),
  UNIQUE (owner_id, id)
);
INSERT INTO ledger_projects_v10
  (id,owner_id,github_repo_id,github_full_name,description,status,revision,
   created_at,updated_at,name,github_organization_id,development_url,product_url)
  SELECT id,owner_id,github_repo_id,github_full_name,description,status,revision,
         created_at,updated_at,name,github_organization_id,development_url,product_url
  FROM ledger_projects;
DROP TABLE ledger_projects;
ALTER TABLE ledger_projects_v10 RENAME TO ledger_projects;
-- Reuse the existing index count: live owner/status lists exclude tombstones.
CREATE INDEX ledger_projects_owner_status ON ledger_projects(owner_id, deleted_at, status);
-- Fail the whole batch before clearing SQLite's deferred-violation counter.
INSERT INTO d1_command_guard(ok)
  VALUES(CASE WHEN NOT EXISTS(SELECT 1 FROM pragma_foreign_key_check) THEN 1 ELSE 0 END);
DELETE FROM d1_command_guard;
PRAGMA defer_foreign_keys=OFF;
