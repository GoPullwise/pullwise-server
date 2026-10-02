# Ledger plan policy

User decisions, 2026-09-28: Free has 3 projects; Pro and Max have 100 each.
Only Max has Jev, with a $5 budget per account per month, including annual
subscriptions, without rollover. Core expense history remains separate from
Creem platform payment facts. Jev activation/quality gates remain off.

## Initial configurable defaults

| Allowance per account | Free | Pro | Max |
| --- | ---: | ---: | ---: |
| Stored GitHub projects | 3 | 100 | 100 |
| Stored expense records | 500 | 20,000 | 20,000 |
| Successful protected write batches per UTC minute | 10 | 60 | 60 |
| Successful protected write batches per UTC calendar month | 1,000 | 10,000 | 10,000 |
| Jev provider-cost reservation per UTC calendar month | $0 | $0 | $5 |

Project/Jev decisions are user requirements. Record and write defaults are
initial engineering recommendations, not validated production demand estimates.
500 records is roughly a year at 40 records/month; 20,000 is about 200 records
per configured project when all 100 slots are used. These are aggregate account
limits, not independent allowances per project. They are adjustable.

Stored project counts include archived projects; stored record counts include
soft-deleted expenses because their rows, audits and idempotency remain stored.
Archival/deletion does not refund capacity. Idempotent replay and repeat removal
do not increment usage. Downgrade never deletes history: reads, exports and edits
remain available subject to write protection; above-cap new creations are denied.
Existing counters are not reset when limits change or the user changes plan.

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
{"free":{"records":800},"pro":{"records":30000},"max":{"records":30000,"jevMonthlyBudgetUsd":"4.00"}}
```

Unknown fields, duplicate JSON keys, invalid/nonpositive limits and nonzero
Free/Pro Jev budgets are rejected. USD budgets use decimal strings, converted
to integer millionths of a dollar; never float currency arithmetic. Operational
limits must stay within the parser's bounded integer range. Pro/Max record
allowances must match; change both explicitly or the configuration is rejected.

This is an operator binding, not a public settings endpoint or browser variable.
Editing an example file alone does not update Cloudflare. Any remote settings
change must preserve existing bindings/secrets and the D1 pause and follow
deployment review. A separate paused preview and its test Secrets are configured;
no remote schema or D1 runtime was activated. Prices and product IDs remain owned by verified Creem catalog
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

The shared daily assistance guard (20/day) and
request/response bounds remain additional safety controls. Free/Pro cannot
invoke Jev even with suggestions scope. Max eligibility does not imply that
Jev is active: public DTOs distinguish `eligible` and `available`. Preserve
enable/evaluated flags at 0 until real quality, metering and provider gates pass.

Max assistance is part of ordinary `POST /api/v1/expenses` and expense PATCH,
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
First initialization counts the owner's stored projects/records once; later
UPSERTs use lazy CASE branches and stored totals. GETs do not initialize or
update usage. Failed business/credential/audit batches roll back the counter.

Migration `0004_ledger_plan_usage.sql` is unexecuted remotely. It adds a table,
its implicit primary-key index and DDL cost. Every protected write adds a usage
row write (and index effects on first insert); initialization adds count reads.
These effects must be included in S18's cumulative 1,000-write/10,000-read budget.
SQL batch/result counts are not billed row bounds. Existing imported data needs
a reviewed initialization/cardinality bound. The remote allowlist remains empty.

The commercial quota is not the global validation hard cap and does not solve
unbounded unauthenticated OAuth/provider traffic or total multi-account billing.
The independent validation coordinator, D1 pause, provider gates and cost review
remain necessary before any remote activation.
