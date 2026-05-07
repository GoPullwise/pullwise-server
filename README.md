# Pullwise Server

A lightweight Python development API that matches the current `pullwise-web` frontend contract.

By default it keeps the local development mock for GitHub login and repository authorization. When GitHub OAuth and GitHub App environment variables are configured, those same endpoints switch to the real GitHub flows through Authlib and PyGithub. The server auto-loads `.env` from this directory when it starts.

## Run

A local `.env` is included for development and defaults to `http://localhost:3000` -> `http://localhost:5174`.

```powershell
python -m pip install -e .
python -m pullwise_server
```

Then point the frontend at:

```text
VITE_API_BASE_URL=http://localhost:3000
```

The server persists sessions, GitHub authorization state, selected repositories, scans, issues, and settings in SQLite by default at `.pullwise/pullwise.sqlite3`. Override it with `PULLWISE_DB_PATH=F:\path\to\pullwise.sqlite3` if needed.

## Local Flow

- GitHub identity login returns a local callback URL and creates a cookie session unless real OAuth credentials are configured.
- Email magic link returns `magicLink` / `devMagicLink`; the frontend shows it as a local development shortcut.
- GitHub repository authorization is separate from login and redirects to the repository picker unless a real GitHub App is configured.
- `GET /dev/magic-links` lists unexpired local magic links for debugging.

## Real GitHub Setup

Create or configure a GitHub App for Pullwise. In the app settings:

- Homepage URL: `http://localhost:5174`
- Callback URL for user authorization: `http://localhost:3000/auth/github/callback`
- Setup URL: `http://localhost:3000/integrations/github/callback`
- Permissions: `Contents: read`, `Metadata: read`, `Pull requests: read/write`; add write permissions only for features you actually enable.

Then set these variables in `F:\pullwise-server\.env`:

```env
PULLWISE_GITHUB_CLIENT_ID=your_oauth_or_github_app_client_id
PULLWISE_GITHUB_CLIENT_SECRET=your_oauth_or_github_app_client_secret
PULLWISE_GITHUB_APP_SLUG=your-github-app-slug
PULLWISE_GITHUB_APP_ID=123456
PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH=F:\path\to\pullwise.private-key.pem
```

`PULLWISE_GITHUB_CLIENT_ID` / `PULLWISE_GITHUB_CLIENT_SECRET` make `/auth/github/authorize` jump to real GitHub sign-in. `PULLWISE_GITHUB_APP_SLUG` makes `/integrations/github/authorize` jump to the GitHub App installation screen. `PULLWISE_GITHUB_APP_ID` plus the private key let the server mint an installation token and list the repositories that were authorized.

For deployment or secret stores, use `PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64` instead of `PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH`.

## Frontend Contract

Identity login:

- `GET /auth/session`
- `GET /auth/github/authorize?redirectTo=...`
- `GET /auth/github/callback?redirectTo=...`
- `POST /auth/email/magic-link`
- `GET /auth/email/callback?token=...`
- `GET /dev/magic-links` for local debugging only
- `POST /auth/sign-out`

GitHub repository authorization:

- `GET /integrations/github/authorize?scope=all|selected&redirectTo=...`
- `GET /integrations/github/callback?scope=all|selected&redirectTo=...`
- `GET /integrations`
- `POST /integrations/{provider}/connect`
- `DELETE /integrations/{provider}`

Prototype data APIs:

- `GET /repositories`, `POST /repositories/sync`
- `GET /scans`, `POST /scans`, `GET /scans/{id}`, `POST /scans/{id}/cancel`
- `GET /issues`, `GET /issues/{id}`, `PATCH /issues/{id}/status`
- `POST /issues/{id}/fixes/apply`, `POST /issues/{id}/pull-requests`
- `GET /settings`, `PATCH /settings`
- `GET /billing/plan`, `POST /billing/checkout-sessions`, `POST /billing/portal-sessions`

## Environment

See `.env.example`. The server reads `.env` automatically first, then falls back to process environment variables and built-in local defaults.
