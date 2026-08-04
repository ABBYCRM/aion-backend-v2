# AION backend

FastAPI runtime for AION with authenticated chat, bounded provider failover, owner-scoped notes, Brave web search, and GitHub App integration.

## Security model

- Every `/api/*` route requires `X-AION-Key` or `Authorization: Bearer ...`.
- Admin routes require a separate key from `AION_ADMIN_KEYS`.
- GitHub writes additionally require `GITHUB_WRITE_ENABLED=true` and `X-AION-Confirm: yes`.
- Provider, Brave, and GitHub credentials exist only in deployment secrets.
- Notes reject values that resemble API keys or private keys.
- Client messages may only use `user` and `assistant` roles.
- Request size, message count, completion tokens, concurrency, and rate limits are enforced server-side.

## Local start

```bash
cp .env.example .env
set -a; . ./.env; set +a
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
```

For unauthenticated local development only, set:

```bash
ENVIRONMENT=development
ALLOW_UNAUTHENTICATED_DEV=true
```

Production startup fails when `AION_API_KEYS` or `AION_ADMIN_KEYS` is absent.

## Web search

Set `BRAVE_API_KEY`. The client can enable search on a chat turn or send:

```text
/search current FastAPI security guidance
```

Search results are inserted as untrusted tool data with numbered source markers. The model is instructed to cite those markers.

## GitHub

A GitHub App is the preferred credential:

```text
GITHUB_APP_ID
GITHUB_INSTALLATION_ID
GITHUB_PRIVATE_KEY
GITHUB_ALLOWED_REPOSITORIES=ABBYCRM/aion-frontend,ABBYCRM/aion-backend-v2
```

Read commands supported in chat:

```text
/github ABBYCRM/aion-frontend repo
/github ABBYCRM/aion-frontend issues
/github ABBYCRM/aion-frontend file app.js
/github ABBYCRM/aion-backend-v2 search resolve_model_chain
```

The REST endpoints also support repository metadata, file reads, issue listing, and code search. Admin-confirmed write endpoints can create issues, branches, files, and draft pull requests when writes are explicitly enabled.

## Deployment

`.do/app.yaml` is the single checked-in deployment specification. Configure all secrets and the exact frontend origin in the DigitalOcean control panel:

```text
AION_API_KEYS
AION_ADMIN_KEYS
CORS_ORIGINS
OPENROUTER_API_KEY or another provider key
BRAVE_API_KEY
GitHub App credentials
```

Do not add these values to the repository, notes database, frontend, or chat context.
