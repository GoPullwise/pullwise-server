# Pullwise Server

A lightweight Python API for `pullwise-web`.

By default the server does not return local mock login callbacks or synthetic
review findings. Configure real GitHub OAuth, GitHub App, and review provider
credentials for real scans. Explicit local mock switches are available only for
development.

Stage 1 production-trial scope:

- GitHub identity login
- GitHub App repository authorization
- Repository listing and sync
- Scan creation, queueing, recovery, cancellation, and history
- Rich issue findings and manual triage/status changes
- Stripe or Creem billing and live readiness status

Stage 2 automation is intentionally not implemented yet:

- Applying fixes
- Creating branches or pull requests
- Notifications
- Slack or Linear writes

If real GitHub OAuth secrets or GitHub App private keys were ever committed or
shared outside the local machine, rotate them in GitHub before production use.

## Run

Use a project-local virtual environment so this server does not share packages
with the system Python. The target runtime is Python 3.10.12.

Windows PowerShell:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m pullwise_server
```

Linux/macOS/server:

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m pullwise_server
```

Point the frontend at:

```text
VITE_API_BASE_URL=http://localhost:8080
```

The server persists sessions, GitHub authorization state, selected
repositories, scans, issues, and settings in SQLite by default at
`.pullwise/pullwise.sqlite3`. Override with `PULLWISE_DB_PATH`.

The server writes application and request logs to daily dated files by default
at `.pullwise/logs/pullwise-YYYY-MM-DD.log`. The day boundary is midnight in
the server's local time. Override with `PULLWISE_LOG_DIR`,
`PULLWISE_LOG_LEVEL`, and `PULLWISE_LOG_ROTATION_TIME=HH:MM`.

Current storage is intentionally lightweight: the app stores logical state as
JSON payloads in SQLite, and scan/issue listing endpoints read the in-process
state. This is suitable for small deployments and trials. For high-volume or
multi-tenant production use, move sessions, scans, issues, and billing events to
dedicated tables with pagination and retention policies before relying on this
backend as an unbounded system of record.

## Production Deployment

Deploy this service on infrastructure that can run a normal Python process with
OS-level tools. Suitable targets include a VPS, a container platform, Render,
Railway, Fly.io, ECS, Cloud Run, or Cloudflare Containers when available. The
host needs:

- Python 3.10.12
- `git` on `PATH`
- outbound HTTPS access to GitHub, Stripe, Creem, and the review provider
- persistent storage for `PULLWISE_DB_PATH` and `PULLWISE_CHECKOUT_ROOT`
- Codex CLI or Claude Code installed when `PULLWISE_REVIEW_PROVIDER` uses them

Install and run:

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m pullwise_server --host 0.0.0.0 --port 8080
```

On platforms that inject `$PORT`, pass that value to `--port` or set
`PULLWISE_PORT`.

Minimum production environment:

```env
PULLWISE_HOST=0.0.0.0
PULLWISE_PORT=8080
PULLWISE_MODE=production
PULLWISE_APP_URL=https://app.your-domain.com
PULLWISE_ALLOWED_ORIGINS=https://app.your-domain.com
PULLWISE_API_BASE_URL=https://app.your-domain.com/api
PULLWISE_DB_PATH=/data/pullwise.sqlite3
PULLWISE_LOG_DIR=/data/logs
PULLWISE_LOG_ROTATION_TIME=00:00
PULLWISE_RATE_LIMIT_ENABLED=true
PULLWISE_RATE_LIMIT_REQUESTS=600
PULLWISE_RATE_LIMIT_WINDOW_SECONDS=60
PULLWISE_MAX_CONCURRENT_SCANS=1
PULLWISE_MAX_CONCURRENT_SCANS_PER_USER=1
PULLWISE_CHECKOUT_ROOT=/data/checkouts
PULLWISE_COOKIE_SECURE=true
```

Use `PULLWISE_API_BASE_URL=https://app.your-domain.com/api` when the web app is
deployed to Cloudflare Pages with the included `/api` proxy. OAuth callbacks
then return through the Pages domain, so browser session cookies are set on the
same origin used by the frontend.

