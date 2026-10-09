# Invitation approval schema upgrade

The invitation flow creates an unassigned link. Opening it with a real Pullwise
cookie session can submit a join request, but only the original inviter's
current approval grants the selected role. The durable cross-ledger inbox uses
invitation ownership and current management authority. Up to 100 lifetime
applicants may request each link; one successful approval consumes it.

## Canonical source and local proof

`cloudflare/server/migrations/0008_workspace_join_approval.sql` is the only
populated-v7 migration. Its 17 statements execute as one atomic D1 batch:

1. Copy all invitations into the replacement table, make the existing GitHub
   recipient pair nullable, retain IDs, hashes, roles, states, revisions,
   expirations, original creator generations and accepted identities, then
   replace the original table and restore its indexes.
2. Add the creator/status index for the durable inbox.
3. Copy all workspace audit events unchanged and permit `request_join`,
   `approve_join` and `reject_join` in the rebuilt event table.
4. Create `workspace_join_requests` with a unique invitation/applicant pair,
   invite foreign key, revision bounds, review metadata checks and three
   bounded inbox/applicant indexes. No existing membership changes.
5. Assert zero foreign-key violations through the existing command guard,
   clear it and commit the complete batch.

The compiled frozen v4/v5/v6/v7 authorities remain separately verifiable.
Current v8 schema fingerprint is
`5f6de7e9cb06b5ddba57baf17075c47dbd9b060f5fd7c04852981af66739128a`.
Empty isolated databases use 38 compiled final-schema statements in one batch;
replaying all eight historical migrations would exceed the fixed 64-statement
batch limit. The final schema is identical to applying the canonical migration
sequence. `workspace_invites` has four indexes and join requests have five,
including implicit primary/unique indexes. Scalar insert/update reservations
therefore charge 5/9 and 6/11 rows respectively. Requests also participate in
the existing commercial write allowance; they consume no expense/project slots.

`tests/test_preview_invite_approval_schema_upgrade.py` proves populated history,
original journal markers, cumulative accounting, schema drift rejection,
unknown-outcome stops, atomic rollback, metadata checks and restart behavior.
`tests/test_invite_approval_meter_plan_limits.py` verifies index reservations and
pre-dispatch commercial allowance rejection. Native D1 metadata, actual restart
and transaction behavior are separately recorded by
`scripts/check-invite-approval-schema-native.py` in
`docs/validation/invite-approval-schema-native-local-2026-10-09.json`.

## Reviewed remote gate

This source change does not activate production D1 or execute a remote
migration. `PULLWISE_PREVIEW_SCHEMA_V8_UPGRADE_ENABLED` defaults to `0`; the
checked-in preview flag is deliberately enabled after local native schema acceptance;
runtime fallback for a missing flag remains `0`. Publication also requires the
native application journey and review
of the deployed v7 journal's numeric cardinalities. Production
`PULLWISE_D1_ACCESS_ENABLED=0` remains required.

The sole permitted upgrade path holds the existing singleton coordinator lock
and original database. It requires the exact v7 fingerprint, complete prior
v4/v6/v7/state-record markers, verified product cardinalities, no active ticket,
no persisted stop and no previous v8 attempt. It appends one
`product-schema-v7-to-v8` case and `schema_upgrade_v8` marker to the same journal.
No schema reset, namespace replacement, counter reset, GET-driven unflagged
migration, synthetic remote account or forced retry is allowed.

Let `S` be all verified v7 table rows combined, `A` app-state rows, `I`
invitation rows and `E` workspace-event rows. The closed four-group plan reserves
`21120 + 128*S + 384*A + 64*I + 32*E` rows read and
`384 + 9*I + 6*E` rows written before any D1 operation. It includes schema
catalog reads, complete before/after counts, identity structural checks,
foreign-key scans, invitation/event copies and old/new index effects. The read
reservations cover three native attempts; the exact compiled mutation group
requires a single write attempt or absent attempts with the documented
nonretryable D1 write contract. Every result still needs complete numeric
rows-read/written metadata. Unknown outcomes or incomplete accounting persist
a stop, retain all reservations and cannot replay after restart.

After one reviewed preview upgrade, verify v8 fingerprint, zero FK violations,
unchanged historical numeric counts, completed v8 marker and appended numeric
accounting through the existing journal. A lost or ambiguous response is a
stop requiring investigation, not permission to invoke the migration again.
Real A/B invitation acceptance remains a separate finite preview user journey;
local synthetic/native evidence does not claim that acceptance.
