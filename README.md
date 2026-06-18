# Pullwise Server

A lightweight Python API for `pullwise-web`.

By default the server does not return local mock login callbacks. Configure real
GitHub OAuth, GitHub App credentials, and at least one external worker for real
scans. Explicit local mock switches are available only for development.

Current production-trial scope:

- GitHub identity login
- GitHub App repository authorization and multi-account installation management
- Repository listing and sync
- Scan creation, queueing, recovery, cancellation, and history
- Distributed worker system: worker registry, heartbeat, atomic job claiming,
  progress reporting, result upload, timeout recovery, and retry logic
- Admin worker management: create, enable, disable, delete, rotate token,
  and view audit events
- Workspace creation and API key management for external automation
- Rich issue findings and manual triage/status changes
- Deterministic fix preview for auto-fixable findings
- GitHub pull request creation for deterministic issue fixes
- Resource-scoped quota system with workspace/repository-level limit tracking
- Creem billing, pricing metadata, API docs metadata, and live
  readiness status

Stage 2 remediation is intentionally narrow:

- Pullwise can preview deterministic fix diffs and open GitHub pull requests
  through the GitHub App for auto-fixable findings.
- The GitHub App installation must grant `Contents: write`, `Pull requests:
  write`, and `Metadata: read`.
- Direct fix application (`POST /issues/{id}/fixes/apply`) explicitly returns
  `501 Not Implemented` to prevent accidental writes.

Still not implemented:

- Direct in-place fix application
- Batch fixes
- Auto-merge
- Notifications
- Slack or Linear writes
- AI-generated replacement patches beyond the finding payload

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

Current storage uses SQLite with structured tables for core entities
(`app_state`, `repositories`, `quota_buckets`, `quota_ledger`,
`repo_fingerprints`, `api_keys`, `worker_tokens`, `workers`,
`worker_audit_events`, `scan_jobs`, `job_results`) alongside in-process
state for sessions, scans, and issue listings. This is suitable for small
deployments and trials. For high-volume or multi-tenant production use, add
pagination, retention policies, and connection pooling before relying on this
backend as an unbounded system of record.

## Production Deployment

Deploy this service on infrastructure that can run a normal Python process with
OS-level tools. Suitable targets include a VPS, a container platform, Render,
Railway, Fly.io, ECS, Cloud Run, or Cloudflare Containers when available. The
host needs:

- Python 3.10.12
- `git` on `PATH`
- outbound HTTPS access to GitHub, Creem, and worker hosts
- persistent storage for `PULLWISE_DB_PATH` and `PULLWISE_CHECKOUT_ROOT`

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
PULLWISE_ALLOWED_ORIGINS=https://app.your-domain.com,https://admin.your-domain.com
PULLWISE_API_BASE_URL=https://app.your-domain.com/api
PULLWISE_WORKER_SERVER_URL=https://api.your-domain.com
PULLWISE_DB_PATH=/data/pullwise.sqlite3
PULLWISE_LOG_DIR=/data/logs
PULLWISE_LOG_ROTATION_TIME=00:00
PULLWISE_SERVER_CLEANUP_INTERVAL_SECONDS=3600
PULLWISE_SCAN_JOB_RETENTION_SECONDS=2592000
PULLWISE_WORKER_COMMAND_RETENTION_SECONDS=2592000
PULLWISE_WORKER_AUDIT_RETENTION_SECONDS=7776000
PULLWISE_CHECKOUT_ROOT=/data/checkouts
PULLWISE_STATE_ENCRYPTION_KEY_PATH=/etc/pullwise/secrets/state-encryption-key
PULLWISE_COOKIE_SECURE=true
PULLWISE_COOKIE_SAME_SITE=Lax
PULLWISE_ADMIN_USER_IDS=
PULLWISE_ADMIN_EMAILS=admin@example.com
PULLWISE_WORKER_JOB_TIMEOUT_SECONDS=1800
```

Plan quotas, scan queue/retry/lease limits, repository scan limits, rate
limits, worker control-plane defaults, review calibration settings, and the
non-secret Creem catalog live in the server database. Edit them through the
admin app Settings page or `PATCH /admin/system-config`; the server seeds
hardcoded defaults when the DB has no value.

Server cleanup only prunes operational records: expired sessions/GitHub OAuth state, terminal worker commands/audit rows, and terminal scan job/result duplicates that have already been applied to the user-visible scan state. User scan results in `SCANS`/`ISSUES` are retained.

Production deployments must provide a separate state encryption key file. The
server uses it to encrypt GitHub OAuth user tokens before writing `app_state`
JSON to SQLite. Keep this file outside the project tree and outside migration
packages:

```bash
sudo install -d -m 750 -o root -g pullwise /etc/pullwise/secrets
openssl rand -base64 32 | sudo tee /etc/pullwise/secrets/state-encryption-key >/dev/null
sudo chown root:pullwise /etc/pullwise/secrets/state-encryption-key
sudo chmod 440 /etc/pullwise/secrets/state-encryption-key
```

`./launcher.sh export` intentionally does not package this key. When restoring
or moving an encrypted database, provision the same key separately before
starting the server.

Use `PULLWISE_API_BASE_URL=https://app.your-domain.com/api` when the web app is
deployed to Cloudflare Pages with the included `/api` proxy. OAuth callbacks
then return through the Pages domain, so browser session cookies are set on the
same origin used by the frontend.

