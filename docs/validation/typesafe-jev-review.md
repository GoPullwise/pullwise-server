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
older test-stop notes. This file does not claim real evaluation has completed.
Production database activation, payment acceptance, and real GitHub login are
independent product checks; synthetic model evaluation cannot prove them.

Focused local verification covers request ordering/context bounds, pre-fetch
admission, streamed response cancellation, optional failures and budget exhaustion,
maximum valid expense payloads, Unicode input, and evaluation metric arithmetic.
The root task records the final command results and any real-provider evidence.
