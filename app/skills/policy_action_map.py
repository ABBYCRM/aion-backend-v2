"""policy_action_map.py — allowlisted action -> real skill mapping.

CSV `if_action` / `else_action` are TEXT, not code. We never eval them.
This module is the small allowlist that maps common policy phrases
to the real skills (or internal ops) the kernel can actually run.

Locked 2026-08-05 per operator plan (P3). Every entry is opt-in:
  - Phrase fragment -> skill id (or internal op)
  - Unknown phrase -> no execution, just evidence injection
  - GitHub writes still require the existing GITHUB_WRITE_ENABLED +
    X-AION-Confirm gate; this map does not bypass that.
"""
from __future__ import annotations

from typing import Any


ACTION_MAP: list[tuple[str, str | None]] = [
    # backoff / retry
    ("backoff per retry-after", "tools.sleep_backoff"),
    ("backoff with retry-after", "tools.sleep_backoff_with_retry_after"),
    ("backoff per x-ratelimit-reset", "tools.sleep_backoff"),
    ("backoff per x-ratelimit", "tools.sleep_backoff"),
    ("backoff", "tools.sleep_backoff"),
    ("wait until x-ratelimit-reset", "tools.sleep_backoff"),
    ("retry-after", "tools.sleep_backoff"),
    ("retry with backoff", "tools.sleep_backoff"),
    ("rebuild and retry", "tools.sleep_backoff"),

    # refresh / re-auth
    ("re-auth", "github.refresh_auth"),
    ("refresh auth", "github.refresh_auth"),
    ("refresh the installation token", "github.refresh_auth"),
    ("regenerate lockfile", "github.regenerate_lockfile"),

    # no-retry (give up fast on 4xx)
    ("do not retry 4xx", "tools.no_retry"),
    ("do not auto-retry", "tools.no_retry"),
    ("halt api activity", "tools.no_retry"),
    ("fall back to cached state", "tools.use_cached_state"),

    # on-call / escalation
    ("page on-call", "ops.notify"),
    ("escalate", "ops.notify"),
    ("require operator", "ops.require_human"),
    ("require sign-off", "ops.require_human"),

    # cache / etag
    ("cache the response", "tools.use_cached_state"),
    ("etag", "tools.use_etag"),
    ("if-none-match", "tools.use_etag"),

    # scraper fallbacks
    ("try the next provider", "scraper.fallback"),
    ("fall through to next", "scraper.fallback"),
    ("fallback to scrapfly", "scraper.fallback"),
    ("fallback to scrapingbee", "scraper.fallback"),

    # ci
    ("re-run just the failed leg", "ci.rerun_leg"),
    ("rebuild and redeploy", "ci.redeploy"),

    # no-op
    ("do not bypass", None),
    ("do not auto-merge", None),
    ("do not eval", None),
    ("do not invent", None),
    ("do not use plain '=='", None),
    ("do not publish", None),
    ("never run else_action", None),
]


def map_action(phrase: str) -> str | None:
    """Find the longest matching fragment; return the handler or None.
    The match is case-insensitive substring. None = no execution allowed."""
    if not phrase:
        return None
    p = phrase.lower()
    best = None
    for frag, handler in ACTION_MAP:
        if frag in p and (best is None or len(frag) > best[0]):
            best = (len(frag), handler)
    return best[1] if best else None


def actions_to_handlers(if_action: str, else_action: str) -> dict[str, str | None]:
    """Map both if/else phrases to their handlers. Used by the kernel
    after a successful policy_for_tool_error match to decide what
    is safely auto-runnable vs evidence-only."""
    return {
        "if": map_action(if_action or ""),
        "else": map_action(else_action or ""),
    }
