-- Runtime foundation for S03-S05 on a fresh D1 database.
-- Account and platform billing facts stay separate from ledger expenses.
CREATE TABLE IF NOT EXISTS app_state (
  name TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at INTEGER NOT NULL
);
INSERT OR IGNORE INTO app_state(name,payload,updated_at) VALUES
  ('users','{}',0),('sessions','{}',0),('billingEvents','{}',0),
  ('billingPendingUpdates','[]',0);

CREATE TABLE IF NOT EXISTS d1_command_guard (ok INTEGER NOT NULL CHECK(ok=1));
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
  key_prefix TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
  scopes TEXT NOT NULL DEFAULT '[]', expires_at INTEGER,
  restrictions TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL,
  last_used_at INTEGER, revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id,revoked_at);

CREATE TABLE IF NOT EXISTS account_entitlement_authority (
  owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>=1),
  plan TEXT NOT NULL, period TEXT NOT NULL,
  period_start INTEGER NOT NULL CHECK(period_start>=0),
  valid_until INTEGER NOT NULL, dirty INTEGER NOT NULL CHECK(dirty IN (0,1))
);
CREATE TABLE IF NOT EXISTS billing_webhook_receipts (
  event_id TEXT PRIMARY KEY, raw_sha256 TEXT NOT NULL, update_json TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','applied'))
);
CREATE TABLE IF NOT EXISTS billing_public_catalog (
  id INTEGER PRIMARY KEY CHECK(id=1), payload_json TEXT NOT NULL,
  expires_at INTEGER NOT NULL, source_revision INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
