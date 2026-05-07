# Pullwise Server

A lightweight Python development API that matches the current `pullwise-web` frontend contract.

It is intentionally dependency-free and uses the Python standard library so it can run without installing packages. The auth implementation is a local development mock: GitHub OAuth and magic email links create local sessions and redirect back to the frontend.

## Run

```powershell
python -m pullwise_server --host 0.0.0.0 --port 3000
```

Then point the frontend at:

```text
VITE_API_BASE_URL=http://localhost:3000
```

## Frontend Contract

Identity login:

- `GET /auth/session`
- `GET /auth/github/authorize?redirectTo=...`
- `GET /auth/github/callback?redirectTo=...`
- `POST /auth/email/magic-link`
- `GET /auth/email/callback?token=...`
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

See `.env.example`. The server reads environment variables directly; no `.env` loader is required.
