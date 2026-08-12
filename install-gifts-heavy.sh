#!/bin/sh
# v2.8.14 — install each heavy package one at a time, log success/fail.
# Operator said "install all" — if any of these don't fit in the DO basic-xxs
# image, we log it and move on. Each package is isolated so one failure
# doesn't kill the others.
set +e
LOG=/var/data/gifts_install.log
mkdir -p "$(dirname "$LOG")"
> "$LOG"

while IFS= read -r line; do
    case "$line" in ""|\#*) continue ;; esac
    pkg=$(echo "$line" | tr -d '[:space:]')
    if [ -z "$pkg" ]; then continue; fi
    echo "[gifts] installing $pkg..." | tee -a "$LOG"
    if pip install --no-cache-dir --disable-pip-version-check "$pkg" >>"$LOG" 2>&1; then
        echo "[gifts] OK  $pkg" | tee -a "$LOG"
    else
        echo "[gifts] FAIL $pkg" | tee -a "$LOG"
        # Try to clean up partial install
        base=$(echo "$pkg" | cut -d= -f1 | cut -d'<' -f1 | cut -d'>' -f1)
        pip uninstall -y "$base" >>"$LOG" 2>&1 || true
    fi
done < /app/requirements-gifts-heavy.txt

# Also try the browsers the heavy packages need
echo "[gifts] installing playwright chromium..." | tee -a "$LOG"
if command -v playwright >/dev/null 2>&1; then
    playwright install chromium >>"$LOG" 2>&1 || echo "[gifts] playwright chromium install failed" | tee -a "$LOG"
fi

echo "[gifts] install log written to $LOG"
