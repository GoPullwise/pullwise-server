# Pullwise Server

A lightweight Python API for `pullwise-web`.

By default the server does not return local mock login callbacks, local magic
links, or synthetic review findings. Configure real GitHub OAuth, SMTP email,
GitHub App, and review provider credentials for real scans. Explicit local mock
switches are available only for development.

## Run

```powershell
python -m pip install -e .
python -m pullwise_server
```

Point the frontend at:

```text
VITE_API_BASE_URL=http://localhost:3000
```

The server persists sessions, GitHub authorization state, selected
repositories, scans, issues, and settings in SQLite by default at
`.pullwise/pullwise.sqlite3`. Override with `PULLWISE_DB_PATH`.

## Production Deployment

Deploy this service on infrastructure that can run a normal Python process with
OS-level tools. Suitable targets include a VPS, a container platform, Render,
Railway, Fly.io, ECS, Cloud Run, or Cloudflare Containers when available. The
host needs:

- Python 3.10 or newer
- `git` on `PATH`
- outbound HTTPS access to GitHub, Stripe, Creem, SMTP, and the review provider
- persistent storage for `PULLWISE_DB_PATH` and `PULLWISE_CHECKOUT_ROOT`
- Codex CLI or Claude Code installed when `PULLWISE_REVIEW_PROVIDER` uses them

Install and run:

```bash
python -m pip install -e .
python -m pullwise_server --host 0.0.0.0 --port 3000
```

On platforms that inject `$PORT`, pass that value to `--port` or set
`PULLWISE_PORT`.

Minimum production environment:

```env
PULLWISE_HOST=0.0.0.0
PULLWISE_PORT=3000
PULLWISE_MODE=production
PULLWISE_APP_URL=https://app.your-domain.com
PULLWISE_ALLOWED_ORIGINS=https://app.your-domain.com
PULLWISE_API_BASE_URL=https://app.your-domain.com/api
PULLWISE_DB_PATH=/data/pullwise.sqlite3
PULLWISE_CHECKOUT_ROOT=/data/checkouts
PULLWISE_COOKIE_SECURE=true
```

Use `PULLWISE_API_BASE_URL=https://app.your-domain.com/api` when the web app is
deployed to Cloudflare Pages with the included `/api` proxy. OAuth callbacks and
magic links then return through the Pages domain, so browser session cookies are
set on the same origin used by the frontend.

If a trusted reverse proxy supplies `X-Forwarded-Proto`, `X-Forwarded-Host`, and
`X-Forwarded-Prefix`, you may omit `PULLWISE_API_BASE_URL` and set:

```env
PULLWISE_TRUST_PROXY_HEADERS=true
```

Only enable that flag behind a proxy you control.

Health check:

```text
GET /health
```

Expected shape:

```json
{"ok": true, "service": "pullwise-server"}
```

Run verification before deploying:

```bash
python -m unittest discover -s tests
```

## Real GitHub Setup

Create or configure a GitHub App for Pullwise:

- Homepage URL: `http://localhost:5174`
- Callback URL for user authorization: `http://localhost:3000/auth/github/callback`
- Setup URL: `http://localhost:3000/integrations/github/callback`
- Required permissions for current functionality: `Contents: read`, `Metadata: read`

For Cloudflare Pages production with the `/api` proxy, use:

- Homepage URL: `https://app.your-domain.com`
- Callback URL for user authorization: `https://app.your-domain.com/api/auth/github/callback`
- Setup URL: `https://app.your-domain.com/api/integrations/github/callback`

Set these variables in `F:\pullwise-server\.env`:

```env
PULLWISE_GITHUB_CLIENT_ID=your_oauth_or_github_app_client_id
PULLWISE_GITHUB_CLIENT_SECRET=your_oauth_or_github_app_client_secret
PULLWISE_GITHUB_APP_SLUG=your-github-app-slug
PULLWISE_GITHUB_APP_ID=123456
PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH=F:\path\to\pullwise.private-key.pem
```

`PULLWISE_GITHUB_CLIENT_ID` and `PULLWISE_GITHUB_CLIENT_SECRET` make
`/auth/github/authorize` use real GitHub sign-in. `PULLWISE_GITHUB_APP_SLUG`
makes `/integrations/github/authorize` use the GitHub App installation screen.
`PULLWISE_GITHUB_APP_ID` plus the private key let the server mint installation
tokens and list authorized repositories.

For deployment or secret stores, use `PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64`
instead of `PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH`.

## Email Magic Link Setup

Set these variables to enable real email sign-in:

```env
PULLWISE_EMAIL_PROVIDER=smtp
PULLWISE_EMAIL_FROM=Pullwise <login@your-domain.com>
PULLWISE_SMTP_HOST=smtp.your-provider.com
PULLWISE_SMTP_PORT=587
PULLWISE_SMTP_USERNAME=your_username
PULLWISE_SMTP_PASSWORD=your_password
PULLWISE_SMTP_STARTTLS=true
```

`POST /auth/email/magic-link` sends a short-lived login link and does not return
the token in the API response. The only exception is explicit local development
mode with `PULLWISE_ENABLE_DEV_MAGIC_LINKS=true`.

## Review Worker Setup

