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

## Real GitHub Setup

Create or configure a GitHub App for Pullwise:

- Homepage URL: `http://localhost:5174`
- Callback URL for user authorization: `http://localhost:3000/auth/github/callback`
- Setup URL: `http://localhost:3000/integrations/github/callback`
- Required permissions for current functionality: `Contents: read`, `Metadata: read`

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

Explicitly not implemented:

- `POST /auth/email/magic-link` unless SMTP is configured or `PULLWISE_ENABLE_DEV_MAGIC_LINKS=true`
- `GET /dev/magic-links` unless `PULLWISE_ENABLE_DEV_MAGIC_LINKS=true`
- `POST /issues/{id}/fixes/apply`
- `POST /issues/{id}/pull-requests`
- `GET /billing/plan`
- `POST /billing/checkout-sessions`
- `POST /billing/portal-sessions`
- Slack or Linear integration writes

Those endpoints return `501 Not Implemented` instead of fake success payloads.
