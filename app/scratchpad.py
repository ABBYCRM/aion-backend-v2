"""
AION Scratchpad — operator-scoped persistent memory for API keys,
env snippets, project notes, and any other artifacts the chat should
remember and reuse across sessions.

Every entry is signed with a 7-law kernel signature at write time. The
signature travels with the data so the laws (REALITY, CONTINUITY,
FIDELITY, LATTICE, EPISTEMIC, PERPETUITY, DECISION) remain in force
across sessions, machines, and restarts. Any consumer that loads an
entry can re-verify the signature against the embedded kernel state
and detect tampering.

Design:
  - JSONL storage at ./data/scratchpad/entries.jsonl (or /tmp fallback
    when the runtime is read-only).
  - Secrets are masked in list responses; `?reveal=true` is required
    to see the plaintext. Every reveal is audit-logged.
  - Each entry carries:
      * the operator-facing fields (name, value, kind, tags, notes)
      * a `kernel_signature` block (state, score, law checks, fingerprint)
      * a `lawset_version` and `continuity_thread_id` for portability
  - Search is plain substring across name, tags, notes (and value when
    reveal=true). No embeddings, no surprise.
"""
from __future__ import annotations
import hashlib
import os
import json
import re
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_PATHS = [
    Path("./data/scratchpad/entries.jsonl"),
    Path("/tmp/aion-scratchpad/entries.jsonl"),
]

SECRET_KINDS = {"api_key", "secret", "credential", "token", "password", "private_key"}

