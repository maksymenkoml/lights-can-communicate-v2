# Repository automation

The owner requested GitHub Actions, pre-commit/pre-push hooks, Dependabot and coding-agent rules that prohibit work directly on main.

Use the existing shell/standard-library toolchain. Pre-commit checks the index snapshot, not potentially different working files; it rejects protected branches, known private configuration, whitespace problems, Python lint and JavaScript syntax errors. Pre-push additionally rejects protected destination refs and retains the full current tests/archive contracts. Setup warms the pinned Ruff tool cache and installs the tracked hooks without modifying runtime state.

CI runs with read-only repository permissions, no secrets or deployment jobs, SHA-pinned actions and pinned development dependencies. A second job runs the existing vendor contracts in the same pinned diyHue image without networking. Dependabot covers GitHub Actions, the Ruff requirements manifest and Docker Compose. Shared AGENTS.md rules are imported by other agent entry points. All development uses feature branches and owner-merged PRs.

A generic pre-commit framework would add another dependency without replacing the existing full-suite hook. Native hooks preserve the project's current workflow and allow precise index checks. This is the implementation choice for the requested scope. Hosted branch-protection settings are outside this change; local hooks and tracked instructions enforce the requested development workflow but are not a server authorization boundary.
