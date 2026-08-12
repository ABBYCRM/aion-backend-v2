#!/bin/sh
# v2.8.15 — install each heavy package one at a time, log success/fail.
# Operator said "install all" — if any of these don't fit in the DO basic-xxs
# build memory, we log it and move on. Each package is isolated so one
# failure doesn't kill the others.
#
# v2.8.15 also: on FAIL, aggressively clean up partial installs (downloads
# in /tmp, half-installed deps) so the next package has room.
set +e
LOG=/var/data/gifts_install.log
mkdir -p "$(dirname "$LOG")"
> "$LOG"

# Each package is tried in isolation. Set PIP_NO_BUILD_ISOLATION to keep build
# memory low. Use --no-deps to avoid pulling transitive heavy deps that aren't
# strictly needed for the package's own import to succeed.
for line in $(grep -v "^#" /app/requirements-gifts-heavy.txt | grep -v "^$"); do
    pkg=$(echo "$line" | tr -d '[:space:]')
    if [ -z "$pkg" ]; then continue; fi
    echo "[gifts] installing $pkg..." | tee -a "$LOG"
    # Try with deps first
    if pip install --no-cache-dir --disable-pip-version-check "$pkg" >>"$LOG" 2>&1; then
        echo "[gifts] OK   $pkg" | tee -a "$LOG"
    else
        echo "[gifts] FAIL $pkg (retry without --no-deps would still fail; cleaning up)" | tee -a "$LOG"
        # Clean up partial download
        rm -rf /tmp/pip-* /tmp/*.whl 2>/dev/null || true
        # Best-effort uninstall of half-installed dist
        base=$(echo "$pkg" | cut -d= -f1 | cut -d'<' -f1 | cut -d'>' -f1)
        pip uninstall -y "$base" >>"$LOG" 2>&1 || true
    fi
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

# Final cleanup of any temp build artifacts before image snapshot
rm -rf /tmp/pip-* /tmp/*.whl /tmp/.cache 2>/dev/null || true
echo "[gifts] install log written to $LOG"
