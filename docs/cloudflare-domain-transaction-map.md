# Server transaction mapping to local D1 — 2026-09-23

This is an executable storage-boundary experiment and a migration map. It is
not a deployed Server, a replacement ProductStore, or a CF2 pass. Production
Jev and live GitHub remain disabled. No account, credential, price, product ID,
callback or payment-provider configuration was changed.

## Tested boundary

`cloudflare/probe/src/server_mapping.py` emits finite prepared statements over
the actual Server tables. `tests/export_d1_server_fixture.py` creates a new
synthetic ProductStore and exports its schema/data into an ignored probe module;
it never accepts a database path or exports a user database. The same command
builders run under SQLite unit tests and local workerd D1 `batch()`.

Every command list is one batch. Conditional updates followed by CHECK guards
turn zero-row CAS into failure before commit. Cloudflare documents whole-batch
rollback on a statement failure in its [D1 binding API](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch).
Separate awaits for the statements would invalidate the proof.

| Existing Server authority | D1 mapping | Current evidence / remaining work |
| --- | --- | --- |
| `background_jobs`, `source_records`, `source_contexts`, `processing_usage_ledger` | Frozen source/version/context/config/auth/owner and live reservation guard; current eligible job becomes running with a 120-second token | Local D1 competing claims admit one. Queue selection/fairness selection, terminal invalidation/release and full scheduler adaptation remain outside this command |
| `provider_attempts` | Owner/global monthly and inclusive last-60-second counts plus the new attempt in the claim batch | SQLite tests show budget rejection rolls the job back; local D1 executes the combined command. Existing isolated monthly/rolling rollover tests remain separate evidence |
| `assessments`, `source_assessment_publications`, `items`, `item_versions` | Validate every source and context plus lease, then insert immutable result and CAS the current Item version | Local D1 rejects a changed secondary dependency and an expired/stale claim; current test result is synthetic, not model-quality evidence |
| `processing_usage_buckets`, `processing_usage_ledger`, `background_jobs` | Result, ItemVersion, reserved→used and succeeded state in the publication batch | Local D1 final reservation failure rolls back earlier result/version inserts. Replay after process restart cannot consume again |
| `app_state.users`, `account_entitlement_authority`, `d1_claim_authority` | Match the exact persisted account entry and monotonic entitlement revision, effective period and time limit before claim and publication; freeze the revision at claim | Local D1 rejects stale or dirty projection, expiry, A→B→A replay and a result from an older claim revision. Other account writers must participate in this protocol before production use |
| `app_state.billingEvents` | After the existing billing handler accepts a fact, the candidate batch updates that event record and the affected stored user, increments the entitlement revision and marks it dirty together | Synthetic event payload persists, duplicate event rejects atomically, and existing event/other-account records survive. This does not execute a Creem webhook in Workers |

## Account and Creem write boundary

Current `db.save_state`/`save_state_item` persist JSON state through
`state_for_storage`, which protects secret fields. `load_state`/`load_state_item`
restore it through `state_for_runtime`. A D1 adapter must preserve encrypted
records, account IDs, sessions, OAuth state, GitHub access, API-key hashes,
customer/subscription references and payment history. Loading empty product
tables is not an account migration.

The current Creem handler remains the authority for raw-body signature checking,
duplicate/late/pending event association and subscription lifecycle. Do not add
a second payment state machine to the Worker or to Web. Its accepted payment
fact must survive independently of a failed entitlement projection. Before an
analysis command can run, the projection must correspond to the current durable
payment/account revision and effective period; stale or absent projection must
deny admission, not reuse the old paid limit.

The finite local mapping now has `account_entitlement_authority` and
`d1_claim_authority`. A previously accepted synthetic event updates the matching
user entry and billingEvents entry, increments the owner revision, and marks the
projection dirty in one batch. The trusted projection command checks that same
persisted entry and revision, then records plan, period, limit and a strict
validUntil boundary. Claim additionally checks the ledger's period and freezes
the revision; publication checks it again. An old claim cannot publish after
event A→B→A even when the final user JSON equals the original. Expiry denies a
claim or publication at the boundary. Local SQLite and workerd/D1 tests cover
these transitions and a real restart.

The experiment still has **no actual Creem handler or account persistence
adapter on Workers**. The production caller must provide the existing handler's
validated/deduplicated event record and `state_for_storage` user payload; every
accepted account/checkout/Creem write must bump the durable revision. A missing
writer would break the A→B→A fence. Projection must calculate the current
effective plan and expiry from the persisted account using Server's entitlement
rules, then CAS the revision. Annual billing still releases monthly quota,
upgrades do not reset usage, and a checkout return URL never grants paid rights.
Keep those cases in the existing billing protection tests while adding target
runtime equivalents.

No model result, transaction rollback, or compensation may erase a valid Creem
event, change an amount/currency, or manufacture provider confirmation. A
provider response loss is not exactly-once model execution; attempt spend is
conservatively retained while successful processing remains charge-key guarded.

## Scope still requiring adaptation

- Full ProductStore async reads and consistent authorization-filtered list/count
  snapshots; no use of a Worker/Container local SQLite file as durable storage.
- Source-only and cached publication, thread membership CAS for projections
  computed outside D1, adjacent handling inheritance, atomic multi-source
  context links, and safe cache upsert. The current mapped publication is a
  first-result/one-Item case; it deliberately rejects duplicate inserts.
- Queue fairness selection, retry/backoff, cancellation/revocation releases,
  webhook receipts and cron execution over the same authority. Existing local
  protocol probes validate mechanisms, not all Server commands.
- Connect all real account/Creem writes, encrypted payload handling, pending and
  late event behavior, and trusted entitlement calculation to the mapped revision
  protocol. The local synthetic expiry/revision proof is not that integration.
- Real Server HTTP on Workers, real provider bounded exit and actual model
  quality. The new Web HTTP integration uses local CPython and the Web proxy
  function in Node, with synthetic cookies/keys; it is not a browser or remote
  Workers deployment test.

## Reproduce

From `pullwise-server`, with TEMP/TMP set to the workspace test directory:

```powershell
$env:PYTHONPATH=$PWD.Path
D:/Python313/python.exe -m pytest tests/test_cloudflare_server_mapping.py -q
D:/Python313/python.exe tests/export_d1_server_fixture.py
```

Use the probe README's existing tool/cache environment and start its pinned
Wrangler locally on **8796**, using `--persist-to .wrangler/server-map-state`.
Run `D:/Python313/python.exe verify_server_mapping.py` from the probe directory.
Stop that process, restart with the identical persist directory, then run
`D:/Python313/python.exe verify_server_mapping.py --after-restart`. The driver
bypasses environment proxies for loopback. No schema/SQL upload endpoint exists.