Use `PULLWISE_WORKER_SERVER_URL` for distributed worker install commands. It
should point directly at the backend control plane instead of the browser/OAuth
Pages proxy when those are different origins.

Keep `PULLWISE_ALLOWED_ORIGINS` to exact trusted origins. Wildcard `*` is
ignored because the API uses credentialed browser requests.

If deploying the separate `pullwise-admin` frontend, include its exact origin in
`PULLWISE_ALLOWED_ORIGINS`. GitHub OAuth secrets and admin authorization still
remain server-side through `PULLWISE_GITHUB_*`, `PULLWISE_ADMIN_EMAILS`, and
`PULLWISE_ADMIN_USER_IDS`.

If `pullwise-admin` is deployed on a different site such as
`https://pullwise-admin.danuberiverferryman.workers.dev` and calls
`https://api.pull-wise.com` directly, set:

```env
PULLWISE_ALLOWED_ORIGINS=https://pull-wise.com,https://pullwise-admin.danuberiverferryman.workers.dev
PULLWISE_COOKIE_SAME_SITE=None
PULLWISE_COOKIE_SECURE=true
```

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
  "reviewProvider": "worker",
  "github": {
    "oauthConfigured": true,
    "appInstallConfigured": true,
    "appApiConfigured": true,
    "appVisibilityCheck": true
  },
  "billing": {"provider": "creem", "enabled": true},
  "limits": {
    "maxQueuedScansGlobal": 1000,
    "rateLimitEnabled": true
  }
}
```

The health payload intentionally exposes readiness booleans and provider names,
not secrets, access tokens, private key contents, or private key paths.

### Distributed Worker System

The server maintains a global FIFO job queue for scans. Workers pull jobs,
execute them, and report progress and results. Work is dispatched atomically
so multiple workers never receive the same job.

Public status is available at `GET /status/system`. It returns scan-system
summary fields plus a sanitized `workers` list for the web status page. Public
worker entries expose fixed capacity and heartbeat status only; hostnames, internal
errors, worker tokens, token hashes, and audit events remain admin-only.

Administrators manage workers at `/admin/*` endpoints:

These admin APIs are the registry control plane. They manage desired state and
credentials, including create, enable, disable, metadata update, token rotation,
diagnostics, detail, audit, and worker deletion. They should not directly execute
host commands from the server. Delete/uninstall operations use a pull-based
command model where the admin creates a command, the worker receives it during
heartbeat or command polling, executes it locally, and reports the result. See
`docs/worker-management-control-plane.md`.

- `GET /admin/workers` — list all workers
- `GET /admin/workers/{id}` — worker detail with audit events
- `POST /admin/workers` — create a new worker (returns one-time token)
- `POST /admin/workers/{id}/enable` — enable a disabled worker
- `POST /admin/workers/{id}/disable` — disable a worker
- `PATCH /admin/workers/{id}` — update worker metadata
- `DELETE /admin/workers/{id}` — queue worker uninstall and remove it from admin lists
- `GET /admin/status` — scan system status (admin view)

Worker bootstrap: `GET /install-worker.sh` returns a shell script that installs
the worker package as a systemd service. The script accepts `--server`,
`--worker-id`, and `--worker-token-file` arguments, or reads the one-time token
from `PULLWISE_WORKER_TOKEN`. The target host must have Python 3.9 or newer.
By default, the installer downloads the
`pullwise-worker` wheel from the `GoPullwise/pullwise-worker` GitHub Release
matching the worker `version` provided at admin creation time. The default
worker version, default package, and release lookup URL are database-backed
system config. Override the full package URL with `PULLWISE_WORKER_PACKAGE` or
`--package` during controlled upgrades. Re-running the installer force-reinstalls
the selected worker wheel so same-version rebuilds are not skipped by pip. The Codex CLI bootstrap package is
pinned by default as `@openai/codex@0.135.0`; override it with
`PULLWISE_CODEX_PACKAGE` or `--codex-package` when rolling out a new CLI.

Worker endpoints (authenticated via bearer token):

- `POST /worker/heartbeat` — report running jobs, health, and fixed single-job capacity
- `POST /worker/jobs/claim` — atomically claim queued jobs
- `POST /worker/jobs/{id}/progress` — report scan phase and progress
- `POST /worker/jobs/{id}/result` — upload completed scan results

Jobs that timeout (no heartbeat or progress) are automatically re-queued up
to the database-backed scan job max-attempts setting, then marked failed.

### Billing Provider Configuration

Creem:

```env
PULLWISE_CREEM_API_KEY=...
PULLWISE_CREEM_WEBHOOK_SECRET=...
```

Creem is the only supported billing provider. Billing is enabled when the
Creem API key, webhook secret, and database-backed Creem product catalog are
configured. Creem product IDs, test mode, API base URL, upgrade behavior, and
billing timeout are edited through admin system config, not environment
variables. If Creem is not configured, billing is disabled automatically.

### GitHub App Configuration

Required for repository authorization, scan access, and pull request creation:

```env
PULLWISE_GITHUB_APP_ID=123456
PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH=/data/github-app.pem
# or as base64:
PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64=LS0t...
PULLWISE_GITHUB_CLIENT_ID=...
PULLWISE_GITHUB_CLIENT_SECRET=...
PULLWISE_GITHUB_WEBHOOK_SECRET=...
```

The GitHub App must be installed on target accounts and grant:
`Contents: write`, `Pull requests: write`, `Metadata: read`.

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

Production web app URL: `https://pull-wise.com`.

Production public API base URL: `https://api.pull-wise.com`.

Browser requests use the `pw_session` HTTP-only session cookie. The same opaque
session id is also accepted as `Authorization: Bearer <session-id>` for REST API
clients or proxies that prefer bearer forwarding. Existing frontend requests do
not need to send this header because `pullwise-web` uses credentialed requests.

Endpoints that read or mutate account data require a valid session, whether it
arrives by cookie or bearer header. Public OAuth callback routes, billing
webhooks, and `/health` stay callable without a user session, with webhook
signature verification enforced by the billing provider integration.

Account-scoped automation uses API keys created from the signed-in dashboard:

- `GET /api-keys` lists keys for the current account.
- `POST /api-keys` creates a key and returns the `pwk_...` token once.
- `DELETE /api-keys/{id}` revokes a key.

External API requests can pass the key as `Authorization: Bearer <api-key>` or
`X-Pullwise-Api-Key: <api-key>`. API keys are tied to one user account, store
only a hash server-side, inherit that user's authorized repositories, and grant
scopes such as `repositories:read`, `scans:write`, `scans:read`, and
`quota:read`.

API-key endpoints:

- `GET /api/v1/repositories`
- `POST /api/v1/repositories/{repoId}/scans`
- `POST /api/v1/repositories/{repoId}/scans/stop`
- `GET /api/v1/repositories/{repoId}/scans/current`
- `GET /api/v1/repositories/{repoId}/quota`

Example:

```bash
curl https://api.pull-wise.com/api/v1/repositories \
  -H "Authorization: Bearer $PULLWISE_API_KEY"
```

`POST /api/v1/repositories/{repoId}/scans` accepts optional JSON fields
`branch`, `commit`, and `requestId`. `requestId` is an idempotency key: reuse
for the same repository returns the existing scan; reuse for a different
repository returns `409 Conflict`.

The machine-readable API description is available at `GET /api-docs` and
`GET /api/docs`.

When the database-backed rate limit setting is enabled, each request is counted
in SQLite in the `api_rate_limits` table. The limiter keys by signed-in user id
when a valid session is present, otherwise by client IP address. Defaults are
`600` requests per `60` seconds, with blocking disabled until enabled in admin
system config.

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
PULLWISE_STATE_ENCRYPTION_KEY_PATH=/etc/pullwise/secrets/state-encryption-key
```

Keep the GitHub App private key outside the repository. The recommended path is
`/etc/pullwise/secrets/github-app-private-key.pem` with directory permissions
`root:pullwise 0750` and file permissions `root:pullwise 0640`, while the
service runs as `User=pullwise` and `Group=pullwise`.

Keep the state encryption key in the same secrets directory, but do not store it
in the repository or migration archive. The recommended permissions are
`root:pullwise 0440`, or `root:root 0400` if the service can read it through
your secret manager or mount configuration.

The production audit expects exact HTTPS origins, secure cookies, writable
persistent paths for the SQLite database, logs, and checkouts, real GitHub
OAuth/App credentials, and at least one registered external worker for scan
processing.

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

Optional Git auto-deploy polling is available through `git-watch.sh` from the
repository root. It periodically fetches the configured upstream and, when a new
fast-forward commit is available, pulls it, runs the README verification steps,
restarts through `launcher.sh`, and checks health:

```bash
chmod +x git-watch.sh
./git-watch.sh --once
PULLWISE_WATCH_INTERVAL_SECONDS=60 ./git-watch.sh
```

By default the watcher runs `./launcher.sh setup`,
`.venv/bin/python -m unittest discover -s tests`, `./launcher.sh restart`, and
`./launcher.sh health`. Enable production-only steps when the process has the
right permissions:

```bash
PULLWISE_WATCH_RUN_SYNC_ENV=true PULLWISE_WATCH_RUN_DOCTOR=true ./git-watch.sh
```

The watcher refuses to update a dirty working tree unless
`PULLWISE_WATCH_ALLOW_DIRTY=true` is set, uses `git pull --ff-only`, and writes
logs to `.pullwise/git-watch.log`. It records the last successfully deployed
commit in `.pullwise/git-watch.deployed-head`; if setup, tests, restart, or
health checks fail after a pull, the next polling cycle retries deployment even
when Git is already at the latest upstream commit.

For server migration or backup, export the runtime state:

```bash
sudo ./launcher.sh export /tmp/pullwise-server-$(date +%Y%m%d).tar.gz
```

The archive includes `server.env`, the SQLite database plus WAL/SHM sidecars
when present, logs, checkouts, the configured GitHub App private key PEM, and
other `.pullwise` generated state. It intentionally excludes the state
encryption key, so provision the same
`PULLWISE_STATE_ENCRYPTION_KEY_PATH` file separately before starting an
imported encrypted database. On a new host, copy the archive into the repository
directory and import it:

```bash
sudo ./launcher.sh import /tmp/pullwise-server-20260523.tar.gz
sudo ./launcher.sh doctor
sudo ./launcher.sh start
```

Import restores `/etc/pullwise/server.env`, places artifacts at the paths named
inside that env file, restores the PEM file when
`PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH` is set, and renders the systemd service
file. It does not restore `PULLWISE_STATE_ENCRYPTION_KEY_PATH`.

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

The server does not execute review jobs locally. It creates queued scan jobs and
external `pullwise-worker` hosts claim, run, and upload results through the
worker API.

Standalone pullwise-worker hosts keep only process-level executable settings.
Subscription plan policy is not read from worker or server environment
variables; it is stored in the server database and attached to each claimed job.

In distributed worker mode, total running scan capacity comes from connected
workers. Each worker processes exactly one job at a time and does not keep a
local job queue; server-side scan jobs remain queued until a worker finishes its
current job and claims the next one. The server enforces the global queue limit
and existing permission/quota checks, but it does not reject scans because the
same user already has queued or running jobs. Edit global queue limits through
admin system config; they are not read from worker hosts and they are not read
from server environment variables after startup.

If all online workers are busy, new scans remain `queued` until a worker
finishes and claims more work.
Queued scan payloads include `queue.position`, `queue.ahead`, `queue.reason`,
`queue.message`, and global queue context so the frontend can explain why a scan
is waiting and when it moves to running.

Workers poll with bounded exponential backoff when the queue is empty or the
server is temporarily unreachable. Tune `PULLWISE_WORKER_POLL_SECONDS`,
`PULLWISE_WORKER_POLL_JITTER_SECONDS`, and
`PULLWISE_WORKER_MAX_BACKOFF_SECONDS` to avoid thundering-herd polling under
larger fleets.

Scan/review flow tracing is enabled by default through the server logger named
`pullwise_server.scan`. Events are emitted as single-line JSON payloads for
queue claim, worker phase updates, worker result upload, finding counts,
failures, cancellation, cleanup, and completion. They go to the same console
and daily log files configured by `PULLWISE_LOG_DIR`.

Disable scan trace logs without changing code:

```env
PULLWISE_SCAN_LOGS_ENABLED=false
```

The disable switch also accepts `0`, `no`, `off`, or `disabled`.

## Billing Setup

Pullwise uses Creem for billing.
The built-in catalog is Free, Pro, and Max. Scan quota is tracked for the
signed-in user and for each stable GitHub repository id. Free defaults to 5
user scans/month and 5 scans/month for each repository. GitHub forks that
report the same source repository share the source repository quota bucket.
Pro and Max pricing, currency, and billing intervals are read from the
configured Creem products. Pro defaults to 60 user scans/month and 60
scans/month for each repository. Max defaults to 90 user scans/month and 90
scans/month for each repository.
Monthly review allowance resets on the user's subscription-cycle
anniversary, or on the free-cycle anniversary when the account is not entitled
to a paid plan.

Quota controls are database-backed system config. Edit Free, Pro, and Max user
and repository monthly scan limits through the admin Settings page.

Subscription plan agent CLI/model/reasoning policy is stored in the server
database. The server seeds Free, Pro, and Max defaults into `app_state` on first
read, admins edit them through `/admin/subscription-plans/agent-configs`, and
each claimed scan job carries the resolved `agentConfig` to the worker.

Creem:

```env
PULLWISE_CREEM_API_KEY=creem_key
PULLWISE_CREEM_WEBHOOK_SECRET=whsec_...
```

Creem product IDs are database-backed system config. Configure each monthly and
yearly Creem product ID under the matching Pullwise plan; Creem checkout and
subscription upgrade calls use `product_id`. Test mode, API base URL, upgrade
behavior, and billing timeout are also admin system config. API keys and
webhook signing secrets remain deployment secrets.

Implemented billing routes:

- `GET /billing/plan`
- `GET /billing`
- `POST /billing/checkout-sessions`
- `POST /billing/change-interval`
- `POST /billing/cancel-subscription`
- `POST /billing/resume-subscription`
- `POST /webhooks/creem`

Checkout URLs are created server-side with `userId`
metadata. Webhooks verify Creem `creem-signature` before updating billing
state. Plan and interval upgrades use the Creem subscription upgrade endpoint.
Pullwise supports higher-tier changes and monthly-to-yearly changes, with
the database-backed Creem upgrade behavior defaulting to immediate proration. Lower-tier
changes and yearly-to-monthly changes are not offered by Pullwise. Cancellation
is scheduled for the end of the paid period and can be resumed before that
period ends; Pullwise does not issue automatic refunds for the remaining period.

## User and Billing State

Runtime state is persisted in SQLite. The current lightweight deployment stores
application records in `app_state` JSON payloads, `api_rate_limits` rows for
the database-backed limiter, and normalized repository/quota tables.
User records contain:

- Basic account: `id`, `name`, `email`, `avatarUrl`, `createdAt`, `providers`
- GitHub identity: `githubId`, `githubLogin`, `githubHtmlUrl`,
  `githubAccessToken`, `githubOAuthScope`, `githubAccessTokenUpdatedAt`
- Repository access: GitHub App installation ids/accounts, repository
  selection, authorized repository names/items, permission summary, pending
  authorization state, and sync status
- Billing state: provider, customer id, subscription id/item id, plan,
  interval, status, period start/end, cancel flags, last processed event
  metadata, and checkout/session metadata
- Scan quota is deducted from user and repository quota buckets

Normalized tables include `repositories`, `quota_buckets`, `quota_ledger`,
`api_keys`, `worker_tokens`, `workers`, `scan_jobs`, `job_results`, and
`repo_fingerprints`. New scans store `repoId`, `githubRepoId`, user usage,
repository usage, quota bucket ids, and checkout fingerprint risk decisions.
Forks share repository quota through their GitHub source repo id, and content
fingerprints are recorded after checkout for clone/reuse review.

The frontend-facing surfaces are `GET /auth/session` for login and GitHub state,
and `GET /billing/plan` for plan catalog plus current billing status. Webhooks
update billing state idempotently by event id and can queue subscription updates
until the checkout/customer mapping exists.

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
```

Use local GitHub mocks only when testing frontend wiring without real GitHub.

## Frontend Contract

Implemented endpoints:

- `GET /auth/session`
- `GET /pricing`
- `GET /api-docs`
- `GET /dashboard/overview`
- `GET /auth/github/authorize?redirectTo=...`
- `GET /auth/github/callback?redirectTo=...`
- `POST /auth/sign-out`
- `GET /workspaces`
- `POST /workspaces`
- `GET /api-keys`
- `POST /api-keys`
- `DELETE /api-keys/{id}`
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
- `POST /issues/{id}/fixes/preview`
- `POST /issues/{id}/pull-requests`
- `GET /settings`
- `PATCH /settings`
- `GET /billing/plan`
- `POST /billing/checkout-sessions`
- `POST /billing/change-interval`
- `POST /billing/cancel-subscription`
- `POST /billing/resume-subscription`
- `POST /webhooks/creem`
- `GET /api/v1/repositories`
- `POST /api/v1/repositories/{repoId}/scans`
- `POST /api/v1/repositories/{repoId}/scans/stop`
- `GET /api/v1/repositories/{repoId}/scans/current`
- `GET /api/v1/repositories/{repoId}/quota`

Explicitly not implemented:

- `POST /issues/{id}/fixes/apply`
- Batch fixes and auto-merge
- Notification delivery
- Slack or Linear integration writes
- AI-generated replacement patches beyond the finding payload

The direct apply endpoint returns `501 Not Implemented` instead of a fake
success payload.
