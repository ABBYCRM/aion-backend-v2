# Notes

## 2026-08-12 (later) — memory, streaming, model upgrade (Claude)

Operator report: "it can't even remember the last thing I asked" + "I need a
stronger model than chat gpt mini" + wants nvidia/xai as alternatives.

- **Stronger default model.** `openai_chat_model`/`primary_model` were
  `gpt-4.1` (the v2.8.17 bump). Bumped to `gpt-5`. `fallback_models` now
  includes `gpt-4.1, nvidia/nemotron-3-super-120b-a12b, grok-4,
  deepseek/deepseek-chat`. Added full xAI (Grok) provider support —
  `XAI_API_KEY`/`XAI_BASE_URL` settings, `_client_for`/`_default_provider`
  wiring in `app/llm.py` (xAI is OpenAI-compatible at `api.x.ai/v1`).
  **Note:** `primary_model`/`fallback_models` must be bare model IDs
  (`gpt-5`, `grok-4`), not `provider/model` — `_default_provider()` matches
  on the ID's own prefix. NVIDIA is the one real exception: its own catalog
  IDs are genuinely namespaced (`nvidia/nemotron-...`). The old default
  (`primary_model=openai/gpt-4.1`) was actually a **pre-existing bug**:
  `_default_provider("openai/gpt-4.1")` doesn't match any prefix rule and
  silently fell through to `openrouter`, so `PRIMARY_MODEL` was never
  actually honored for the direct (non-Brain) chat path. Fixed as part of
  this change.
- **Cost heads-up:** gpt-5 costs substantially more per token than
  gpt-4o-mini. Worth confirming that's the intended tradeoff before this
  reaches production traffic.
- **Continuity / durable memory.** The actual bug lived in Aion-Brain (see
  its NOTES.md) — `/api/chat` there used a fresh random UUID as the memory
  session key on every single request, so cross-turn recall never worked.
  On this side: added `session_id` to `ChatRequest`, threaded it through
  `brain_client.stream_chat()` to Brain's `/api/chat` (falls back to the
  caller's `principal.subject` if the client doesn't send one yet).
- **Streaming**: already worked end-to-end here (SSE `stream_chat` in
  `app/llm.py`, consumed by the frontend's `consumeSse`). No changes needed
  on this side.

Verified: `pytest -q` — identical failure count before/after this diff
(63 failed/223 passed/4 skipped both times — pre-existing, mostly
live-provider-key-dependent). Updated
`test_contract_openai_model_upgraded_off_mini` to assert the new gpt-5
default (same pattern as the test itself already used for the gpt-4.1 bump).

## 2026-08-12 — AION system review (Claude)

Follow-up to a health/correctness review of the backend. Fixed the issues found:

- **`app/skills/seed_all.py`** — the skill registry failed to boot entirely.
  Three bugs, all introduced in the same commit (v2.8.17 Hedra rollout):
  - `wire_executors()` mapped `"builtin:hedra.image_to_video"` to
    `hedra.hedra_image_to_video`, a function that no longer exists in
    `app/skills/clients/hedra.py` (only `hedra_image` / `hedra_video` are
    defined there, and no `SkillSpec` with id `hedra.image_to_video` exists
    either — the capability was folded into `hedra.video` via
    `start_image_url`). Removed the dead dict entry.
  - Two `input_schema` dicts (`hedra.image`, `hedra.video`) used the
    JS/JSON literal `true` instead of Python's `True` for the `poll`
    field's default, raising `NameError` at spec-build time.
  - All three raised before the skill registry could register a single
    executor, so `bootstrap()` failed silently at startup (caught and
    only logged by `app/main.py`) and left the entire skill system dead
    on every boot.
  - Result: full `pytest -q` run went from 97 failed / 189 passed / 4
    skipped to 58 failed / 228 passed / 4 skipped. The remaining 58 are
    pre-existing: most pass in isolation and only fail as part of the
    full run (test-order/shared-state pollution), and a handful need
    live provider API keys not present in this environment. Flagged back
    rather than guess-fixed.

- **Removed the "gifts" package pipeline** — `requirements-gifts-light.txt`,
  `requirements-gifts-heavy.txt`, `install-gifts-heavy.sh`, and the
  corresponding `COPY`/`RUN` block in `Dockerfile`. Grepped all 17 package
  names across `app/` — zero imports anywhere. They were installed into the
  production image purely on an operator chat instruction ("install all"),
  including social-media scrapers (LinkedIn/Instagram/Facebook) with ToS/legal
  exposure and several very new, low-provenance "AI agent router" packages.
  Dead weight with real supply-chain and compliance risk; the rest of the
  Dockerfile is untouched.

- **Minor cleanups**:
  - `.do/app.yaml`: `APP_VERSION` bumped from the stale `2.8.12` to match
    the code's actual default (`app/settings.py:113` → `2.8.17`).
  - `requirements.txt`: `cryptography>=42.0.0` pinned to `==50.0.0` (the
    version that currently resolves), matching `SECURITY.md`'s own stated
    "no `>=`, pinned only" policy.
  - `RUNBOOK.md` §5.1: corrected "gpt-4o-mini is the default" to
    "gpt-4.1 is the default", matching `app/settings.py`'s actual
    `openai_chat_model` / `primary_model` defaults.

All changes are on branch `claude/aion-system-analysis-htqn5i`.
