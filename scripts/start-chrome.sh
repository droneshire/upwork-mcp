#!/usr/bin/env bash
# Launch Chrome with remote debugging on localhost, using a dedicated profile
# (Chrome 136+ refuses remote debugging on the default profile).
# Log into Upwork once in this window; the session persists in the profile.
set -euo pipefail

PORT="${CHROME_DEBUG_PORT:-9222}"
PROFILE="${UPWORK_CHROME_PROFILE:-$HOME/.upwork-chrome-profile}"
CHROME="${CHROME_BIN:-$(command -v google-chrome || command -v google-chrome-stable || command -v chromium)}"

mkdir -p "$PROFILE"
exec "$CHROME" \
  --remote-debugging-port="$PORT" \
  --remote-debugging-address=127.0.0.1 \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  "https://www.upwork.com/nx/find-work/best-matches" "$@"
