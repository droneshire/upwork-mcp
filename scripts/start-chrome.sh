#!/usr/bin/env bash
# Launch Chrome with remote debugging on localhost, using a dedicated profile
# (Chrome 136+ refuses remote debugging on the default profile).
# Log into Upwork once in this window; the session persists in the profile.
set -euo pipefail

PORT="${CHROME_DEBUG_PORT:-9222}"
PROFILE="${UPWORK_CHROME_PROFILE:-$HOME/.upwork-chrome-profile}"
CHROME="${CHROME_BIN:-$(command -v google-chrome || command -v google-chrome-stable || command -v chromium)}"

mkdir -p "$PROFILE"

# "Continue where you left off": keeps session cookies (Upwork's login) across
# Chrome restarts, e.g. when switching from --login mode to debug mode.
PREFS="$PROFILE/Default/Preferences"
if [[ -f "$PREFS" ]] && ! pgrep -f -- "--user-data-dir=$PROFILE" >/dev/null; then
  python3 - "$PREFS" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d.setdefault("session", {})["restore_on_startup"] = 1
json.dump(d, open(p, "w"))
PY
fi

# `--login`: open the same profile WITHOUT remote debugging. Google sign-in
# ("Continue with Google") refuses or loops in debug-enabled browsers, so log
# in this way once, close Chrome, then start normally.
if [[ "${1:-}" == "--login" ]]; then
  shift
  exec "$CHROME" --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check \
    "https://www.upwork.com/ab/account-security/login" "$@"
fi

exec "$CHROME" \
  --remote-debugging-port="$PORT" \
  --remote-debugging-address=127.0.0.1 \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  "https://www.upwork.com/nx/find-work/best-matches" "$@"