The scan worker defaults to disabled review provider mode so scans do not create
synthetic findings by accident. For a real agent run, install `git` plus either
Claude Code or Codex CLI, then set:

```env
PULLWISE_REVIEW_PROVIDER=claude_code
ANTHROPIC_API_KEY=your_key
PULLWISE_CHECKOUT_ROOT=F:\tmp\pullwise-checkouts
```

Supported values:

- `claude_code`: clone the selected repo and run Claude Code in the checkout
- `codex`: clone the selected repo and run Codex in the checkout
- `mock`: explicit local wire-up only; returns synthetic findings

`claude_code` and `codex` clone the selected repository during the `clone`
phase. The clone uses `github_auth.create_installation_access_token`, stores the
checkout path as `repoPath`, and passes that path to `review.run_review`.

The Codex provider uses official non-interactive `codex exec` mode with a
read-only sandbox, `--output-schema`, and `--output-last-message` so the worker
can parse structured findings.

## Billing Setup

Pullwise supports either Stripe or Creem. Configure one provider, or set
`PULLWISE_BILLING_PROVIDER=stripe|creem` if both providers are present.

Stripe:

```env
PULLWISE_STRIPE_SECRET_KEY=sk_live_or_test
PULLWISE_STRIPE_PRICE_ID=price_...
PULLWISE_STRIPE_WEBHOOK_SECRET=whsec_...
```

Creem:

```env
PULLWISE_CREEM_API_KEY=creem_key
PULLWISE_CREEM_PRODUCT_ID=prod_...
PULLWISE_CREEM_WEBHOOK_SECRET=whsec_...
PULLWISE_CREEM_TEST_MODE=false
```

Implemented billing routes:

- `GET /billing/plan`
- `POST /billing/checkout-sessions`
- `POST /billing/portal-sessions`
- `POST /webhooks/stripe`
- `POST /webhooks/creem`

Checkout URLs are created server-side. Webhooks verify Stripe
`Stripe-Signature` or Creem `creem-signature` before updating account billing
state.

## Cloudflare Worker Boundary

`pullwise-web` is a good fit for Cloudflare Pages. `pullwise-server` is not a
good fit for a normal Cloudflare Worker as currently implemented.

Cloudflare's own docs describe Workers as an edge runtime with CPU, memory,
startup, environment variable, and subrequest limits:
https://developers.cloudflare.com/workers/platform/limits/

Cloudflare Python Workers are beta and run through Pyodide:
https://developers.cloudflare.com/workers/languages/python/

Python Workers support pure Python and Pyodide-supported packages, but not a
full Linux/Python process environment:
https://developers.cloudflare.com/workers/languages/python/packages/

The Python Workers standard library page also lists modules that are unavailable
or non-functional in that WebAssembly VM:
https://developers.cloudflare.com/workers/languages/python/stdlib/

Pullwise scans currently require capabilities that belong on a server/container:

- `git clone` into a checkout directory
- subprocess execution for Git, Codex CLI, or Claude Code
- persistent SQLite state unless replaced by an external database
- persistent checkout storage
- long-running review work with provider CLIs and network access

To run this backend on Workers, it would need a redesign around Workers-native
storage and execution primitives, for example D1 or external Postgres for state,
R2 for artifacts, Queues or Workflows for background work, and a separate
containerized scanner for Git/Codex execution. The current deployable path is:

```text
Cloudflare Pages web app -> Pages Function /api proxy -> Python server/container
```

## Explicit Local Development Mocks

These switches are off by default:

```env
PULLWISE_ENABLE_LOCAL_GITHUB_MOCKS=true
PULLWISE_ENABLE_DEV_MAGIC_LINKS=true
PULLWISE_REVIEW_PROVIDER=mock
```

Use them only when testing frontend wiring without real GitHub or a real review
provider.

## Frontend Contract

Implemented endpoints:

- `GET /auth/session`
- `GET /auth/github/authorize?redirectTo=...`
- `GET /auth/github/callback?redirectTo=...`
- `POST /auth/sign-out`
- `GET /integrations/github/authorize?scope=all|selected&redirectTo=...`
- `GET /integrations/github/callback?scope=all|selected&redirectTo=...`
- `GET /integrations`
- `DELETE /integrations/github`
- `GET /repositories`
- `POST /repositories/sync`
- `GET /scans`
- `POST /scans`
- `GET /scans/{id}`
- `POST /scans/{id}/cancel`
- `GET /issues`
- `GET /issues/{id}`
- `PATCH /issues/{id}/status`
- `GET /settings`
- `PATCH /settings`
- `GET /billing/plan`
- `POST /billing/checkout-sessions`
- `POST /billing/portal-sessions`
- `POST /webhooks/stripe`
- `POST /webhooks/creem`

Explicitly not implemented:

- `POST /auth/email/magic-link` unless SMTP is configured or `PULLWISE_ENABLE_DEV_MAGIC_LINKS=true`
- `GET /dev/magic-links` unless `PULLWISE_ENABLE_DEV_MAGIC_LINKS=true`
- `POST /issues/{id}/fixes/apply`
- `POST /issues/{id}/pull-requests`
- Slack or Linear integration writes

Those endpoints return `501 Not Implemented` instead of fake success payloads.
