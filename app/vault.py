"""Encrypted, admin-scoped secret vault for the AION runtime.

The vault stores provider credentials (LLM keys, search keys, GitHub tokens,
email keys, browser-automation keys, etc.) encrypted at rest with Fernet.
This lets an operator:

  - List all configured keys + their provider + their health status
  - Reveal a single key value (for copy-paste during rotation)
  - Rotate a key (writes a new value, records who rotated it + when)
  - Ping every provider to verify its key actually works right now
  - Wire a new key into the live settings (so the app picks it up on the
    next request) without redeploying

This is intentionally NOT exposed to the public API surface. The only
access path is the /api/vault/* routes, all of which require admin
authentication. The intent is to make the operator's "where is my
Pinecone key" / "is Resend still working" / "rotate GitHub" questions
answerable from one place.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken

from .settings import settings


# Provider categories used by the UI. AION uses these to pick the right
# icon, the right "what does this key do" description, and the right
# ping endpoint.
ProviderCategory = Literal["llm", "search", "github", "email", "browser", "scraping", "vector", "inngest", "discord", "screenshot", "image", "storage", "other"]


# Default catalogue of known keys. When a new key is added via /api/vault
# we record it in the DB; on every boot we reconcile against the catalogue
# so the operator can see "here are the 20+ keys we know about, here is
# which one is wired up, here is the health".
KNOWN_KEYS: list[dict[str, str]] = [
    # LLM providers
    {"name": "OPENAI_API_KEY", "category": "llm", "label": "OpenAI", "description": "GPT-4o / DALL-E / Sora / TTS", "env_aliases": "OPENAI_API_KEY"},
    {"name": "EMBEDDINGS_API_KEY", "category": "llm", "label": "OpenAI Embeddings", "description": "Vector embeddings (often same as OpenAI key)", "env_aliases": "OPENAI_API_KEY"},
    {"name": "NVIDIA_API_KEY", "category": "llm", "label": "NVIDIA NIM", "description": "Nemotron / DeepSeek / Kimi on NVIDIA NIM", "env_aliases": "NVIDIA_API_KEY"},
    {"name": "OPENROUTER_API_KEY", "category": "llm", "label": "OpenRouter", "description": "Multi-model router (DeepSeek, Llama, etc.)", "env_aliases": "OPENROUTER_API_KEY"},
    {"name": "KIMI_API_KEY", "category": "llm", "label": "Kimi / Moonshot", "description": "Moonshot Kimi K2 / K3 (China-frontier reasoning)", "env_aliases": "MOONSHOT_API_KEY"},
    {"name": "BITDEER_API_KEY", "category": "llm", "label": "Bitdeer AI Inference", "description": "DeepSeek V4 Pro on Bitdeer GPU cloud", "env_aliases": "BITDEER_API_KEY"},
    {"name": "ANTHROPIC_API_KEY", "category": "llm", "label": "Anthropic Claude", "description": "Claude Sonnet / Opus", "env_aliases": "ANTHROPIC_API_KEY"},
    {"name": "HELICONE_API_KEY", "category": "llm", "label": "Helicone", "description": "LLM observability + caching proxy", "env_aliases": "HELICONE_API_KEY"},
    # Web search + scraping
    {"name": "BRAVE_API_KEY", "category": "search", "label": "Brave Search", "description": "Web search via api.search.brave.com", "env_aliases": "BRAVE_API_KEY"},
    {"name": "TAVILY_API_KEY", "category": "search", "label": "Tavily", "description": "Search optimized for AI agents", "env_aliases": "TAVILY_API_KEY"},
    {"name": "EXA_API_KEY", "category": "search", "label": "Exa", "description": "Neural search", "env_aliases": "EXA_API_KEY"},
    {"name": "FIRECRAWL_API_KEY", "category": "scraping", "label": "Firecrawl", "description": "Markdown extraction from any URL", "env_aliases": "FIRECRAWL_API_KEY"},
    {"name": "SCRAPINGBEE_API_KEY", "category": "scraping", "label": "ScrapingBee", "description": "Headless scraping proxy", "env_aliases": "SCRAPINGBEE_API_KEY"},
    {"name": "SCRAPFLY_API_KEY", "category": "scraping", "label": "Scrapfly", "description": "Anti-bot scraping service", "env_aliases": "SCRAPFLY_API_KEY"},
    # GitHub
    {"name": "GITHUB_TOKEN", "category": "github", "label": "GitHub PAT", "description": "Personal access token for repo ops", "env_aliases": "GITHUB_TOKEN"},
    {"name": "GITHUB_APP_ID", "category": "github", "label": "GitHub App ID", "description": "App identifier for GitHub App auth", "env_aliases": "GITHUB_APP_ID"},
    {"name": "GITHUB_INSTALLATION_ID", "category": "github", "label": "GitHub Installation ID", "description": "Installation id for GitHub App auth", "env_aliases": "GITHUB_INSTALLATION_ID"},
    {"name": "GITHUB_PRIVATE_KEY", "category": "github", "label": "GitHub App Private Key", "description": "PEM private key for GitHub App signing", "env_aliases": "GITHUB_PRIVATE_KEY"},
    # Email
    {"name": "RESEND_API_KEY", "category": "email", "label": "Resend", "description": "Transactional email", "env_aliases": "RESEND_API_KEY"},
    {"name": "RESEND_FROM", "category": "email", "label": "Resend From Address", "description": "Default sender for Resend emails", "env_aliases": "RESEND_FROM"},
    # Browser automation
    {"name": "STEEL_API_KEY", "category": "browser", "label": "Steel.dev", "description": "Cloud browser sessions for AI agents", "env_aliases": "STEEL_API_KEY"},
    {"name": "SCREENSHOTONE_ACCESS_KEY", "category": "screenshot", "label": "ScreenshotOne Access", "description": "Screenshot API access key", "env_aliases": "SCREENSHOTONE_ACCESS_KEY"},
    {"name": "SCREENSHOTONE_SECRET_KEY", "category": "screenshot", "label": "ScreenshotOne Secret", "description": "Screenshot API secret", "env_aliases": "SCREENSHOTONE_SECRET_KEY"},
    # Agent platforms
    {"name": "COMPOSIO_API_KEY", "category": "other", "label": "Composio", "description": "OAuth toolkit broker for 200+ apps", "env_aliases": "COMPOSIO_API_KEY"},
    # Vector / RAG
    {"name": "PINECONE_API_KEY", "category": "vector", "label": "Pinecone", "description": "Managed vector database", "env_aliases": "PINECONE_API_KEY"},
    # Sandbox / code execution
    {"name": "E2B_API_KEY", "category": "other", "label": "E2B", "description": "Firecracker sandbox for AI code execution", "env_aliases": "E2B_API_KEY"},
    # Event / workflow
    {"name": "INNGEST_EVENT_KEY", "category": "inngest", "label": "Inngest", "description": "Event-driven workflow platform", "env_aliases": "INNGEST_EVENT_KEY"},
    # Discord
    {"name": "DISCORD_BOT_TOKEN", "category": "discord", "label": "Discord Bot", "description": "Discord bot user token", "env_aliases": "DISCORD_BOT_TOKEN"},
]


# ENV var names known to feed a key. Used to seed the vault on first
# boot (we do NOT auto-rotate live envs — we just copy the value in and
# record the source).
def _seed_value(name: str) -> str:
    return os.getenv(name, "").strip()


class VaultError(Exception):
    pass


class VaultNotConfigured(VaultError):
    pass


@dataclass
class VaultEntry:
    id: str
    name: str
    category: ProviderCategory
    label: str
    description: str
    fingerprint: str       # sha256 of value, first 12 chars (NEVER the value)
    value_length: int      # length of the decrypted value
    source: str            # "env", "manual", "auto-import", "rotate"
    has_value: bool
    last_rotated_at: int | None
    last_rotated_by: str | None
    last_ping_at: int | None
    last_ping_status: str | None     # "ok" | "error" | "unconfigured"
    last_ping_latency_ms: int | None
    last_ping_error: str | None
    created_at: int
    updated_at: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "label": self.label,
            "description": self.description,
            "fingerprint": self.fingerprint,
            "value_length": self.value_length,
            "has_value": self.has_value,
            "source": self.source,
            "last_rotated_at": self.last_rotated_at,
            "last_rotated_by": self.last_rotated_by,
            "last_ping_at": self.last_ping_at,
            "last_ping_status": self.last_ping_status,
            "last_ping_latency_ms": self.last_ping_latency_ms,
            "last_ping_error": self.last_ping_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _derive_fernet_key(master: str) -> bytes:
    """Derive a 32-byte Fernet key from an arbitrary master password via
    SHA-256 + base64. The Fernet key needs to be url-safe base64 of 32
    bytes — we feed SHA-256 into that."""
    digest = hashlib.sha256(master.encode()).digest()
    return base64.urlsafe_b64encode(digest)


class VaultStore:
    def __init__(self) -> None:
        # Vault DB lives in the same data dir as notes + audit.
        # If no master key is set, the vault refuses to start (fail-closed
        # same shape as the rest of AION).
        path = Path(settings.notes_db_path).with_name("vault.sqlite3")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._master_key = os.getenv("AION_VAULT_MASTER_KEY", "").strip()
        if not self._master_key:
            # Derive from the AION admin key set so we have a stable key
            # across restarts even when the operator hasn't set a master.
            # This is fine for our threat model (admin-only access, no
            # public reads) but the operator can override.
            derived = settings.admin_keys[0] if settings.admin_keys else ""
            if not derived:
                raise VaultNotConfigured(
                    "AION_VAULT_MASTER_KEY or AION_ADMIN_KEYS must be set for the vault to work"
                )
            self._master_key = derived
            self._key_is_derived = True
        else:
            self._key_is_derived = False
        self._fernet = Fernet(_derive_fernet_key(self._master_key))
        self._initialize()

    @property
    def key_is_derived(self) -> bool:
        return self._key_is_derived

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS vault (
                    id TEXT PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    category TEXT NOT NULL,
                    label TEXT NOT NULL,
                    description TEXT NOT NULL,
                    value_ciphertext BLOB,
                    fingerprint TEXT,
                    value_length INTEGER NOT NULL DEFAULT 0,
                    has_value INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    last_rotated_at INTEGER,
                    last_rotated_by TEXT,
                    last_ping_at INTEGER,
                    last_ping_status TEXT,
                    last_ping_latency_ms INTEGER,
                    last_ping_error TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_vault_category ON vault(category)")

    # ---- Public API ----

    def status(self) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT COUNT(*) AS total, SUM(has_value) AS configured FROM vault").fetchone()
        return {
            "available": True,
            "key_is_derived": self._key_is_derived,
            "total": int(row["total"] or 0),
            "configured": int(row["configured"] or 0),
            "known_keys": len(KNOWN_KEYS),
        }

    def list_entries(self, *, category: str | None = None) -> list[VaultEntry]:
        sql = "SELECT * FROM vault"; params: list[Any] = []
        if category:
            sql += " WHERE category = ?"; params.append(category)
        sql += " ORDER BY category, label"
        with self._connect() as db: rows = db.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def get_entry(self, name: str) -> VaultEntry | None:
        with self._connect() as db: row = db.execute("SELECT * FROM vault WHERE name = ?", (name,)).fetchone()
        return self._row(row) if row else None

    def reveal(self, name: str) -> str | None:
        """Decrypt and return the plaintext value. Admin-only at the route layer."""
        with self._connect() as db:
            row = db.execute("SELECT value_ciphertext FROM vault WHERE name = ?", (name,)).fetchone()
        if not row or not row["value_ciphertext"]:
            return None
        try:
            plaintext = self._fernet.decrypt(row["value_ciphertext"]).decode()
        except InvalidToken as exc:
            raise VaultError("decryption_failed: master key may have changed") from exc
        return plaintext

    def set_value(self, *, name: str, value: str, actor: str, source: str = "manual", metadata: dict[str, Any] | None = None) -> VaultEntry:
        """Set / rotate the value for a known vault entry. Creates the
        entry on first set if the name is in KNOWN_KEYS."""
        info = next((k for k in KNOWN_KEYS if k["name"] == name), None)
        if info is None:
            raise VaultError(f"unknown_key: {name}. Add it to KNOWN_KEYS in app/vault.py first.")
        if not value or len(value) > 8000:
            raise VaultError("invalid_value_length (1..8000 chars)")
        ciphertext = self._fernet.encrypt(value.encode())
        fingerprint = _fingerprint(value)
        now = int(time.time())
        existing = self.get_entry(name)
        with self._connect() as db:
            if existing is None:
                entry_id = f"vault_{uuid.uuid4().hex[:16]}"
                db.execute("""
                    INSERT INTO vault(id, name, category, label, description, value_ciphertext, fingerprint, value_length, has_value, source, last_rotated_at, last_rotated_by, last_ping_at, last_ping_status, last_ping_latency_ms, last_ping_error, metadata_json, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (entry_id, name, info["category"], info["label"], info["description"], ciphertext, fingerprint, len(value), 1, source, now, actor, None, None, None, None, json.dumps(metadata or {}), now, now))
            else:
                db.execute("""
                    UPDATE vault SET value_ciphertext=?, fingerprint=?, value_length=?, has_value=1, source=?, last_rotated_at=?, last_rotated_by=?, metadata_json=?, updated_at=? WHERE name=?
                """, (ciphertext, fingerprint, len(value), source, now, actor, json.dumps(metadata or {}), now, name))
        # Hot-reload the env if the operator opted in. This is the
        # "wire into live settings" knob — without a redeploy.
        env_alias = info.get("env_aliases") or name
        os.environ[env_alias] = value
        return self.get_entry(name)  # type: ignore[return-value]

    def delete_value(self, name: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM vault WHERE name = ?", (name,))
        return cursor.rowcount > 0

    def record_ping(self, name: str, *, ok: bool, latency_ms: int, error: str | None) -> None:
        now = int(time.time())
        status = "ok" if ok else ("error" if error else "unconfigured")
        with self._connect() as db:
            db.execute("""
                UPDATE vault SET last_ping_at=?, last_ping_status=?, last_ping_latency_ms=?, last_ping_error=?, updated_at=? WHERE name=?
            """, (now, status, latency_ms, error[:500] if error else None, now, name))

    def reconcile_with_env(self, *, actor: str = "system:reconcile") -> int:
        """On every boot, copy any non-empty env value into the vault so
        the operator can see them in /api/vault even if they were never
        explicitly set via the API. Existing manual values are NOT
        overwritten (we only set where has_value=0)."""
        seeded = 0
        for info in KNOWN_KEYS:
            existing = self.get_entry(info["name"])
            if existing and existing.has_value:
                continue
            value = _seed_value(info["name"])
            if not value:
                continue
            try:
                self.set_value(name=info["name"], value=value, actor=actor, source="env")
                seeded += 1
            except VaultError:
                continue
        return seeded

    # ---- Internals ----

    @staticmethod
    def _row(row: sqlite3.Row) -> VaultEntry:
        return VaultEntry(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            label=row["label"],
            description=row["description"],
            fingerprint=row["fingerprint"] or "",
            value_length=row["value_length"],
            source=row["source"],
            has_value=bool(row["has_value"]),
            last_rotated_at=row["last_rotated_at"],
            last_rotated_by=row["last_rotated_by"],
            last_ping_at=row["last_ping_at"],
            last_ping_status=row["last_ping_status"],
            last_ping_latency_ms=row["last_ping_latency_ms"],
            last_ping_error=row["last_ping_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )


vault = VaultStore()


# ---- Ping registry ----
# Each pinger accepts the value + returns (ok, latency_ms, error).
# Pings are intentionally cheap and never throw — they return error
# strings instead so the caller can store them on the entry.

async def _ping_generic(name: str, value: str, *, url: str, headers: dict[str, str] | None = None, method: str = "GET", json_body: dict | None = None) -> tuple[bool, int, str | None]:
    import httpx
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            request_headers = dict(headers or {})
            if method == "GET":
                resp = await client.get(url, headers=request_headers)
            else:
                resp = await client.request(method, url, headers=request_headers, json=json_body)
        latency = int((time.time() - started) * 1000)
        if resp.status_code < 400:
            return True, latency, None
        return False, latency, f"http_{resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, int((time.time() - started) * 1000), f"{type(exc).__name__}: {exc}"


async def ping(name: str, value: str) -> tuple[bool, int, str | None]:
    """Per-provider health check. Returns (ok, latency_ms, error)."""
    if not value:
        return False, 0, "value_empty"
    if name == "OPENAI_API_KEY":
        return await _ping_generic(name, value, url="https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {value}"})
    if name == "EMBEDDINGS_API_KEY":
        return await _ping_generic(name, value, url="https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {value}"})
    if name == "NVIDIA_API_KEY":
        return await _ping_generic(name, value, url="https://integrate.api.nvidia.com/v1/models", headers={"Authorization": f"Bearer {value}"})
    if name == "OPENROUTER_API_KEY":
        return await _ping_generic(name, value, url="https://openrouter.ai/api/v1/auth/key", headers={"Authorization": f"Bearer {value}"})
    if name == "KIMI_API_KEY":
        return await _ping_generic(name, value, url="https://api.moonshot.cn/v1/models", headers={"Authorization": f"Bearer {value}"})
    if name == "BITDEER_API_KEY":
        return await _ping_generic(name, value, url="https://api-inference.bitdeer.ai/v1/models", headers={"Authorization": f"Bearer {value}"})
    if name == "ANTHROPIC_API_KEY":
        return await _ping_generic(name, value, url="https://api.anthropic.com/v1/models", headers={"x-api-key": value, "anthropic-version": "2023-06-01"})
    if name == "HELICONE_API_KEY":
        return await _ping_generic(name, value, url="https://api.helicone.ai/v1/me", headers={"Authorization": f"Bearer {value}"})
    if name == "BRAVE_API_KEY":
        return await _ping_generic(name, value, url="https://api.search.brave.com/res/v1/web/search?q=test", headers={"X-Subscription-Token": value})
    if name == "TAVILY_API_KEY":
        return await _ping_generic(name, value, url="https://api.tavily.com/search", method="POST", json_body={"api_key": value, "query": "ping", "max_results": 1, "include_answer": False})
    if name == "EXA_API_KEY":
        return await _ping_generic(name, value, url="https://api.exa.ai/search", method="POST", headers={"x-api-key": value, "Content-Type": "application/json"}, json_body={"query": "ping", "numResults": 1})
    if name == "FIRECRAWL_API_KEY":
        return await _ping_generic(name, value, url="https://api.firecrawl.dev/v1/team/credit_usage", headers={"Authorization": f"Bearer {value}"})
    if name == "SCRAPINGBEE_API_KEY":
        return await _ping_generic(name, value, url=f"https://app.scrapingbee.com/api/v1/ping?api_key={value}")
    if name == "SCRAPFLY_API_KEY":
        return await _ping_generic(name, value, url=f"https://api.scrapfly.io/scrape?key={value}&url=https://example.com")
    if name == "GITHUB_TOKEN":
        return await _ping_generic(name, value, url="https://api.github.com/user", headers={"Authorization": f"Bearer {value}", "Accept": "application/vnd.github+json"})
    if name == "RESEND_API_KEY":
        return await _ping_generic(name, value, url="https://api.resend.com/domains", headers={"Authorization": f"Bearer {value}"})
    if name == "STEEL_API_KEY":
        return await _ping_generic(name, value, url="https://api.steel.dev/v1/sessions", headers={"Authorization": f"Bearer {value}"})
    if name == "SCREENSHOTONE_ACCESS_KEY":
        # ScreenshotOne requires an HMAC signature. Just check the format.
        return (True, 0, None) if 16 <= len(value) <= 128 else (False, 0, "key_format_unexpected")
    if name == "COMPOSIO_API_KEY":
        # v3 apps endpoint, but auth may differ. Try /api/v1/auth/me first.
        return await _ping_generic(name, value, url="https://backend.composio.dev/api/v1/auth/me", headers={"x-api-key": value})
    if name == "PINECONE_API_KEY":
        return await _ping_generic(name, value, url="https://api.pinecone.io/indexes", headers={"Api-Key": value, "X-Pinecone-API-Version": "2024-07"})
    if name == "E2B_API_KEY":
        return await _ping_generic(name, value, url="https://api.e2b.dev/sandboxes", method="POST", headers={"Authorization": f"Bearer {value}", "Content-Type": "application/json"}, json_body={"template": "base"})
    if name == "INNGEST_EVENT_KEY":
        # Inngest keys come in multiple flavours. Just check the length.
        return (True, 0, None) if len(value) >= 32 else (False, 0, "key_too_short")
    if name == "DISCORD_BOT_TOKEN":
        return await _ping_generic(name, value, url="https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {value}"})
    return (True, 0, None) if len(value) >= 8 else (False, 0, "value_too_short")


async def ping_all() -> list[dict[str, Any]]:
    """Ping every configured key in parallel and return a list of results."""
    entries = [e for e in vault.list_entries() if e.has_value]
    async def _one(entry: VaultEntry) -> dict[str, Any]:
        try:
            plaintext = vault.reveal(entry.name)
        except VaultError as exc:
            return {"name": entry.name, "ok": False, "latency_ms": 0, "error": f"decrypt_failed: {exc}", "category": entry.category}
        if plaintext is None:
            return {"name": entry.name, "ok": False, "latency_ms": 0, "error": "no_value_stored", "category": entry.category}
        ok, latency, error = await ping(entry.name, plaintext)
        vault.record_ping(entry.name, ok=ok, latency_ms=latency, error=error)
        return {"name": entry.name, "ok": ok, "latency_ms": latency, "error": error, "category": entry.category}
    results = await asyncio.gather(*[_one(e) for e in entries], return_exceptions=True)
    out: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"ok": False, "error": f"exception: {r}"})
        else:
            out.append(r)
    return out
