#!/bin/sh
# v2.8.15 — install each heavy package one at a time, log success/fail.
# Operator said "install all" — if any of these don't fit in the DO basic-xxs
# build memory, we log it and move on. Each package is isolated so one
# failure doesn't kill the others.
#
# v2.8.15 also:
# - On FAIL, retry with --ignore-requires-python (handles deps that
#   incorrectly pin py3.11, e.g. isage-intent pinned to ==3.11.*)
# - On final FAIL, retry with --no-deps (last resort: get the package
#   itself in even if transitive deps can't resolve)
# - Aggressively clean up partial downloads between attempts
set +e
LOG=/var/data/gifts_install.log
mkdir -p "$(dirname "$LOG")"
> "$LOG"

for line in $(grep -v "^#" /app/requirements-gifts-heavy.txt | grep -v "^$"); do
    pkg=$(echo "$line" | tr -d '[:space:]')
    if [ -z "$pkg" ]; then continue; fi
    echo "[gifts] installing $pkg..." | tee -a "$LOG"
    if pip install --no-cache-dir --disable-pip-version-check "$pkg" >>"$LOG" 2>&1; then
        echo "[gifts] OK   $pkg" | tee -a "$LOG"
        continue
    fi
    # Cleanup partial downloads
    rm -rf /tmp/pip-* /tmp/*.whl 2>/dev/null || true
    echo "[gifts] FAIL $pkg (deps may pin wrong py version; retrying with --ignore-requires-python)" | tee -a "$LOG"
    if pip install --no-cache-dir --disable-pip-version-check --ignore-requires-python "$pkg" >>"$LOG" 2>&1; then
        echo "[gifts] OK   $pkg (with --ignore-requires-python)" | tee -a "$LOG"
        continue
    fi
    rm -rf /tmp/pip-* /tmp/*.whl 2>/dev/null || true
    echo "[gifts] FAIL $pkg (last resort: --no-deps)" | tee -a "$LOG"
    if pip install --no-cache-dir --disable-pip-version-check --ignore-requires-python --no-deps "$pkg" >>"$LOG" 2>&1; then
        echo "[gifts] PARTIAL $pkg (--no-deps; transitive deps NOT installed)" | tee -a "$LOG"
    else
        echo "[gifts] FAIL $pkg (all retries failed)" | tee -a "$LOG"
    fi
    # Cleanup
    rm -rf /tmp/pip-* /tmp/*.whl 2>/dev/null || true
done

# Playwright chromium (needed for browser-automation-cli + linkedin_scraper)
if command -v playwright >/dev/null 2>&1; then
    echo "[gifts] installing playwright chromium..." | tee -a "$LOG"
    if playwright install chromium >>"$LOG" 2>&1; then
        echo "[gifts] OK   playwright chromium" | tee -a "$LOG"
    else
        echo "[gifts] FAIL playwright chromium" | tee -a "$LOG"
    fi
fi

# Final cleanup
rm -rf /tmp/pip-* /tmp/*.whl /tmp/.cache 2>/dev/null || true
echo "[gifts] install log written to $LOG"
