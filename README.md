# AION backend 2.1

Authenticated FastAPI runtime for AION chat, bounded web search, allowlisted GitHub App operations, and optional owner-scoped notes.

## Production invariants

- `AION_API_KEYS` and distinct `AION_ADMIN_KEYS` are mandatory.
- `CORS_ORIGINS` must contain exact HTTPS frontend origins.
- Models are configured as exact provider/model pairs. Set `PRIMARY_PROVIDER` + `PRIMARY_MODEL`; every fallback should be `provider:model` in `FALLBACK_MODELS`.
- GitHub access is denied unless `GITHUB_ALLOWED_REPOSITORIES` is non-empty.
- GitHub token fallback is disabled unless `ALLOW_GITHUB_TOKEN_FALLBACK=true`.
- GitHub writes additionally require an admin key, `GITHUB_WRITE_ENABLED=true`, and `X-AION-Confirm: yes`.
- Notes are excluded from model context by default and only matching notes are included after explicit client opt-in.
- App Platform files are ephemeral. In production, notes are disabled unless a PostgreSQL `DATABASE_URL` is configured. Audit events always go to structured stdout and are also persisted when PostgreSQL is configured.

## Local development

```bash
cp .env.example .env
pip install -r requirements-dev.txt
ENVIRONMENT=development ALLOW_UNAUTHENTICATED_DEV=true uvicorn app.main:app --reload
```

## DigitalOcean

The repository `.do/app.yaml` deploys the API from `main`. Attach a PostgreSQL component or managed database and bind its connection string to `DATABASE_URL` for durable notes and audit history. Do not put secrets in Git, notes, chat, or frontend storage.
