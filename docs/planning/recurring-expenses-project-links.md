# Project links and recurring expenses

Implemented and published to preview on 2026-10-08. Both repositories are pushed
to GitHub main; production D1 stays paused without cron. Local/native evidence
and actual publication are recorded separately in
[current acceptance](../validation/local-acceptance.md) and the
[preview release receipt](../validation/recurring-links-preview-release-2026-10-08.json).

## Project destinations

Projects retain optional `developmentUrl` and `productUrl` independently of
GitHub bindings. Create defaults to null; omitted PATCH fields retain their
value and explicit null clears it. The Web accepts a bare domain by adding
HTTPS; the API accepts canonical absolute HTTP(S), no credentials or control
characters, and at most 2,048 UTF-8 bytes after serialization. These are user
navigation destinations, never Server requests or authentication redirects.

Projects expose product shortcuts in the existing aligned list. Unlinked
projects may expose the configured development shortcut. Linked projects use
individually authorized repository and organization metadata on the fixed
GitHub origin; lost GitHub access does not become a manual-development fallback.
Shortcuts and project navigation are separate semantic anchors. All existing
project role, key, ownership, metadata-visibility and revision guards remain.

## Actual expenses and automation

Project entry always targets the current project; shared-pool entry always
targets the shared pool. One-time entry records an expense already incurred;
recurring entry configures future expenses. Recurring creation and complete
edits support the same Jev category omission as ordinary expenses, subject to
the Owner's eligible plan, enabled preference, model availability and allowance.
Only a confident existing active category is selected. Explicit categories and
other explicit input remain unchanged. `CATEGORY_REQUIRED` preserves the draft
without saving the rule. Background execution reuses the saved category and
never invokes Jev.

Planned costs remain separate from ordinary expenses and are excluded from
spent totals and reports. A start date on or before today in the schedule's
timezone records exactly one initial expense dated `startOn`, without filling
every intervening period. A future start waits for that exact date, even when
the calendar selector differs; selectors govern later periods. Every successful
occurrence creates a separate ordinary expense with its original date and
normal history/edit/delete/export/report behavior. Repeated amounts are never
accumulated into an existing expense.

Calendar schedules store their original selectors, IANA timezone, inclusive
start date and optional inclusive end date:

| Frequency | Selector |
| --- | --- |
| Weekly | ISO weekday, Monday 1 through Sunday 7 |
| Monthly | Original day 1 through 31 |
| Quarterly | Month position 1 through 3 of each natural calendar quarter, plus day |
| Yearly | Original month and day |

A short month clamps the occurrence to its last day while retaining the original
anchor for later months or leap years. A local midnight fold chooses the first
instant; a gap chooses the first valid instant afterwards. Pinned tzdata is
bundled for the native Python Worker; CPython timezone tests alone are not
runtime acceptance.

## Management and execution

`/api/v1/expense-recurring-rules` and `/{id}` provide the same management
resources to browser sessions and Bearer API keys. GET requires `expenses:read`;
creation/edit/pause/resume/cancel/historical retry require `expenses:write`, the issuer's current
workspace role and the permitted project/shared target. POST uses the existing namespaced idempotency-key convention;
PATCH and DELETE require If-Match. A rule's target is immutable. Editing or
resuming validates the current actor and renews the background grant, without
changing its original creation-idempotency identity or generated history.
Cancellation preserves the rule and permanent occurrence facts. Stopping
remains possible when the commercial write allowance is exhausted.

A key-created or key-resumed rule stores only the original credential hash in
internal template metadata. The public rule DTO contains no token or key hash.
Every generation checks that key's current scopes, revocation, expiry, workspace
binding, membership revision and target permissions, with those facts fenced in
the same generated-expense transaction. A full edit or explicit resume adopts
the current actor and credential; a browser session can renew the rule without
retaining an old key grant. Pausing does not silently replace the saved grant.

Create, edit and resume present a next date strictly after local today, or null
when the schedule has ended. GET projects an overdue stored pointer into the
future for both `nextOccurrenceOn` and `nextRunAt`, returning `awaitingSync`
without a financial write. Reads never generate expenses or repair state.
Resume starts at the next future matching date and does not backfill paused
periods. Blocked access/category/commercial-write-allowance rules still require
explicit correction and resume. Delayed scheduled execution remains bounded;
there are at most 100 uncanceled rules per workspace, 10 rules and 10 processed
occurrences per tick, and 3 occurrences per rule per tick.

Capacity failures follow the current Owner's automatic-removal policy. If a
due expense cannot be inserted, the rule advances its future calendar and
retains the first ten unresolved failures in `pendingOccurrences`, with frozen
`periodKey`, `scheduledOn`, `amount`, `currency`, `purpose`, `categoryId`,
`blockedCode` and `createdAt`. While ten remain unresolved, later failures are
discarded rather than replacing those retained records. Each retained
occurrence keeps an Add to expenses action. The original period identity is
preserved across rule edits and expense removal.

Manual posting PATCHes the rule with only `retryPeriodKey` and the current
`If-Match`. It checks the current caller's authority, target, category, capacity
and credential. One atomic success creates one expense and immutable period
identity, removes that pending occurrence and leaves the future calendar
unchanged. A failed retry retains the occurrence. A settled period with a
current revision returns a no-op rule; a stale revision returns 412. Canceled
rules reject historical retries.

The saved execution account receives an English-only email when it has a usable
verified address, otherwise an actionable Pullwise inbox notification. Delivery
claims prevent repeating a settled send. The cookie-only read
`GET /api/v1/recurring-expense-notifications` returns up to 100 oldest actionable
retained failures plus `hasMore` across currently authorized ledgers, independent
of the selected workspace. It filters recipient identity, membership and target
access, excludes canceled rules, removed targets and email-delivered failures,
and never marks read or changes delivery state. Successful historical posting
removes the actionable notification with its pending occurrence.

Only preview has the hourly `0 * * * *` scheduled trigger. A due local-day cost
is normally generated within the next hourly check, subject to authorization,
plan limits and bounded catch-up. The handler awaits the original fixed
ValidationBudget coordinator, under its existing product lock. It uses real
execution time, PlanLimitedD1 and ProductMeteredD1. It never fabricates a session,
accepts a caller clock, retries a native ambiguous outcome or exposes an HTTP
tick endpoint. Healthy no-due ticks use an indexed bounded query and reuse the
persisted cardinality proof.

Each generation rechecks the current real actor, membership, target/category
and exact owner-plan snapshot. Expense, schedule audit, permanent period-key
occurrence, commercial usage and next-date advancement commit in one guarded
batch. The `(rule_id, period_key)` identity survives schedule revisions and
ordinary expense editing or soft deletion, preventing duplicate regeneration.

## Publication boundary

Schema 7 preserves the original preview database, DO namespace, cumulative
journal and completed upgrades. One reviewed atomic v6-to-v7 upgrade follows
finite local native migration, rollback/restart, API and scheduled acceptance.
The scheduler refuses an unready schema and does not migrate user data.
Main receives both repositories; deployments target preview explicitly.
Production D1 stays paused and has no recurring trigger. Local fixtures are
never remote accounts or evidence of real preview business transactions.
