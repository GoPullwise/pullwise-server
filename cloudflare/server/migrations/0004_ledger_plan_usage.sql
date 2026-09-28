-- Lazy per-account counters. No cron, daily maintenance or GET-side writes.
-- Capacity is cumulative: archival/deletion do not refund slots.
CREATE TABLE ledger_plan_usage (
  owner_id TEXT PRIMARY KEY,
  projects INTEGER NOT NULL,
  records INTEGER NOT NULL,
  month TEXT NOT NULL,
  writes INTEGER NOT NULL,
  minute INTEGER NOT NULL,
  minute_writes INTEGER NOT NULL,
  jev_reserved_microusd INTEGER NOT NULL,
  project_cap INTEGER NOT NULL,
  record_cap INTEGER NOT NULL,
  minute_cap INTEGER NOT NULL,
  month_cap INTEGER NOT NULL,
  jev_cap INTEGER NOT NULL,
  project_delta INTEGER NOT NULL,
  record_delta INTEGER NOT NULL,
  jev_delta INTEGER NOT NULL,
  previous_month TEXT NOT NULL,
  previous_minute INTEGER NOT NULL,
  CONSTRAINT plan_clock_fence CHECK(month>=previous_month AND minute>=previous_minute),
  CONSTRAINT plan_project_limit CHECK(project_delta=0 OR projects<=project_cap),
  CONSTRAINT plan_record_limit CHECK(record_delta=0 OR records<=record_cap),
  CONSTRAINT plan_write_rate CHECK(minute_writes<=minute_cap),
  CONSTRAINT plan_monthly_write_limit CHECK(writes<=month_cap),
  CONSTRAINT plan_max_required CHECK(jev_delta=0 OR jev_cap>0),
  CONSTRAINT plan_jev_budget_limit CHECK(jev_delta=0 OR jev_reserved_microusd<=jev_cap)
);
