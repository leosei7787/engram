#!/bin/bash
# Launch the engram dashboard server under launchd.
#
# Resolves the repo root from the script's own location, so the LaunchAgent
# plist only needs to point at this file — moving the repo only requires
# updating WorkingDirectory + ProgramArguments in the plist, not this script.
#
# launchd does NOT inherit your shell PATH. Be explicit so python3, the
# Anthropic SDK install, and the `claude` CLI used by the chat backend
# (cfg.chat.backend: cli) are all reachable.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# miniconda first (Python 3.13 — matches pyproject.toml requires-python >=3.11)
# nvm node bin for the `claude` CLI used by chat backend
export PATH="$HOME/miniconda3/bin:$HOME/.nvm/versions/node/v22.16.0/bin:/usr/local/bin:/usr/bin:/bin"

mkdir -p "$HOME/Library/Logs"
exec python3 engram/dashboard/server.py
