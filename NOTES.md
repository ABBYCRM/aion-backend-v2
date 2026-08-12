# Notes

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
