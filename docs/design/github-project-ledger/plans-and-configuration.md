# Ledger plan policy

Current user decisions, 2026-10-09: Free has 3 projects and 100 expense records;
Pro has 20 projects and 20,000 records; Max has 100 projects and 100,000 records.
Pro and Max include Jev, with respective $3/$5 provider-cost reservation budgets
per ledger Owner per UTC calendar month, including annual subscriptions, without rollover.
This is an assistance limit, not a redeemable balance or a payment credit.
Members of a shared ledger use its Owner's plan and combined allowances;
their personal subscriptions remain separate. Core expense history remains separate from
Creem platform payment facts. On 2026-10-06 the actual 36-case en/zh quality and
Python Worker transport checks passed: Preview enable/evaluated flags are now
versioned as `1`, while production flags and production D1 access remain `0`.
Current evidence is in [local acceptance](../../validation/local-acceptance.md).

## Current configurable defaults

| Allowance per ledger Owner (shared by members) | Free | Pro | Max |
| --- | ---: | ---: | ---: |
| Stored projects (standalone or GitHub-linked) | 3 | 20 | 100 |
| Current expense records | 100 | 20,000 | 100,000 |
| Successful protected write batches per UTC minute | 10 | 60 | 60 |
| Successful protected write batches per UTC calendar month | 1,000 | 10,000 | 10,000 |
| Jev provider-cost reservation per UTC calendar month | $0 | $3 | $5 |

Project and record capacities are user requirements. Write defaults remain
configurable engineering allowances, not validated production demand estimates.
These are aggregate ledger limits shared by its projects and shared pool, not
independent allowances per project. They are adjustable.

Stored project counts include archived and removed projects. Expense capacity counts
undeleted shared expenses and expenses whose project has not been removed;
archived projects still consume expense slots. Manual expense removal and project
removal free expense slots. Financial rows, immutable audits, creation replays and
occurrence identities remain internally retained, so active capacity is not a
physical storage ceiling. Removed expenses are excluded from ordinary expense
reads, reports and exports and cannot be restored. Authorized activity retains
its normal rolling 24-hour removal snapshot. Idempotent replay and repeat removal
do not increment usage. Downgrade preserves remaining history, exports and edits
subject to write protection; already-over-limit ledgers require manual cleanup.
Project counts and commercial write/model usage are not reset by plan changes.

Owner account Settings exposes `autoRemoveOldestExpense`, default false for every
plan, through cookie-only `GET/PATCH /api/v1/account/expense-retention` with its
own revision and required `If-Match` for PATCH. It belongs to the personal account
independently of the selected workspace, and governs all actors creating expenses
in that account's ledger. Changing the preference itself removes no expenses.
Disabled creates at capacity return `RECORD_LIMIT`. Enabled creates at exactly
capacity atomically soft-remove one globally oldest expense, ordered by
`occurredOn`, then `createdAt`, then ID, and insert the new expense. The oldest
target must be permitted to the current member/key: otherwise return
`RETENTION_TARGET_FORBIDDEN` without choosing a later permitted record. Above
capacity returns `RETENTION_CLEANUP_REQUIRED` without bulk deletion. Both modes
have no overage charge. Ordinary and recurring creates retain the original
credential, current Owner preference/plan, membership, target and idempotency
fences for the complete replacement transaction; replays never retire another
expense. Replacement consumes one commercial write and has zero net active-slot
growth. Failure of any audit, financial write or quota guard rolls everything back.

Authenticated `GET /billing`, `GET /billing/plan` and `GET /api/v1/me` return
`ledgerUsage` with `workspaceId` and `projects`/`expenseRecords` objects containing
`used`, the current effective-plan `limit`, and `remaining` clamped at zero.
Billing always shows the caller's personal ledger; `/api/v1/me` uses the selected
workspace and its Owner's plan. The same authorization batch reads the committed
cumulative project counter (owner-indexed row count before initialization) and
always counts current active expenses using the same predicate as mutation quota.
Legacy cumulative expense counters never authorize capacity. Reads never initialize,
reset or update the counters; the next normal mutation atomically reconciles the
expense counter from its final financial state.
Public pricing and revoked sessions never disclose personal capacity usage.

"Write rate" means how many changes an account may make in one minute, including
API-key clients. It protects against runaway scripts and rapid duplicate edits;
normal manual entry usually stays well below it. It uses fixed UTC minute
buckets, not a rolling 60-second window. A boundary burst can span two buckets.
The monthly cap separately bounds sustained usage. Each protected domain batch
counts once: project/category/expense/key creation. Jev reservation and suggestion
event batches do not consume commercial write slots; ordinary assisted expense
saves still count once. All batches retain independent D1 accounting.
Key revocation is exempt so exhausted quota cannot trap a compromised key.
OAuth and provider payment facts are not blocked by this commercial quota;
they still require the independent runtime admission/security controls.

## Operator configuration

Defaults live in `pullwise_server/ledger_plan_policy.py`. Supply a plain_text
Worker variable named `PULLWISE_PLAN_LIMITS_JSON` to override selected fields.
`config/ledger-plans.example.json` gives the full shape. For example:

```json
{"free":{"records":800},"pro":{"records":30000},"max":{"records":300000,"jevMonthlyBudgetUsd":"4.00"}}
```