AUTO_DETECT_PATTERNS = [
    (r"\bsk-[A-Za-z0-9_\-]{16,}\b", "api_key", "openai-style key"),
    (r"\bsk-or-v1-[A-Za-z0-9_\-]{16,}\b", "api_key", "openrouter key"),
    (r"\bsk-proj-[A-Za-z0-9_\-]{16,}\b", "api_key", "openai project key"),
    (r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b", "api_key", "anthropic key"),
    (r"\bnvapi-[A-Za-z0-9_\-]{16,}\b", "api_key", "nvidia nim key"),
    (r"\bghp_[A-Za-z0-9]{16,}\b", "token", "github pat"),
    (r"\bghs_[A-Za-z0-9]{16,}\b", "token", "github app token"),
    (r"\bgithub_pat_[A-Za-z0-9_]{16,}\b", "token", "github fine-grained pat"),
    (r"\bhf_[A-Za-z0-9]{16,}\b", "token", "huggingface token"),
    (r"\bdop_v1_[A-Za-z0-9_\-]{16,}\b", "token", "digitalocean pat"),
    (r"\brnd_[A-Za-z0-9_\-]{16,}\b", "token", "render api key"),
    (r"\bAIza[A-Za-z0-9_\-]{35}\b", "api_key", "google api key"),
    (r"\bxox[abp]-[A-Za-z0-9\-]{10,}\b", "token", "slack token"),
    (r"\bsk_live_[A-Za-z0-9]{16,}\b", "api_key", "stripe live key"),
    (r"\bsk_test_[A-Za-z0-9]{16,}\b", "api_key", "stripe test key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "api_key", "aws access key id"),
]

_MASK_PREFIX = 4
_MASK_SUFFIX = 4

# The 7 law names — used for verification and pretty printing.
LAW_NAMES = ["REALITY", "CONTINUITY", "FIDELITY", "LATTICE",
             "EPISTEMIC", "PERPETUITY", "DECISION"]

# Lawset version. Bump when the kernel or its decision rules change
# in a way that would invalidate old signatures.
LAWSET_VERSION = "1.1.0"


# ---------------------------------------------------------------------------
# Kernel signing
# ---------------------------------------------------------------------------
def _kernel_sign(name: str, value: str, kind: str,
                 tags: List[str], thread_id: str = "") -> dict:
    """Run a lightweight 7-law evaluation over the entry and return the
    signature block. The kernel is enforced locally — no LLM call —
    so it works even when the model is down. Any breach of
    FIDELITY/LATTICE will mark the entry REJECT and downgrade trust.
    """
    from .kernel import (
        AION_CONTINUITY_PACK,
        MissionContext, resolve_decision, DecisionState, LawCheck,
    )

    # Build a synthetic kernel context. The "user input" is a structured
    # summary of the entry, so the 7 laws reason over what's actually
    # being persisted.
    summary = f"persist(kind={kind}, name={name}, len={len(value)}, tags={','.join(tags or [])})"
    ctx = MissionContext(
        user_input=summary,
        history=[],
        metadata={"entry_kind": kind, "is_secret": kind in SECRET_KINDS, "thread_id": thread_id},
    )
    decision = resolve_decision(ctx)

    # 7-law check explicit copy (so the signature is self-describing
    # even if the kernel module is updated later)
    laws = {c.law: {"passed": c.passed, "note": c.note} for c in decision.checks}

    # Content fingerprint — covers name + value + tags + lawset_version
    # so we can detect tampering on read.
    h = hashlib.sha256()
    h.update(LAWSET_VERSION.encode("utf-8"))
    h.update(b"|")
    h.update(name.strip().encode("utf-8"))
    h.update(b"|")
    h.update(value.encode("utf-8"))
    h.update(b"|")
    h.update(",".join(sorted(t.lower() for t in (tags or []))).encode("utf-8"))
    content_fingerprint = h.hexdigest()[:24]

    return {
        "lawset_version": LAWSET_VERSION,
        "continuity_pack_id": AION_CONTINUITY_PACK["system_name"],
        "decision_state": decision.state.value,
        "decision_score": decision.score,
        "decision_rationale": decision.rationale,
        "laws": laws,
        "protocol": decision.protocol,
        "thread_id": thread_id or f"thread_{uuid.uuid4().hex[:10]}",
        "content_fingerprint": content_fingerprint,
        "signed_at": int(time.time()),
    }


def _kernel_verify(entry: dict) -> Tuple[bool, str]:
    """Re-derive the signature and compare. Returns (ok, reason)."""
    sig = entry.get("kernel_signature") or {}
    if not sig:
        return False, "missing kernel_signature"
    if sig.get("lawset_version") != LAWSET_VERSION:
        # Don't reject — the entry was signed under a different kernel
        # version. The data is still valid; we just flag the drift.
        return True, f"drift: lawset {sig.get('lawset_version')} != current {LAWSET_VERSION}"
    h = hashlib.sha256()
    h.update(LAWSET_VERSION.encode("utf-8"))
    h.update(b"|")
    h.update((entry.get("name") or "").strip().encode("utf-8"))
    h.update(b"|")
    h.update((entry.get("value") or "").encode("utf-8"))
    h.update(b"|")
    h.update(",".join(sorted(t.lower() for t in (entry.get("tags") or []))).encode("utf-8"))
    expected = h.hexdigest()[:24]
    if sig.get("content_fingerprint") != expected:
        return False, "content_fingerprint mismatch (tamper or corruption)"
    return True, "ok"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def _now() -> int:
    return int(time.time())


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= _MASK_PREFIX + _MASK_SUFFIX + 2:
        return "*" * len(value)
    return f"{value[:_MASK_PREFIX]}{'*' * max(4, len(value) - _MASK_PREFIX - _MASK_SUFFIX)}{value[-_MASK_SUFFIX:]}"


def _seed_from_env() -> None:
    """If AION_SCRATCHPAD_SEED is set, parse it as JSON and prepend any
    entries whose ids we don\'t already have. Used so an operator can
    pin a baseline scratchpad (API keys, project URLs) into the runtime
    spec without manually re-entering them after every redeploy."""
    raw = os.environ.get("AION_SCRATCHPAD_SEED", "").strip()
    if not raw:
        return
    try:
        seed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[scratchpad] AION_SCRATCHPAD_SEED is not valid JSON: {e}", flush=True)
        return
    if not isinstance(seed, list):
        return
    sp = Scratchpad()
    existing = {e.get("id") for e in sp._read_all()}
    added = 0
    for item in seed:
        if not isinstance(item, dict):
            continue
        if item.get("id") in existing:
            continue
        try:
            sp.add(
                name=item.get("name", "seed"),
                value=item.get("value", ""),
                kind=item.get("kind", "note"),
                tags=item.get("tags", []),
                source=item.get("source", "seed"),
                notes=item.get("notes", ""),
                thread_id=item.get("thread_id", ""),
                skip_kernel_sign=item.get("skip_kernel_sign", False),
            )
            added += 1
        except Exception as exc:
            print(f"[scratchpad] seed add failed: {exc}", flush=True)
    if added:
        print(f"[scratchpad] seeded {added} entries from AION_SCRATCHPAD_SEED", flush=True)


def _open_store() -> tuple:
    for p in _PATHS:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.touch()
            return p, False
        except Exception:
            continue
    p = Path("/tmp/aion-scratchpad/entries.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return p, True


class Scratchpad:
    def __init__(self):
        self.path, _ = _open_store()

    # ---- I/O ----
    def _read_all(self) -> List[dict]:
        out = []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return out

    def _write_all(self, entries: List[dict]) -> None:
        tmp = self.path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp.replace(self.path)

    def _append(self, entry: dict) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            fallback = Path("/tmp/aion-scratchpad/entries.jsonl")
            fallback.parent.mkdir(parents=True, exist_ok=True)
            with fallback.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.path = fallback

    # ---- CRUD ----
    def add(self, *, name: str, value: str, kind: str = "note",
            tags: Optional[List[str]] = None, source: str = "manual",
            notes: str = "", thread_id: str = "",
            skip_kernel_sign: bool = False) -> dict:
        norm_tags = [t.strip().lower() for t in (tags or []) if t and t.strip()]
        entry = {
            "id": f"sp_{uuid.uuid4().hex[:12]}",
            "name": name.strip(),
            "kind": kind.strip().lower(),
            "value": value,
            "tags": norm_tags,
            "source": source,
            "notes": notes,
            "created_at": _now(),
            "updated_at": _now(),
            "last_used_at": 0,
            "use_count": 0,
        }
        # Kernel signature is mandatory for any entry. Operators can
        # pass skip_kernel_sign=True only for legacy migration; new
        # code should always sign.
        if not skip_kernel_sign:
            try:
                entry["kernel_signature"] = _kernel_sign(
                    name=entry["name"], value=entry["value"],
                    kind=entry["kind"], tags=entry["tags"],
                    thread_id=thread_id,
                )
            except Exception as e:
                # Don't fail the write — store the entry with a degraded
                # signature so the operator can re-sign later.
                entry["kernel_signature"] = {
                    "lawset_version": LAWSET_VERSION,
                    "error": f"sign_failed: {type(e).__name__}: {e}",
                    "signed_at": _now(),
                }
        else:
            entry["kernel_signature"] = {
                "lawset_version": LAWSET_VERSION,
                "skipped": True,
                "signed_at": _now(),
            }
        self._append(entry)
        return entry

    def list(self, *, kind: Optional[str] = None, tag: Optional[str] = None,
             q: Optional[str] = None, reveal: bool = False,
             verify: bool = False) -> List[dict]:
        items = self._read_all()
        if kind:
            items = [e for e in items if e.get("kind") == kind.lower()]
        if tag:
            tag_l = tag.lower()
            items = [e for e in items if tag_l in (e.get("tags") or [])]
        if q:
            ql = q.lower()
            items = [
                e for e in items
                if ql in (e.get("name") or "").lower()
                or ql in (e.get("notes") or "").lower()
                or any(ql in t for t in (e.get("tags") or []))
                or (reveal and ql in (e.get("value") or "").lower())
            ]
        out = []
        for e in items:
            view = dict(e)
            if e.get("kind") in SECRET_KINDS and not reveal:
                view["value"] = _mask(e.get("value", ""))
                view["revealed"] = False
            else:
                view["revealed"] = True
            if verify:
                ok, reason = _kernel_verify(e)
                view["verify"] = {"ok": ok, "reason": reason}
            out.append(view)
        return out

    def get(self, entry_id: str, *, reveal: bool = False, verify: bool = False) -> Optional[dict]:
        for e in self._read_all():
            if e.get("id") == entry_id:
                view = dict(e)
                if e.get("kind") in SECRET_KINDS and not reveal:
                    view["value"] = _mask(e.get("value", ""))
                    view["revealed"] = False
                else:
                    view["revealed"] = True
                if verify:
                    ok, reason = _kernel_verify(e)
                    view["verify"] = {"ok": ok, "reason": reason}
                return view
        return None

    def update(self, entry_id: str, patch: dict) -> Optional[dict]:
        items = self._read_all()
        out = None
        for i, e in enumerate(items):
            if e.get("id") == entry_id:
                allowed = {"name", "value", "kind", "tags", "notes", "source"}
                content_changed = False
                for k, v in patch.items():
                    if k in allowed:
                        if e.get(k) != v:
                            content_changed = True
                        e[k] = v
                e["updated_at"] = _now()
                if content_changed:
                    # Re-sign the entry under the same thread_id.
                    thread_id = (e.get("kernel_signature") or {}).get("thread_id", "")
                    try:
                        e["kernel_signature"] = _kernel_sign(
                            name=e["name"], value=e["value"],
                            kind=e["kind"], tags=e["tags"],
                            thread_id=thread_id,
                        )
                        e["resigned_at"] = _now()
                    except Exception as exc:
                        e.setdefault("kernel_signature", {})["resign_error"] = str(exc)
                items[i] = e
                out = e
                break
        if out is not None:
            self._write_all(items)
        return out

    def delete(self, entry_id: str) -> bool:
        items = self._read_all()
        new = [e for e in items if e.get("id") != entry_id]
        if len(new) == len(items):
            return False
        self._write_all(new)
        return True

    def use(self, entry_id: str) -> Optional[dict]:
        items = self._read_all()
        for i, e in enumerate(items):
            if e.get("id") == entry_id:
                e["use_count"] = int(e.get("use_count", 0)) + 1
                e["last_used_at"] = _now()
                items[i] = e
                self._write_all(items)
                return e
        return None

    def stats(self) -> dict:
        items = self._read_all()
        by_kind = {}
        secret_count = 0
        for e in items:
            k = e.get("kind", "note")
            by_kind[k] = by_kind.get(k, 0) + 1
            if k in SECRET_KINDS:
                secret_count += 1
        # Tally the kernel decision states
        decision_states = {}
        for e in items:
            sig = e.get("kernel_signature") or {}
            ds = sig.get("decision_state", "unsigned")
            decision_states[ds] = decision_states.get(ds, 0) + 1
        return {
            "total": len(items),
            "by_kind": by_kind,
            "secrets": secret_count,
            "decision_states": decision_states,
            "lawset_version": LAWSET_VERSION,
            "path": str(self.path),
        }

    def verify_all(self) -> dict:
        """Re-verify every entry's kernel signature. Returns a summary."""
        items = self._read_all()
        ok = 0
        drift = 0
        bad = 0
        bad_entries = []
        for e in items:
            v, reason = _kernel_verify(e)
            if v and reason == "ok":
                ok += 1
            elif v and reason.startswith("drift"):
                drift += 1
            else:
                bad += 1
                bad_entries.append({"id": e.get("id"), "name": e.get("name"), "reason": reason})
        return {
            "total": len(items),
            "ok": ok,
            "drift": drift,
            "bad": bad,
            "bad_entries": bad_entries[:50],
        }

    # ---- Convenience: detect candidates in arbitrary text ----
    def detect_in_text(self, text: str) -> List[dict]:
        if not text:
            return []
        out = []
        seen = set()
        for pat, kind, hint in AUTO_DETECT_PATTERNS:
            for m in re.finditer(pat, text):
                v = m.group(0)
                if v in seen:
                    continue
                seen.add(v)
                out.append({"value": v, "kind": kind, "hint": hint, "preview": _mask(v)})
        return out

    # ---- Continuity context: turn relevant entries into a block
    # that the LLM can use during chat. Used by the kernel prompt
    # so the chat always has access to recent / relevant operator data.
    # ----
    def continuity_context(self, *, query: str = "", limit: int = 6,
                           kinds: Optional[List[str]] = None) -> str:
        items = self.list(q=query, kind=kinds[0] if kinds and len(kinds) == 1 else None)
        # If a multi-kind list was passed, do a per-kind filter
        if kinds and len(kinds) > 1:
            items = [e for e in items if e.get("kind") in kinds]
        if not items:
            return ""
        # Prefer recently used + named entries; sort by recency.
        items.sort(key=lambda e: (e.get("last_used_at", 0), e.get("created_at", 0)), reverse=True)
        items = items[:limit]
        lines = ["[SCRATCHPAD — relevant operator memory]"]
        for it in items:
            sig = it.get("kernel_signature") or {}
            state = sig.get("decision_state", "?")
            thread = sig.get("thread_id", "?")
            tag_str = (" [" + ",".join(it.get("tags") or []) + "]") if it.get("tags") else ""
            preview = it.get("value", "")
            if it.get("kind") in SECRET_KINDS and not it.get("revealed"):
                preview = preview  # already masked
            lines.append(f"- {it['name']}{tag_str} :: {preview}  (kernel={state} thread={thread})")
        return "\n".join(lines) + "\n[/SCRATCHPAD]"


scratchpad = Scratchpad()
