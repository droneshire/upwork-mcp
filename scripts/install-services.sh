#!/usr/bin/env bash
# Install systemd user services (Linux). Assumes the repo is at
# ~/Documents/upwork-mcp; edit the unit files' paths otherwise.
#   ./scripts/install-services.sh               # debug Chrome on login
#   ./scripts/install-services.sh --with-worker # also run the worker as a service
set -euo pipefail
cd "$(dirname "$0")/.."
units=(upwork-chrome.service)
[[ "${1:-}" == "--with-worker" ]] && units+=(upwork-worker.service)
mkdir -p ~/.config/systemd/user
for u in "${units[@]}"; do ln -sf "$PWD/systemd/$u" ~/.config/systemd/user/; done
systemctl --user daemon-reload
systemctl --user enable --now "${units[@]}"
systemctl --user --no-pager status "${units[@]}" | grep -E "●|Active:"
