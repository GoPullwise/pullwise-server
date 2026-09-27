# Pullwise Server

Pullwise Server is transitioning to the [GitHub project expense ledger](docs/design/github-project-ledger/README.md). It keeps account, GitHub authorization and Creem payment facts. The local Python runtime still exposes PR, CI and upstream Updates routes while the new ledger routes are implemented in stages.

The current implementation is **in progress**. Local GitHub App fact collection is assembled at startup when App credentials and a webhook secret are configured. PR discussion and review facts use the bounded GraphQL reader. CI job logs use an isolated, deadline-bound local HTTPS reader. Jev assessment, Cloudflare scheduled composition and production validation are still open; do not present this build as a complete production service.

The former full-repository scan, issue-fix, Reviewer, Agent-first, Worker fleet and Model Gateway APIs have been retired. Some old internal database helpers and tables remain pending physical cleanup. Existing account, authorization, payment and local database records are preserved.

## Local development

Requires Python 3.10.12. In this directory:

```bash
python3.10 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pullwise_server
```

Point `pullwise-web` at `VITE_API_BASE_URL=http://localhost:8080`. SQLite defaults to `.pullwise/pullwise.sqlite3`; set `PULLWISE_DB_PATH` to choose another path. `GET /health` reports local API readiness without exposing credentials.

Real login needs `PULLWISE_GITHUB_CLIENT_ID` and `PULLWISE_GITHUB_CLIENT_SECRET`. GitHub App authorization and automatic fact reads need `PULLWISE_GITHUB_APP_SLUG`, `PULLWISE_GITHUB_APP_ID`, `PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH` (or the supported private-key env form) and `PULLWISE_GITHUB_WEBHOOK_SECRET`. Keep secrets outside the repository. An unconfigured fact reader reports unavailable and does not attempt analysis.

Do not enable Jev processing until its credential, bounded request, admission and publication gates have been verified. Browser reads and service configuration do not start model processing.

## Design and current contract

- [GitHub project expense ledger design](docs/design/github-project-ledger/README.md) defines the requested replacement product.
- [Ledger OpenAPI](openapi/ledger-v1.yaml) is the target contract; [product-v1](openapi/product-v1.yaml) describes the transitional PR/CI/Updates runtime.
- `../pullwise-web` contains the browser client.

For offline verification, use the focused Python unittest suites and `cloudflare/server/sync_server_modules.py --check`. Real GitHub, Creem and Cloudflare integration and remote D1 migration remain gated until S18. The local checks do not establish production readiness.