Keep `PULLWISE_ALLOWED_ORIGINS` to exact trusted origins. Wildcard `*` is
ignored because the API uses credentialed browser requests.

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
{
  "ok": true,
  "service": "pullwise-server",
  "reviewProvider": "codex",
  "github": {
    "oauthConfigured": true,
    "appInstallConfigured": true,
    "appApiConfigured": true,
    "appVisibilityCheck": true
  },
  "billing": {"provider": "stripe", "enabled": true},
  "limits": {
    "maxConcurrentScans": 1,
    "maxConcurrentScansPerUser": 1,
    "rateLimitEnabled": true
  }
}
```

The health payload intentionally exposes readiness booleans and provider names,
not secrets, access tokens, private key contents, or private key paths.

Run verification before deploying:

```bash
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests
```

Run `python -m pip install -e .` before the test suite so imports resolve
against the local package. On Windows, launcher contract tests require a POSIX
shell that provides `cygpath`; when the available shell cannot convert Windows
paths, those launcher tests skip with an explicit reason.

## API Authentication and Limits

Browser requests use the `pw_session` HTTP-only session cookie. The same opaque
session id is also accepted as `Authorization: Bearer <session-id>` for REST API
clients or proxies that prefer bearer forwarding. Existing frontend requests do
not need to send this header because `pullwise-web` uses credentialed requests.

Endpoints that read or mutate account data require a valid session, whether it
arrives by cookie or bearer header. Public OAuth callback routes, billing
webhooks, and `/health` stay callable without a user session, with webhook
signature verification enforced by the billing provider integration.

When `PULLWISE_RATE_LIMIT_ENABLED=true`, each request is counted in SQLite in
the `api_rate_limits` table. The limiter keys by signed-in user id when a valid
session is present, otherwise by client IP address. Defaults are `600` requests
per `60` seconds. In production mode the limiter is enabled by default unless
explicitly disabled; local mode leaves it off unless configured.

### Ubuntu 22.04 launcher

For a single Ubuntu 22.04 host, manage the server through `launcher.sh` from
the repository root:

```bash
sudo apt-get update
sudo apt-get install -y python3.10 python3.10-venv git curl
id -u pullwise >/dev/null 2>&1 || sudo useradd --system --create-home --shell /usr/sbin/nologin pullwise
sudo install -d -o root -g pullwise -m 0750 /etc/pullwise /etc/pullwise/secrets
sudo install -d -o pullwise -g pullwise -m 0750 /var/lib/pullwise /var/log/pullwise
cp .env.example .env.local
$EDITOR .env.local
chmod +x launcher.sh
./launcher.sh setup
sudo ./launcher.sh sync-env
sudo ./launcher.sh install-service
sudo ./launcher.sh doctor
sudo ./launcher.sh start
./launcher.sh status
```

Keep the repository in a service-readable path, for example
`/opt/pullwise-server`, so the `pullwise` system user can enter the working
directory and execute `.venv/bin/python`.

The launcher treats `.env.local` as the editable project-local source of truth
and copies it to `/etc/pullwise/server.env`. The systemd unit reads that file
with `EnvironmentFile=/etc/pullwise/server.env` and starts:

```text
python -m pullwise_server
```

Set `PULLWISE_MODE=production` before running `doctor`. A typical single-host
production storage layout is:

```env
PULLWISE_DB_PATH=/var/lib/pullwise/pullwise.sqlite3
PULLWISE_LOG_DIR=/var/log/pullwise
PULLWISE_CHECKOUT_ROOT=/var/lib/pullwise/checkouts
PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH=/etc/pullwise/secrets/github-app-private-key.pem
```

Keep the GitHub App private key outside the repository. The recommended path is
`/etc/pullwise/secrets/github-app-private-key.pem` with directory permissions
`root:pullwise 0750` and file permissions `root:pullwise 0640`, while the
service runs as `User=pullwise` and `Group=pullwise`.

The production audit expects exact HTTPS origins, secure cookies, writable
persistent paths for the SQLite database, logs, and checkouts, real GitHub
OAuth/App credentials, and a real review provider (`codex` or `claude_code`)
with its CLI installed for the same OS user that runs Pullwise.

Common operations:

```bash
sudo ./launcher.sh stop
sudo ./launcher.sh restart
./launcher.sh health
sudo ./launcher.sh logs journal
sudo ./launcher.sh logs app
sudo ./launcher.sh logs error
sudo ./launcher.sh config
sudo ./launcher.sh audit
```

`launcher.sh start`, `stop`, `restart`, and `status` use systemd automatically
after `install-service` has written the unit file. Set `PULLWISE_MANAGER=direct`
only when you deliberately want the older PID-file background mode.

For server migration or backup, export the runtime state:

```bash
sudo ./launcher.sh export /tmp/pullwise-server-$(date +%Y%m%d).tar.gz
```

The archive includes `server.env`, the SQLite database plus WAL/SHM sidecars
when present, logs, checkouts, the configured GitHub App private key PEM, and
other `.pullwise` generated state. On a new host, copy the archive into the
repository directory and import it:

```bash
sudo ./launcher.sh import /tmp/pullwise-server-20260523.tar.gz
sudo ./launcher.sh doctor
sudo ./launcher.sh start
```

Import restores `/etc/pullwise/server.env`, places artifacts at the paths named
inside that env file, restores the PEM file when `PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH`
is set, and renders the systemd service file.

## Real GitHub Setup

Create or configure a GitHub App for Pullwise:

- Homepage URL: `http://localhost:5174`
- Callback URL for user authorization: `http://localhost:8080/auth/github/callback`
- Setup URL: `http://localhost:8080/integrations/github/callback`
- Required permissions for current functionality: `Contents: write`, `Pull requests: write`, `Metadata: read`
- App visibility: `Public` / `Any account`

