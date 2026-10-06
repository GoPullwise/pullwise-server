# TypeSafe Jev integration review

Reviewed 2026-10-06 using the live official documentation. This review covers
the expense ledger's model tasks, request/response contract, Worker transport,
and the fixed bilingual evaluation design. Local simulations are engineering
checks and are not evidence of real model accuracy.

## Official contract and model choice

- [Models](https://docs.typesafe.ai/models): `jev-1.13.0` is the current pinned
  model. Input costs $0.042 per million tokens; output is free. Requests have a
  64k total-token window and a separate 32k window for state plus the longest
  question. English is its strongest language; Chinese requires direct evaluation.
  Pinning the version keeps thresholds stable across alias changes.
- [HTTP API](https://docs.typesafe.ai/api): use
  `POST https://api.typesafe.ai/v1/systemone` with a server-side Bearer secret.
  Choice responses include the chosen option, probabilities for every option,
  confidence, the returned model, and token usage. Choice permits up to 255
  options. The current validator requires the exact expected questions/options,
  finite probabilities totaling one, and a highest-probability choice.
- [Choice](https://docs.typesafe.ai/primitives/choice): the ledger's existing
  category IDs and project/shared/uncertain labels are appropriate closed answer
  sets. Related questions share one request; an explicit uncertain option permits
  withholding a suggestion instead of forcing a label.
- [State](https://docs.typesafe.ai/concepts/state) and
  [structured questions](https://docs.typesafe.ai/primitives/advanced): the
  current `purpose`/`note` state supplies relevant text directly. Questions define
  judgments rather than embedding them in user text. The ledger deliberately
  accepts the narrower text-only/string-description subset it actually uses.
- [Confidence](https://docs.typesafe.ai/confidence): use confidence for routing,
  not the top probability as if both numbers were identical. Choice confidence
  is `(p_max - 1/n) / (1 - 1/n)`. The existing 0.80 threshold means a top
  probability of about 0.867 for three choices, and about 0.806 for 31 choices.
  Its performance must be measured for each task and language.
- [Jev 1.13 limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13):
  precise literal criteria, adversarial text, irrelevant context, and option order
  need attention. Arithmetic, exact date comparisons, and free-text generation
  belong outside this model.

The product uses Jev to classify an expense into an existing category and give
an advisory cost-scope hint. These are suitable semantic decisions. Currency
conversion, money totals, duplicate candidate bounds, date windows, authorization,
idempotency, budgets, and final persistence stay in code. Explicit user category,
target, money, and date values retain precedence. A model failure never prevents
a manual write with an explicit valid category.

## Repairs from this review

1. `build_request` previously sorted every JSON object, including Choice options.
   That discarded deliberate permutations and replaced the database's category
   order with opaque-ID order. It now preserves caller criteria order while
   retaining deterministic normalization elsewhere.
2. The 48 KiB complete-request limit alone did not constrain the smaller
   state/longest-question window. A separate 24 KiB UTF-8 envelope now leaves
   headroom for provider formatting. This is a conservative input bound, not
   a tokenizer or an actual-token settlement mechanism.
3. `WorkerJevGateway` now validates the complete fixed-model/text-only request
   before fetch. Blank or null secrets cannot enable it. Rejected response
   bodies and interrupted or oversized streams are canceled. The fixed endpoint,
   manual redirects, single attempt, 3-second deadline, and 64 KiB response cap
   remain in force. Provider bodies and credentials are not returned to clients.
4. Question version `ledger-suggest-v2` refers directly to `purpose` and `note`,
   treats expense/category text as data, ignores requests to select labels, and
   defines uncertainty for insufficient, conflicting, or equally fitting inputs.
   Scope selection requires explicit usage evidence. A vendor or category alone
   cannot establish whether a cost is shared.
5. The offline evaluation previously treated both tasks' uncertainty as one
   flag and could hide a weak language behind aggregate accuracy. It now measures
   category and target independently, accepts explicit uncertain gold labels,
   reports both languages separately, and checks reordered variants. Missing
   recorded predictions are rejected rather than inferred from labels.

## Fixed small real-provider evaluation

`tests/fixtures/jev-ledger-synthetic-v2.jsonl` contains **36 agent-authored
synthetic inputs with gold labels** and no predicted results:

- Each language has 12 clearly described expenses covering six categories,
  three uncertainty/boundary cases, and three repeated cases with reversed options.
- Boundaries include a clear category with unstated scope, an unspecified monthly
  bill, overlapping category descriptions, and quoted instructions trying to
  influence an otherwise clear expense.
- There are 18 English and 18 Chinese requests, with six option-order comparison
  pairs. For a `reverseCriteria` case reverse each generated Choice criteria map,
  including the uncertain entry, before passing it through `build_request`.

The approved conservative whole-context reservation is 2,753 micro-USD per
attempt. **36 attempts reserve $0.099108**, below $0.10. Reserve the complete
36-call allowance before execution, run sequentially, make no automatic retries,
and retain reservations for timeouts or unknown outcomes. This test requires no
ledger D1 writes; Worker runtime and provider transport are separate checks.
Actual provider usage should be retained as numeric evidence, not treated as a
refund or a guarantee of the final invoice.

Record actual normalized model responses and independent actionable predictions
for each fixed ID. Predictions are null whenever the returned label is uncertain
or confidence is below 0.80. Append `modelVersion`, `questionVersion`,
`providerOutcome`, and the `agent-authored-synthetic` provenance. A timeout,
HTTP rejection, invalid schema, or exception must not be replaced with gold-label
predictions. Use `scripts/evaluate-ledger-suggestions.py` on the resulting JSONL.

The harness permits review only with complete pinned-model/v2 valid-response
evidence, no false actionable hints, at least 85% coverage of clearly labeled
examples separately per language/task, correct withholding on each uncertain
case, and all six order pairs consistent. These are conservative checks on this
small synthetic suite; passing them does not establish a customer-data error rate.
The gate reports evidence and does not change any deployment binding.

## Enablement and remaining evidence

Both `PULLWISE_JEV_SUGGESTIONS_ENABLED=1` and
`PULLWISE_JEV_SUGGESTIONS_EVALUATED=1` plus a configured `TYPESAFE_API_KEY` are
required for ordinary product calls. Max eligibility, the atomic monthly USD
reservation, and the shared 20-attempt daily limit still apply. Real evaluation
must exercise the actual Python Worker fetch/FFI path with SDK retries absent,
record model/usage/latency results, and retain manual fallback behavior on failure.

The latest user goal authorizes completing preview validation and supersedes
older test-stop notes. The real evaluation below has now completed. The current
scope is preview only; no production changes were performed for this evaluation.
Payment acceptance and real GitHub login remain independent product checks.

Focused local verification covers request ordering/context bounds, pre-fetch
admission, streamed response cancellation, optional failures and budget exhaustion,
maximum valid expense payloads, Unicode input, and evaluation metric arithmetic.
The root task records the final command results and any real-provider evidence.

## Actual preview results: 2026-10-06

The fixed suite completed **36 real TypeSafe requests, 36 valid responses,
zero transport/schema errors and zero automatic provider retries** through the
unmodified canonical `WorkerJevGateway` Python Worker fetch/FFI path. Every
response returned `jev-1.13.0`; every request used `ledger-suggest-v2` and the
original agent-authored synthetic fixture. Only purpose/note and the generated
criteria were sent to the provider. Gold labels were joined locally after each
actual response; simulated predictions were never used as provider evidence.

| Language | Judgment | Clear labels correctly suggested | Expected unknown correctly withheld | False actionable hints |
| --- | --- | --- | --- | --- |
| English | Category | 16 / 16 | 2 / 2 | 0 |
| English | Project/shared scope | 16 / 16 | 2 / 2 | 0 |
| Chinese | Category | 16 / 16 | 2 / 2 | 0 |
| Chinese | Project/shared scope | 16 / 16 | 2 / 2 | 0 |

Each language had 18 requests. Clear coverage and selective accuracy were 100%
separately for each judgment and language. The denominators include three
reordered repeats per language and the independently labeled boundary cases.
All six reversed-option pairs agreed with their base cases. Category and scope
were withheld independently: six requests withheld at least one field, including
cases with a useful category but insufficient scope evidence. The fixed suite's
`providerEvidenceComplete` and `eligibleForReview` gates both passed. These
results support preview review for these advisory tasks; 36 synthetic cases do
not estimate customer-data error rates or establish calibrated confidence.

The provider reported 23,850 input tokens and 4,144 output tokens. At the
documented input price, the usage-derived estimate is $0.0010017; it is not an
invoice. The full approved **$0.099108 reservation remains recorded**. Observed
gateway latency was 65–1,244 ms, median 100 ms and nearest-rank p95 184 ms.

Inspectable evidence:

- [Normalized actual responses](jev-synthetic-v2-results.jsonl): fixed IDs,
  actual chosen labels/confidences, independent actionable predictions, returned
  model/question version, numeric usage/latency, synthetic provenance and gold
  labels. It contains no credential, raw provider body or customer data.
- [Per-language/per-field aggregate](jev-synthetic-v2-aggregate.json).
- [Execution, source hashes, original journal and cleanup](jev-synthetic-v2-execution.json).
- [Original inputs and gold labels](../../tests/fixtures/jev-ledger-synthetic-v2.jsonl).

Reproduce the metrics offline from the repository root:

```sh
python scripts/evaluate-ledger-suggestions.py docs/validation/jev-synthetic-v2-results.jsonl
```

## Transport recovery and preview cleanup

The original attempt journal was preserved rather than reset. Its first two
HTTP calls returned `config_unavailable` from a local Worker before provider
dispatch. They are retained as two bootstrap failures with zero provider calls,
not model predictions. A separate provider phase was claimed in that same
$0.099108 reservation only after a real authenticated readiness response passed.
Every real-case reservation was fsynced before its single POST; exclusive client
locking, fixed IDs and a loopback bridge enforced the cumulative 36-call cap.
Unknown outcomes would retain reservations, and two actual/unknown failures would
stop execution. The supplemental Worker replay set was isolate-local; the single
secret-bearing controlled client and durable journal provided the cumulative cap.

Earlier bootstrap failures remain visible in the execution artifact:

1. A local-port collision prevented the initial private remote session. The
   subsequent two local configuration failures made no provider requests.
2. Wrangler's local workerd remote proxy could not use this environment's
   required egress proxy. Metadata probes failed before model dispatch.
3. A temporary Node adapter used the SDK's opaque preview host/auth headers in
   memory and the environment HTTP proxy. Private Python edge-preview metadata
   returned HTTP 503 with non-JSON output in ordinary mode; its diagnostic body
   was not persisted. Minimal mode returned HTTP 503 / Cloudflare error 1105.
   The 29 SDK files were byte-identical to the normal preview package. A single
   private JavaScript metadata control returned 200, isolating that limitation
   from model transport, secret validation and permanent route settings.
4. The local wrapper runtime initially failed before startup because its SDK
   config directory was unavailable. Supplying the existing session's XDG config
   path resolved it; subsequent native SDK authentication/exports checks passed.
   Two initial regular-preview wrapper readiness probes returned 404; the first
   retained its status only, and the second recorded product `NOT_FOUND`. The
   final wrapper used a private product-module import and explicit
   `__all__ = ["Default", "ValidationBudget"]`. Native SDK checks confirmed one
   export of each class, and the next single authenticated metadata probe
   returned 200 with the actual secret-present/enabled booleans and product
   flags still zero. The preceding 404s did not reach the evaluation route;
   their precise cause is not uniquely established by this recovery.

The successful temporary wrapper was published only to `pullwise-server-preview`
with locked Pywrangler, SDK 1.9.0 and Wrangler 4.136.3. It retained the canonical
product entry byte-for-byte, delegated all ordinary routes, and exported the same
`ValidationBudget` class. A separate prefix required a new random 256-bit auth
Secret, a fixed 30-minute deadline, preview mode, ordinary Jev flags both zero,
128-byte ingress and one of the fixed 36 IDs. Its narrow evaluation environment
held only the TypeSafe credential and temporary local enablement flags; the
evaluation route made zero D1/coordinator calls. No migrations, cron, database
binding changes, coordinator resets or production operations were performed.

After the run, `finally` restored canonical preview version
`a38a11a5-676d-414e-80cc-51208b72ada2`, stopped the bridge, and deleted
`PULLWISE_JEV_EVAL_AUTH_TOKEN`. Secret-name metadata confirmed its removal and
retention of `TYPESAFE_API_KEY`. The existing preview D1 database and fixed
coordinator namespace/scope were preserved. Both ordinary product Jev flags
remained `0`; passing the synthetic gate did not automatically enable them.

## Reviewed preview activation

The root agent independently recomputed the metrics from the normalized actual
responses, then enabled advisory suggestions only on preview at 07:17 UTC on
2026-10-06. The unchanged canonical source `2ed7271` was deployed with the locked
Pywrangler toolchain and an explicit runtime overlay:

```sh
uv run --frozen --python 3.14.2 pywrangler deploy \
  --config wrangler.preview.jsonc \
  --var PULLWISE_JEV_SUGGESTIONS_ENABLED:1 \
  --var PULLWISE_JEV_SUGGESTIONS_EVALUATED:1
```

Published version `a7470c2d-55f7-4f23-a3e0-2f920b6ef4b6` received 100% traffic.
A management read-back verified both flags `1`, mode `preview`, the original D1
database and fixed validation namespace, and the existing TypeSafe Secret name.
The preliminary connector settings PATCH was denied access; the unchanged
flags were read back before the CLI publication. No credential value was exposed.
The deploy and read-back made no model calls or migrations and did not touch
production. Max eligibility, daily/monthly reservation, explicit user choices
and failure fallback remain enforced.

The repository config deliberately retains safe defaults `0`; this reviewed
runtime overlay must be retained deliberately on a later preview deploy. This
activation follows the actual evidence review, rather than the evaluator
silently changing deployment bindings. It does not establish payment or
authenticated end-user acceptance.
