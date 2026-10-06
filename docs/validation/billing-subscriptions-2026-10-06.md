# Preview payment and subscription audit — 2026-10-06

The user requested subscription success/failure validation without real payment.
This audit makes no checkout, purchase, refund, cancellation, or subscription
request to Creem. Provider responses and signed webhook payloads are synthetic;
the signing secret exists only in the local tests. It does not establish payment
processor acceptance or remote preview webhook delivery.

## Repairs

- A `subscription.update` or `checkout.completed` payload with missing/unknown
  subscription status previously defaulted to active. It is now ignored without
  granting paid access. Explicit lifecycle events retain their documented status.
- A delayed active event for an old subscription absent from saved history could
  clear the current subscription before checking timestamps. Timestamp checking
  now precedes a change in subscription identity, preserving a newer current plan.
- Scheduled cancellation and resume now require the provider to acknowledge the
  requested status. An unchanged subscription no longer produces success or a
  contradictory local state. Confirmed resume clears the cancellation timestamp.
- Default and rejected external checkout returns now lead to Billing.
- Server and Web reject billing redirect credentials, nonstandard provider ports,
  misleading host suffixes, and literal control characters.
- Creem responses now enforce the existing 1 MiB limit as bytes arrive; rejected,
  oversized, interrupted, and malformed-length bodies are cancelled. Provider
  errors expose only the numeric HTTP status. Timeouts, redirects, and 5xx retain
  an uncertain upgrade claim; definite 4xx rejection releases it.
- Native acceptance found that the Worker classified provider `ValueError`
  failures, including definite rejection, as invalid caller input (422).
  JSON/Unicode validation now has its own error boundary; provider/configuration
  failures return 503 `BILLING_UNAVAILABLE`, without revealing response text.

The provider contract was checked against the official
[webhook event payloads](https://docs.creem.io/code/webhooks) and
[subscription cancellation API](https://docs.creem.io/api-reference/endpoint/cancel-subscription).
Checkout completion is a signed fact, not a browser return parameter. Upgrade
acknowledgement continues to return pending while the existing plan remains active.

## Local evidence

Python 3.10.12, 91 passing tests:

```sh
.venv/bin/python -m pytest \
  tests/test_cloudflare_billing_mutations.py \
  tests/test_billing_account_rules.py \
  tests/test_creem_event_rules.py \
  tests/test_cloudflare_creem_handler.py \
  tests/test_cloudflare_creem_gateway.py \
  tests/test_billing_lifecycle.py \
  tests/test_cloudflare_billing_read.py \
  tests/test_creem_catalog_projection.py \
  tests/test_cloudflare_billing_catalog_write.py
```

The lifecycle tests exercise the same payment modules over a transactional local
D1-shaped SQLite adapter: Free checkout → signed Pro success → pending Max annual
upgrade → signed unpaid failure → signed Max recovery. They also verify duplicate
delivery, invalid signatures, conflicting owner facts, lost provider responses,
definite rejection, cancellation/resume, period end, past due, unpaid, paused,
cancelled, expired, dispute, partial refund and full refund. Upgrade retries make
one provider request; unknown outcomes cannot silently repeat a charge. Payment
updates preserve an existing business expense, category and expense revision,
and create no expense audit event.

The existing preview diagnostic/redaction regression also passes:

```sh
.venv/bin/python -m pytest tests/test_preview_product_budget.py -k creem
```

The Worker error-boundary and Unicode regression group has 111 passing tests
(overlapping the payment group above). Eight new Worker tests distinguish
malformed caller JSON/UTF-8/surrogates from definite provider rejection, unknown
outcomes, timeout, provider Unicode failure, and invalid provider configuration.

```sh
.venv/bin/python -m pytest tests/test_worker_application_security.py \
  tests/test_jev_preview_unicode.py tests/test_cloudflare_billing_mutations.py \
  tests/test_cloudflare_creem_gateway.py tests/test_billing_lifecycle.py
```

Web billing and redirect tests: 70 passing tests. They cover provider-host
redirects, cancellation/resume and upgrades, pending signed confirmation, user
errors, rapid-click coalescing, checkout timeout/history recovery, and unmounted
mutation completion. Dedicated changed-file ESLint and Prettier checks pass.

```sh
npm test -- src/lib/trusted-redirects.test.js src/screens/billing.test.jsx
npx eslint src/lib/trusted-redirects.js src/lib/trusted-redirects.test.js
npx prettier --check src/lib/trusted-redirects.js src/lib/trusted-redirects.test.js
```

Worker module synchronization and `--check` pass. The full repository/native
acceptance and preview publication evidence belongs to the release record, not
to this local payment audit.

## Per-record storage repair

The previous global `users`, `billingEvents` and `billingPendingUpdates` JSON rows
each shared an 8 KiB ceiling. Pure synthetic reducer serialization reproduced
overflow with 12 subscription history entries (8,397 bytes in the users map),
58 audit events (8,295 bytes), or 15 pending updates (8,686 bytes). These were
storage-shape failures, not sensible product request restrictions.

Account/payment commands now use exact `record:users:<owner>` rows (512 KiB per
user) and independent `record:billingEvents:<event>` /
`record:billingPendingUpdates:<event>` rows (8 KiB each). Ordinary HTTP ingress
remains 8 KiB. Billing reads never perform implicit migration or scan all account
payloads into Python. Owner discovery returns at most two metadata matches;
settlement repeats its uniqueness fence in the atomic transaction. Reconciliation
loads only matching pending event IDs with an explicit 1..16 limit. Unknown-owner
pending updates retain the existing 1,000-record bound.

Immutable `billing_webhook_receipts` remain replay authority. The exact account
snapshot, owner revision, normalized receipt and optional parked record are
guarded together before account/audit/entitlement changes and receipt settlement.
An unrelated account, audit event or pending update no longer invalidates a
whole-map snapshot. Existing receipt hashes, updates, state, account authority
and expense facts are preserved by the explicit cutover. A deliberately
unsupported legacy audit event without a signed receipt is retained; a new signed
delivery with that ID fails closed with its receipt pending, preserving the
account and audit rather than fabricating proof that the legacy event was paid.

The expanded payment/account group passes **97 tests and 11 subtests** on
Python 3.10.12, including seven new storage regressions: actual 100-entry billing
history plus 1,000 cached repositories, more than 58 unrelated audit events,
1,000 pending records and duplicate admission, receipt replay after removing its
audit cache, bounded ordered reconciliation, ambiguous-owner creation between
lookup and commit, unrelated simultaneous mutations, and receiptless legacy
authority rejection. Signed paid → unpaid → old-paid replay preserves Free access,
and retained credentials/repositories and history limits remain intact.
Including `tests/test_worker_application_security.py`, the same group passes
139 tests and 11 subtests; caller-validation and provider-failure HTTP semantics
remain distinct after the storage change.

```sh
.venv/bin/python -m pytest -q \
  tests/test_cloudflare_billing_mutations.py tests/test_billing_lifecycle.py \
  tests/test_billing_record_storage.py tests/test_cloudflare_account_adapter.py \
  tests/test_cloudflare_creem_handler.py tests/test_cloudflare_billing_read.py \
  tests/test_billing_account_rules.py tests/test_creem_event_rules.py \
  tests/test_cloudflare_creem_gateway.py tests/test_cloudflare_billing_catalog_write.py
```

Module synchronization and its `--check` pass for this repair. These are isolated
local tests; native cutover/capacity and eventual preview publication are tracked
in their separate release evidence. No real payment/provider request is made.