The repository connection flow must install Pullwise on the signed-in user's
personal account or one of their organizations. GitHub only allows that when
the GitHub App is public. If the app is private, GitHub limits installation to
the app owner account, so the repository picker will only show owner-account
repositories such as `GoPullwise/*`. Private repositories are supported after a
public app is installed on the user's account or organization and granted
`Contents: write` and `Pull requests: write` access so Pullwise can push fix
branches and open pull requests.

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

By default, `/integrations/github/authorize` checks the configured app slug with
GitHub's public `GET /apps/{app_slug}` endpoint before opening the installation
flow. A `404` there means the app slug is wrong or the app is private/owner-only,
and the server refuses to continue instead of sending the user to a GoPullwise
owner-only repository picker. If the check cannot be completed because GitHub's
API is unavailable or rate-limited, the server also fails closed rather than
opening an installation page that may only expose owner-account repositories.
Keep this visibility check enabled for real user repository installs.
If you set `PULLWISE_GITHUB_APP_INSTALL_URL`, you must still set
`PULLWISE_GITHUB_APP_SLUG`; the custom URL does not bypass the public-app check.

For deployment or secret stores, use `PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64`
instead of `PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH`.

## Review Worker Setup

The scan worker defaults to disabled review provider mode so scans do not create
synthetic findings by accident. For a real agent run, install `git` plus either
Claude Code or Codex CLI, then log in with that CLI as the same OS user/session
that runs Pullwise.

Claude Code:

```env
PULLWISE_REVIEW_PROVIDER=claude_code
PULLWISE_CHECKOUT_ROOT=F:\tmp\pullwise-checkouts
```

Codex:

```env
PULLWISE_REVIEW_PROVIDER=codex
PULLWISE_CHECKOUT_ROOT=F:\tmp\pullwise-checkouts
```

Supported values:

- `claude_code`: clone the selected repo and run Claude Code in the checkout
- `codex`: clone the selected repo and run Codex in the checkout
- `mock`: explicit local wire-up only; returns synthetic findings

`claude_code` and `codex` clone the selected repository during the `clone`
phase. The clone uses `github_auth.create_installation_access_token`, stores the
checkout path as `repoPath`, and passes that path to `review.run_review`.
Checkouts are namespaced by user and scan under
`PULLWISE_CHECKOUT_ROOT/<user-id>/<scan-id>/...`, so one user's working tree is
not reused for another user's scan.

The Codex provider uses official non-interactive `codex exec` mode with a
read-only sandbox, `model_reasoning_effort="xhigh"`, `--output-schema`, and
`--output-last-message` so the worker can parse structured findings. Codex
continues to own login state and model selection through the CLI account/session
configuration used by the service account.

On a single machine, keep scan concurrency conservative because Git plus Codex
or Claude CLI can consume significant CPU and RAM. By default the worker runs
only one real scan at a time globally and one per user:

