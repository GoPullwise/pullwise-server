# Pullwise Server

A lightweight Python development API that matches the current `pullwise-web` frontend contract.

It is intentionally dependency-free and uses the Python standard library so it can run without installing packages. The auth implementation is a local development mock: GitHub OAuth and magic email links create local sessions and redirect back to the frontend. The server auto-loads `.env` from this directory when it starts.

## Run

A local `.env` is included for development and defaults to `http://localhost:3000` -> `http://localhost:5174`.

```powershell
python -m pullwise_server
```

Then point the frontend at:

```text
VITE_API_BASE_URL=http://localhost:3000
```

## Local Flow

- GitHub identity login returns a local callback URL and creates a cookie session.
- Email magic link returns `magicLink` / `devMagicLink`; the frontend shows it as a local development shortcut.
- GitHub repository authorization is separate from login and redirects to the repository picker.
- `GET /dev/magic-links` lists unexpired local magic links for debugging.

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