Unknown fields, duplicate JSON keys, invalid/nonpositive limits and nonzero
Free Jev budgets are rejected. USD budgets use decimal strings, converted
to integer millionths of a dollar; never float currency arithmetic. Operational
limits must stay within the parser's bounded integer range of 1 through
1,000,000. Every plan's project and record overrides are independent.

The Max default remains below the existing 1,000,000-row operator envelope.
Quota counters, D1 parameter bounds and native Number conversion accept these
integer capacities without storing financial data inside account JSON. Money
reports use split integer aggregates proven through 1,000,000 records; project
and expense lists retain bounded pagination and CSV retains its streaming path.
This establishes compatibility of the configured capacity, not a load or latency
benchmark at 100,000 records.

This is an operator binding, not a public settings endpoint or browser variable.
Editing an example file alone does not update Cloudflare. Any remote settings
change must preserve existing bindings/secrets and the D1 pause and follow
deployment review. The separate product Preview is active with its test Secrets
and original bounded validation journal; production remains paused. Prices and product IDs remain owned by verified Creem catalog
facts, not by this allowance configuration.

## Jev cost rule

The confirmed model is `jev-1.13.0`. The
[official model reference](https://docs.typesafe.ai/models) lists $0.042 per
million input tokens, free output, and 64k request context. The model, price
and conservative 65,536-token maximum are pinned together in the policy module.
The user independently supplied the same model/price/context values.

Before a provider attempt the same guarded usage batch reserves **2,753
micro-USD** (ceiling of 65,536 × 42,000 / 1,000,000). An ambiguous provider
outcome retains this reservation; no automatic retry or speculative refund is
performed. This initial implementation reserves worst-case cost even for a
small successful request. It is a conservative provider-cost ceiling, not a
claim about actual invoiced usage or an amount a user is guaranteed to spend.
Verified token-based settlement/refunds are a later acceptance gate. Taxes,
Workers and D1 costs are not included in the Jev provider budget.

Each account's period is the UTC calendar month. The first legitimate write in
a new month replaces the period counters; it does not add unused old credit.
Annual subscriptions use the same monthly periods. A named constraint rejects
late requests that would move counters back to an older month/minute.

Jev has no daily attempt cap. The ledger Owner's shared UTC-month USD reservation
and request/response bounds remain the controls for model use. Historical daily
attempt rows remain untouched and no longer gate or count new calls. Free cannot
invoke Jev even with suggestions scope. Pro/Max eligibility does not imply that
Jev is active: public DTOs distinguish `eligible` and `available`. Preserve
enable/evaluated flags at 0 in each environment until its quality, metering and provider gates pass.
Those Preview gates passed on 2026-10-06; production activation remains separate.

Paid-plan assistance is part of ordinary `POST /api/v1/expenses` and expense PATCH,
including `expenses:write` API keys. No extra suggestion action or scope is
required. A create may omit `categoryId`; confidence at least 0.80 selects an
active existing category. Explicit category, amount, date and target are kept.
When category selection is uncertain/unavailable, create returns 422
`CATEGORY_REQUIRED` without saving; clients preserve the draft and request a
manual category. Edits always provide a category. The response `assistance`
contains advisory category/target/duplicate results and `categorySource`.
Duplicate hints use a bounded exact-purpose check on the same authorized target,
same currency/amount and seven-day date window; they do not reject a write.
GETs perform no inference. Idempotent create replay returns the stored response
without another provider call or reservation, including after category/project
archival, while still enforcing current key/owner/target restrictions.

## Atomicity and D1 cost impact

`PlanLimitedD1` injects exactly one usage UPSERT into the existing credential,
resource, idempotency and audit transaction. Cookies and all API keys for the
same owner share one primary-keyed row. Concurrent writes cannot overfill it.
First initialization counts the owner's stored projects once; later project
UPSERTs use lazy CASE branches and the cumulative total. Every mutation recounts
active expenses from its post-mutation state, including project removal and
ordinary/automatic expense removal. The usage statement is appended after the
original domain commands so it never interrupts adjacent `changes()` CAS guards.
GETs do not initialize or update usage. Failed business/credential/audit batches
roll back the counter and all prepared expense replacement commands.

Migration `0004_ledger_plan_usage.sql` is included in the initialized Preview
schema; production migrations remain a separate explicit operation. It adds a
table, its implicit primary-key index and DDL cost. Every protected write adds a usage
row write (and index effects on first insert); initialization adds project count
reads and expense capacity adds owner-indexed active-count reads. These scan
retained owner financial rows; removal frees active slots without deleting those
stored rows. Automatic replacement also adds one removal audit and ordinary
activity entry, while retaining the same commercial write charge.
These effects are included in cumulative validation accounting. Enabled ordinary
Preview product traffic no longer uses the historical lifetime
1,000-write/100,000-read test ceilings; generic finite validation retains its
original ceilings. The same journal preserves all earlier counters and evidence.
SQL batch/result counts are not billed row bounds. Existing imported data needs
a reviewed initialization/cardinality bound. The active Preview product uses its
reviewed per-SQL admission path; historical empty generic probe plans do not
block ordinary product requests.

The commercial quota is distinct from Preview SQL admission, native metering
and the finite validation plans' hard caps.
Production's explicit normal application path retains commercial/authentication
guards and does not require the temporary Preview coordinator. Its checked-in
D1 pause and separate provider activation remain; see the
[migration path](../../validation/production-migration-readiness.md).