```env
PULLWISE_MAX_CONCURRENT_SCANS=1
PULLWISE_MAX_CONCURRENT_SCANS_PER_USER=1
```

These values limit running scans, not scan creation. If 500 users submit work
while the global limit is 3, three scans run and the rest remain `queued`.
Queued scan payloads include `queue.position`, `queue.ahead`, `queue.reason`,
`queue.message`, and the active limits so the frontend can explain why a scan is
waiting and when it moves to running.

Raise those values only after sizing CPU, RAM, checkout storage, and provider
rate limits.

Scan/review flow tracing is enabled by default through the server logger named
`pullwise_server.scan`. Events are emitted as single-line JSON payloads for
queue claim, phase start/completion, checkout readiness, provider dispatch,
finding counts, failures, cancellation, cleanup, and completion. They go to the
same console and daily log files configured by `PULLWISE_LOG_DIR`.

Disable scan trace logs without changing code:

```env
PULLWISE_SCAN_LOGS_ENABLED=false
```

The disable switch also accepts `0`, `no`, `off`, or `disabled`.

## Billing Setup

Pullwise supports either Stripe or Creem. Configure one provider, or set
`PULLWISE_BILLING_PROVIDER=stripe|creem` if both providers are present.
The built-in catalog is Free plus Pro. Free defaults to 5 reviews/month.
Pro defaults to $29/month or $290/year with 100 reviews/month. Monthly review
allowance resets each calendar month and does not roll over.

Stripe:

```env
PULLWISE_STRIPE_SECRET_KEY=sk_live_or_test
PULLWISE_STRIPE_PRO_MONTHLY_PRICE_ID=price_...
PULLWISE_STRIPE_PRO_YEARLY_PRICE_ID=price_...
PULLWISE_STRIPE_WEBHOOK_SECRET=whsec_...
```

Creem:

```env
PULLWISE_CREEM_API_KEY=creem_key
PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID=prod_...
PULLWISE_CREEM_PRO_YEARLY_PRODUCT_ID=prod_...
PULLWISE_CREEM_WEBHOOK_SECRET=whsec_...
PULLWISE_CREEM_TEST_MODE=false
```

Implemented billing routes:

- `GET /billing/plan`
- `POST /billing/checkout-sessions`
- `POST /billing/portal-sessions`
- `POST /billing/change-interval`
- `POST /webhooks/stripe`
- `POST /webhooks/creem`

Checkout URLs are created server-side. Webhooks verify Stripe
`Stripe-Signature` or Creem `creem-signature` before updating account billing
state. Stripe monthly-to-yearly changes open a Billing Portal confirmation flow;
Creem monthly-to-yearly changes use the subscription upgrade endpoint.

## User and Billing State

Runtime state is persisted in SQLite. The current lightweight deployment stores
logical records in `app_state` JSON payloads plus `api_rate_limits` rows for the
database-backed limiter. User records contain:

- Basic account: `id`, `name`, `email`, `avatarUrl`, `createdAt`, `providers`
- GitHub identity: `githubId`, `githubLogin`, `githubHtmlUrl`,
  `githubAccessToken`, `githubOAuthScope`, `githubAccessTokenUpdatedAt`
- Repository access: GitHub App installation ids/accounts, repository
  selection, authorized repository names/items, permission summary, pending
  authorization state, and sync status
- Billing: provider, customer id, subscription id/item id, plan, interval,
  status, period start/end, cancel flags, last processed event metadata, and
  checkout/session metadata
- Usage: monthly review usage period, plan, used count, configured limit, and
  remaining entitlement

The frontend-facing account surface is `GET /auth/session` for login/GitHub
state and `GET /billing/plan` for plan catalog plus current billing account
status. Webhooks update billing state idempotently by event id and can queue
subscription updates until the checkout/customer mapping exists.

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
- `POST /billing/change-interval`
- `POST /webhooks/stripe`
- `POST /webhooks/creem`

Explicitly not implemented:

- `POST /issues/{id}/fixes/apply`
- `POST /issues/{id}/pull-requests`
- Notification delivery
- Slack or Linear integration writes

Those endpoints return `501 Not Implemented` instead of fake success payloads.
