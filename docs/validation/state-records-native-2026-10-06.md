The normalized state storage passed finite local native Python Worker/D1 and
SQLite Durable Object acceptance. This uses synthetic accounts and signed
synthetic payment facts. It makes no remote D1/provider requests or real charges.
The complete evidence is [state-records-native-2026-10-06.json](state-records-native-2026-10-06.json).

The same schema 5 fingerprint survives cutover. Ten legacy records are copied
in one atomic native mutation batch; original maps become empty and an extra
unknown empty legacy row remains intact. Existing users, sessions, OAuth states,
billing events/pending facts, account revisions and member roles survive.
Restarting workerd on the same files does not replay the cutover. A separate
local DO fixture keeps an unknown-outcome stop and dispatches no D1 statements.

The capacity fixture contains 500 users and 1,000 sessions. It inserts the
1,494 additional records once in 24 finite scalar batches. The owner contains
1,000 canonically normalized repositories and 100 subscription history events.
Its initial 197,041-byte record, and later 198,163-byte billing-enriched record,
both pass native account CAS, authentication and entitlement refresh. Old owner
and editor sessions remain valid. A record exceeding 512 KiB is rejected before
native dispatch, and the next valid request remains healthy. HTTP input retains
its 8 KiB bound.

Signed paid, unpaid and replayed-paid facts produce effective Pro, Free and
Free respectively. Their settled authority revisions are 12, 14 and 14. All
1,000 repositories, 100 history events and retained opaque fields survive.
Session issuance/revocation, wrong-owner rejection, single-use OAuth states,
receipt deduplication and editor authorization also pass. Final user keyset
enumeration safely returns 16 records for a requested upper bound of 17, then
16 disjoint records at the next cursor, with two native SELECTs and zero writes.
The earlier capacity phase's 17-record pages are historical evidence; they are
not the final pagination policy.

The numeric refresh after the large account write reads 1,514 rows for table
counts and 1,509 rows for record-kind counts: 3,023 actual native reads and zero
writes at 1,509 physical app_state rows. It returns numeric aggregates, without
loading every account payload. The independent current-source marker case
passes first strict verification, cached ordinary reads, refresh after mutation
and reconstructed-journal reuse. Original accounting and immutable evidence
remain cumulative; native missing-attempt metadata remains null.

The fresh normalized Projects/subscriptions journey passes 79 local HTTP
requests, including invitation acceptance, workspace isolation, editor/viewer/
admin restrictions, API-key invalidation, project lifecycle and exact expense
history, plus subscription success/failure logic. See
[projects-state-records-native-2026-10-06.json](projects-state-records-native-2026-10-06.json).
Only the inactive user enumeration helper changed afterward; its final source
is covered by the separate native page and large-account rechecks.

Two fixture problems are preserved in the evidence. The first expected an
internal settlement field from the public webhook ACK; the route correctly
returns only received=true. Billing continued on the same database after fixing
that assertion and authority revision expectations. A later restart connected
to a closing TCP listener before the new process was ready; readiness now
requires that process's Wrangler Ready log. Verification continued on the
successfully migrated files. Neither correction reseeds data or resets counters.

Across these fixtures, 2,686 observed Python-native statements report 94,663
rows read and 3,705 rows written. There are 109 bounded local HTTP attempts and
108 retained responses, including the failed restart transport attempt. These
totals exclude Projects fixture CLI initialization/seed, two bounded offline
synthetic recovery SELECTs and explicitly synthetic journal-policy metadata.
All runtime processes stopped. This local report does not constitute
real-account acceptance. The subsequent [actual preview publication](state-records-preview-release-2026-10-06.json)
and [focused two-real-user run](projects-two-real-users-focused-2026-10-06.json)
provide separate final-release evidence; the original partial baseline and its
failure remain preserved.
