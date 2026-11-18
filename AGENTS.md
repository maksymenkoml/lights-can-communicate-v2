# Repository instructions

## Branches and pull requests

- Inspect `git status --short --branch` before making changes. `main`, `master`
  and detached HEAD are read-only: create or select a feature branch first.
- Start new work from current `origin/main`, using a descriptive `feat/`, `fix/`
  or `chore/` branch. Reuse an existing suitable linked worktree; otherwise place
  worktrees under the common repository's ignored `.gitworktrees/<branch>`.
- Never commit or push directly to `main`/`master`, including explicit destination
  refspecs. Never bypass hooks, use `--no-verify`, force-push or weaken checks to
  make a change pass. Do not reset, rename or delete protected branches.
- Commit verified coherent changes to the feature branch and open a draft PR.
  Fix CI and current-head review findings. Only the owner merges; do not merge
  branches or PRs, enable auto-merge or approve your own PR.
- Preserve other sessions' work, existing unstaged edits and untracked files.

## Setup and verification

Requirements: Python 3.10+, Node.js 24+ and `uv`/`uvx`.
Run `sh scripts/setup-dev.sh` in a new clone before editing. It prepares pinned
Ruff and configures `core.hooksPath=.githooks`; Git does not install hooks on clone.

- Pre-commit checks the staged snapshot for private configuration, whitespace,
  Python lint and JavaScript syntax without stashing or modifying working files.
- Before pushing, run `sh .githooks/pre-push </dev/null`. It runs lint, all Node
  and Python tests and both executable-archive profiles. The actual push also
  checks destination refs. Fix failures rather than skipping the hook.
- CI additionally tests the pinned public diyHue image with networking disabled.
  Tests need no real lamps, Hue credentials or NAS access. Do not claim skipped
  vendor or physical-device checks passed.
- Keep Python runtime dependencies in the standard library. Development tools
  belong in `requirements-dev.txt`; Actions use full commit SHAs.

## Runtime and private state

- Repository maintenance does not authorize changing running services, NAS
  installations, pairing/credentials, physical power or light output.
- Never commit `.lighting.json`, `.lighting-stream.json`, `.sync-box.json`, `.env`,
  certificate/private-key material or `experiments/hue-bridge/state/`.
- Preserve configured device IDs, HomeKit accessories/scenes, calibration and
  external Off requests. Discovery must not enroll additional outputs implicitly.
- The runtime spans `lights_can_communicate_v2_core/`, `experiments/hue-bridge/` and
  `experiments/setup-studio/`; `tests/` holds offline contracts. See `core/README.md`.
- Communicate with the owner in Ukrainian; code, comments and repository docs use
  English. Never launch games. Name temporary helpers `tmp_*` and clean only
  owned, unused artifacts after verification.
