# Server language reassessment

Reviewed 2026-09-28 against the current ledger, Cloudflare target and Jev SDKs.
Recommendation: **TypeScript is the better long-term default for this Server**;
retain Python for the current reviewed implementation until an independent
migration passes equivalent local contract, security and payment tests.
This is a recommendation, not authorization to rewrite or deploy.

## Evidence and tradeoffs

| Dimension | Current Python | TypeScript target |
| --- | --- | --- |
| Existing business behavior | Reuses account/payment rules and Python/SQLite regressions | Requires a deliberate port of all invariants and fixtures |
| Cloudflare D1/DO/streams | Supported; current source uses Python SDK and JS/FFI adapters | Direct runtime APIs with generated types; fewer cross-language adapters |
| Jev | Official Python SDK exists; current Worker uses bounded direct HTTP | Official JS/TS SDK infers answers from questions; Worker compatibility still needs a local proof |
| Money/security | Integer minor units and tested parsing are already present | Use integer/BigInt/decimal parsing; never replace with float shortcuts |
| Web/client contract | OpenAPI remains shared across project boundary | Same REST contract; shared wire types become easier without merging projects |
| Development/tooling | Python reference + Python Worker + Node/Wrangler tooling | One Server language and native Workers tooling; expected maintenance simplification |
| D1 cost | Driven by SQL, indexes, guards and traffic | Same; language change alone does not reduce billed rows |
| Transition risk | Low while retaining current implementation | OAuth, Cookie/key authorization, atomic guards, Creem replay and CSV need full regression preservation |

Cloudflare documents both [Python as a first-class language](https://developers.cloudflare.com/workers/languages/python/)
and [fully typed Workers APIs for TypeScript](https://developers.cloudflare.com/workers/languages/typescript/).
Jev has official [Python](https://docs.typesafe.ai/sdk/python) and
[JavaScript/TypeScript](https://docs.typesafe.ai/sdk/javascript) SDKs. Therefore
neither "Python unsupported" nor "Jev TS-only" is a valid reason to migrate.

The recommendation is an engineering inference from Pullwise's workload:
REST, D1, authorization, payment facts and structured API calls, rather than
Python-specific ML/data-science processing. Current FFI stream/binding handling,
module mirroring and two interpreter/tooling environments add integration work.
Python's strongest advantage here is the existing implementation and evidence.
No authoritative historical ADR proving the original language rationale was found.

No performance, cold-start or cost comparison has been benchmarked, so this
evaluation makes no numerical speed/billing claim. The JS SDK documentation
uses Node 20+; compatibility with the chosen Workers flags must be verified,
not assumed from npm availability. Default SDK retries must be disabled for
bounded validation/provider work. Current direct Python HTTP has no retry loop.

## Migration boundary if selected

1. Freeze OpenAPI, SQL schema, money parsing, permission and billing invariants.
2. Build a local TypeScript candidate in the Server project; test with synthetic
   provider transports and SQLite/local bindings only. Preserve Web separately.
3. Port all resource, revocation, Origin, idempotency, audit, revision, quota,
   currency/report, CSV and payment replay cases. Prove equivalent behavior.
4. Verify JS Jev SDK requests against mocks with retries disabled; make no live
   Jev call while quality and metering gates are pending.
5. Select one authoritative Server runtime. Do not introduce permanent Python/TS
   dual writes or a third independently deployed product service.
6. Recalculate SQL/migration/provider validation bounds and obtain the required
   concrete release review. Keep the existing production D1 switch at 0.

Language migration is independent of S17/S18 activation. It does not fix a
missing browser connection, test products, unknown D1 bounds or provider validity.
