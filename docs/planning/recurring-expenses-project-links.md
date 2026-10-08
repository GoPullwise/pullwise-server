# Project links and recurring expenses

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
targets the shared pool. The same form supports one-time costs and recurring
rules. Rules require an explicit active category and never invoke Jev. Planned
costs remain separate from ordinary expenses and are excluded from reports.
Once generated, a cost is an ordinary expense with its original occurrence date
and normal history/edit/delete/export/report behavior.

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

`/api/v1/expense-recurring-rules` and `/{id}` provide cookie-session-only
management. GET requires expense-read; creation/edit/pause/resume/cancel require
expense-write. POST uses the existing namespaced idempotency-key convention;
PATCH and DELETE require If-Match. A rule's target is immutable. Editing or
resuming validates the current actor and renews the background grant, without
changing its original creation-idempotency identity or generated history.
Cancellation preserves the rule and permanent occurrence facts. Stopping
remains possible when the commercial write allowance is exhausted.

A start date in the past allows bounded catch-up. More than 12 due periods
blocks for explicit review; resume starts at the next future matching date and
does not backfill paused periods. Blocked access/category/quota rules require
an explicit correction and resume. There are at most 100 uncanceled rules per
workspace, 10 rules and 10 processed occurrences per tick, and 3 occurrences per
rule per tick. The UI explains these lifecycle states and the next date.

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
