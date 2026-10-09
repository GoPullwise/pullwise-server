# Rolling activity schema v9

The activity tab shows changes made during the previous 24 hours in its project
or shared pool. The separate `ledger_activity_events` projection records the
authenticated actor's safe identity snapshot, affected resource, action, target,
before/after values and server UTC timestamp. Moving an expense produces a row
for each affected scope. Authorization applies to the requested scope and to the
values shown; API key restrictions must not expose another target's contents.

The projection starts empty at publication. Migration 0009 performs no historical
backfill and does not rewrite or retire the original expense/workspace audits or
financial records. Old activity rows are hidden by the server's rolling 24-hour
filter even before lazy retirement. Business mutations and the existing hourly
coordinator path may retire at most 16 expired rows per batch using a bounded
time-index read and unique-ID deletes. GETs are read-only, there is no new cron,
and logging/retirement does not add a commercial business-write charge.

`PULLWISE_PREVIEW_SCHEMA_V9_UPGRADE_ENABLED` defaults to `0`. After local native
schema and authenticated workflow acceptance, preview publication may explicitly
enable the reviewed v8-to-v9 case. The same database, original coordinator
namespace, `pullwise-s17-s18-2026-09-28` singleton, cumulative counters, completed
upgrade markers and state-storage version are retained. Production D1 remains
paused and has no cron.

The case creates one empty table and two indexes in one atomic three-statement
D1 batch. It never rebuilds, copies or deletes existing rows. Exact v8 schema,
verified cardinalities, healthy accounting and completed prior markers are
required before dispatch. For `S = sum(all 21 old table cardinalities)` and
`A = app_state cardinality`, the complete closed plan reserves:

- Reads: `15744 + 96*S + 384*A`.
- Writes: `128`, including catalog/index effects.
- Four finite operation groups: old schema, old cardinality/state/FK proof,
  atomic CREATE batch, and new schema/cardinality/state/FK proof.

The read groups reserve all three possible native attempts. Each result must
provide complete numeric row accounting. The exact CREATE group uses the
documented nonretryable D1 write contract when native attempts metadata is
absent; the native value remains `null`, never invented. Unknown or interrupted
outcomes retain the complete reservation and stop persistently. No reset,
recovery or retry endpoint is introduced.

The canonical v9 schema has 22 tables and 49 indexes, with fingerprint
`3bc9f5e11f8883b22a791180204ef55ba9f6990cb03ea59d6cf315d266c70ad2`.
Fresh initialization compiles directly to 41 SQL statements within the original
64-statement batch limit. Scope/time and retirement indexes avoid historical
JSON scanning; actor JSON is a 2 KiB object, and each optional before/after
snapshot is a 16 KiB object. These envelopes are also reviewed by the metered
application path before SQL dispatch.

Local native evidence:
[activity-schema-native-local-2026-10-09.json](activity-schema-native-local-2026-10-09.json).
Five finite local requests and two actual process restarts retained every one of
the 21 legacy table hashes and original markers. Native migration accounting
was 453 rows read / 5 rows written and exactly matched the journal delta.
An injected failure after CREATE INDEX rolled back the entire native batch;
the unknown-outcome stop and reservations survived restart with zero retry SQL.
Constraint probes exercised UTF-8 byte limits, JSON types, actions and target
pairs. Project/shared 24-hour reads, row-tuple keyset pagination and expiry
queries used the expected covering SEARCH indexes without a table scan or
temporary sort. These isolated fixture/probe operations are explicitly labeled
outside migration accounting and grant no remote application acceptance.
