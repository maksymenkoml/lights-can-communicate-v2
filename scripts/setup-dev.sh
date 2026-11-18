#!/bin/sh
set -eu
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
command -v python3 >/dev/null
command -v node >/dev/null
command -v uvx >/dev/null
UV_TOOL_DIR="$PWD/.ruff_cache/tools" uvx --cache-dir .ruff_cache/uv --from "$(cat requirements-dev.txt)" ruff --version
# Exact HAP-NodeJS release used by Homebridge 2.5.0, for the plugin contracts.
npm ci --prefix homebridge --no-audit --no-fund --ignore-scripts >/dev/null
git config core.hooksPath .githooks
echo 'Development tools ready; tracked pre-commit and pre-push hooks installed.'
