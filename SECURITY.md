# AION Backend — Security Posture

Defensive OPSEC for the Aion runtime. **This file is about protecting Aion
and its users, not about attacking other systems.** Every measure listed here
runs against Aion's own assets.

## What Aion does today

### Authentication & Authorization

| Control | Where | Behavior |
|---------|-------|----------|
| User / Admin API key split | `app/auth.py` | `X-AION-Key` (user) vs `X-AION-Admin-Key` (admin). Admin endpoints refuse user keys. |
| Fail-closed at startup | `app/settings.py` | Missing `AION_API_KEYS` or `AION_ADMIN_KEYS` = process refuses to start. |
| CORS allowlist (exact match) | `app/settings.py` | `CORS_ORIGINS` env var. Bare origin, no path, no creds unless allowlisted. |
| `allowCustomApiBase: false` | `app/config.js` (frontend) | The PWA refuses to point at any backend origin other than the one in `AION_CONFIG.apiBase`. |

### Encryption

| Control | Where | Notes |
|---------|-------|-------|
| TLS in transit | DO App Platform edge | All public traffic is HTTPS. |
| Vault master encryption | `app/vault.py` | Fernet (`cryptography.fernet`) with `AION_VAULT_MASTER_KEY`. Required at startup. |
| Audit log redaction | `app/audit.py` | Sensitive keys (`api_key`, `token`, `password`, `secret`, `messages`, `prompt`, etc.) are replaced with `[REDACTED]` before write. Strings > 500 chars are truncated. |
| No secrets in source | `.gitignore` | `.env`, `.env.*`, `*.sqlite3`, `audit.jsonl` are git-ignored. |
| No secrets in image | `.dockerignore` | `.env`, `.env.*`, `tests/`, all sqlite + audit files are docker-ignored. |

### Audit & Observability

| Control | Where | Notes |
|---------|-------|-------|
| Structured audit log | `app/audit.py` | JSONL on the volume (`$AION_DATA_DIR/audit.jsonl`). Every tool call, every skill run, every decision is recorded. |
| Bounded retention | `app/settings.py` | `audit_retention_lines` (default 10,000) — log is truncated when it grows past the limit. |
| `/api/audit/recent` | `app/main.py` | Admin-only. Returns the last N events for the operator UI. |
| `/api/health/security` | `app/main.py` | Reports pinned vs live package versions, audit log size + last mtime, vault state, CORS allowlist, auth env presence. **Never returns secret values.** |
| `/api/health/corpus` | `app/main.py` | Reports RAG collection counts, scenario store row count, on-disk file counts for the code corpora. |

### Input Validation

| Control | Where | Notes |
|---------|-------|-------|
| Skill input schemas | `app/skills/seed_all.py` | Every SkillSpec has `input_schema` (JSON Schema). The runner rejects inputs that don't match. |
| Path traversal protection | `app/main.py` (`/api/notes`, `/api/gallery`, `/api/scratchpad`) | Owner-scope enforced; no path concatenation. |
| GitHub allowlist | `app/settings.py` | `GITHUB_ALLOWED_REPOSITORIES` — fail-closed if empty. Write additionally requires admin key + `GITHUB_WRITE_ENABLED` + `X-AION-Confirm` header. |
| `policy_for_tool_error` | `app/skills/policy_action_map.py` | Allowlist of text-only `if_action` / `else_action` mappings. No `eval`, no shell. |
| Tool-output sanitizer (P0 P1 next) | TBD | See `RUNBOOK.md` → "Tool output injection mitigation" — the next security milestone. |

## Defense-in-depth checklist for new deploys

When you stand up a new Aion instance, before pointing any user at it:

- [ ] `AION_API_KEYS` set (one or more user keys)
- [ ] `AION_ADMIN_KEYS` set (one or more admin keys)
- [ ] `AION_VAULT_MASTER_KEY` set (32-byte url-safe base64, 44 chars)
- [ ] `CORS_ORIGINS` set to the **exact** frontend origin (no trailing slash, no path)
- [ ] `GITHUB_ALLOWED_REPOSITORIES` set if you want GitHub chat to work (otherwise the chat DEFERs cleanly on any GitHub request)
- [ ] `AION_BRAIN_API_KEY` set if you want the Brain decision service enabled
- [ ] `AION_DATA_DIR=/var/data` is a real mounted volume (not `/app/data`)
- [ ] `/api/health/security` reports `vault.configured: true`
- [ ] `/api/health/security` reports `audit_log.size_bytes > 0` after the first request
- [ ] `/api/health/corpus` reports `coding_books >= 39` and `scenario_store.total_rows >= 5000`
- [ ] `requirements.txt` is pinned (`==`, no `>=`); the security endpoint reports the same versions in `pinned_versions` and `live_versions`

## Secrets that should never be in chat / scrape / RAG

These are explicit `FORBIDDEN` strings in the chat system prompt and the tool wrappers:

- API keys (`sk-...`, `aion_user_...`, `aion_admin_...`, GitHub `ghp_...`, `ghs_...`, etc.)
- Vault master key (the 44-char Fernet key)
- Database connection strings
- Email Resend API key
- Provider API keys for OpenAI / OpenRouter / Moonshot / Brave / Tavily / Exa / Firecrawl

If a user pastes one into chat, the audit log records the event but the
key is redacted. The operator should rotate it on first sight.

## Vulnerability disclosure

Email the maintainer (or open a private security advisory on the GitHub
repo) before disclosing publicly. Aion has a 7-day response window for
critical issues.

## Out of scope (handled outside Aion)

| Control | Owner | Notes |
|---------|-------|-------|
| Network firewall / WAF | DigitalOcean App Platform | DO edge handles DDoS + rate limit |
| Intrusion Detection | Network operator | Snort / Suricata run at the network edge, not in the app container |
| DDoS mitigation | DO + Cloudflare | Aion does not re-implement |
| TLS certificate management | DO | Auto-renewed by the platform |
| Provider AUP compliance | Operator | Aion is a tool; what you do with it is your responsibility |

## Reference

- For runtime posture: `GET /api/health/security`
- For corpus state: `GET /api/health/corpus`
- For audit trail: `GET /api/audit/recent?n=50` (admin)
- For incident response: see `RUNBOOK.md`
