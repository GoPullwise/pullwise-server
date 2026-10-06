# Pullwise Server

Cloudflare Python Worker modules for the [GitHub project expense ledger](docs/design/github-project-ledger/README.md).
The Server owns GitHub identity/repository authorization, Cookie/API-key
security, shared ledgers with Owner/Admin/Editor/Viewer roles, multi-repository
projects, categories, exact money, reports, paginated CSV and optional suggestions.
Creem subscriptions remain separate from expenses.

`cloudflare/server/src/entry.py` is the Worker entry. `pullwise_server/` owns
the implementation; `cloudflare/server/sync_server_modules.py` generates the
ignored Worker mirror. The [ledger OpenAPI](openapi/ledger-v1.yaml) is the shared
business contract.

## Current version (2026-10-06)

The multi-repository, Organization and shared-ledger version is implemented
and released to preview with the v5 schema upgrade. Separate local role/browser
evidence and actual remote publication checks are in
[latest acceptance](docs/validation/local-acceptance.md). Original-version
acceptance remains historical rather than new-role evidence.

A workspace ID is the existing ledger owner ID. Personal ledgers retain an
implicit Owner; inviting members shares that ledger's current and future finance
data without copying or rewriting expense history. Invitations resolve a GitHub
username to a stable user ID, expire after 24 hours and store only a one-time
token's hash. GitHub Organization membership grants no ledger role. The Owner's
plan, write allowances and model budget serve the whole ledger; each member's
personal subscription remains separate.

Projects explicitly bind 1–30 currently authorized repositories and may have an
optional name and Organization association. Repository visibility and new-target
eligibility use the actual actor's GitHub credentials. Existing finance history
remains usable according to ledger role when repository access is lost.

Select a ledger with `X-Pullwise-Workspace`; native browser CSV links use the
`workspaceId` query parameter. Workspace-scoped keys retain their membership
revision and intersect key scopes, current role and target restrictions. Legacy
unscoped keys remain personal. See the [version requirements and migration
status](docs/planning/project-repositories.md) for roles, compatibility and release
gates.

## Offline verification

Use Python 3.10.12 with the deployment/test tools available:

```bash
python -m pytest tests
python scripts/check-ledger-s01.py --allow-placeholders
python cloudflare/server/sync_server_modules.py --check
bash -n scripts/deploy-cloudflare.sh
```

Tests use synthetic SQLite D1 fixtures. Current results and unverified runtime
behavior are in [local acceptance](docs/validation/local-acceptance.md).

## Configuration and deployment

Preview and production have separate `cloudflare/server/wrangler.<environment>.jsonc`
configs and D1 databases. `cloudflare/server/.dev.vars.example` lists local
Worker variable names; credentials belong in Secrets and must not be committed.
`scripts/deploy-cloudflare.sh` defaults to dry-run and rejects placeholder
domains/database IDs. Jev stays disabled until real-provider quality/runtime
gates pass. The user authorized self-authored synthetic en/zh evaluation samples;
their results must be recorded as synthetic-data validation.

The current user request authorizes checks, fixes, main pushes and Cloudflare
publication. The latest scope also removes artificial lifetime request/read/write
test ceilings from enabled preview product traffic. The existing journal retains
all cumulative reservations, observed usage and prior evidence; each SQL batch
still needs bounded admission and native metering. Generic finite validation
keeps its original ceilings. Production D1 access remains paused. Do not reset
the journal, add cron triggers or copy preview credentials/data into production.
The user's USD 200/month Cloudflare target is supported by preview abuse limits
and avoiding repeated full-table cardinality scans, without hard daily/monthly
row caps. Rate counters use bounded hashed DO subjects and return recoverable
429 responses; they add no D1 writes. Remote acceptance stays low frequency.
Keep remote validation finite and record its row reservations and actual results.
Publication is separate from authenticated runtime/provider acceptance. See the
[deployment guide](cloudflare/server/README.md) and current acceptance record for
release commands and remaining gates.
