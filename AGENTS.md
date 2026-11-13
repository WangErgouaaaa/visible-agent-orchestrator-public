# Contributor guidance

This repository is the portable source distribution of Visible Agent
Orchestrator. Source state, installed state, worker binding, and task acceptance
are separate facts.

## Boundaries

- Keep the public tree generic. Do not add credentials, provider settings,
  personal paths, real worker identities, private prompts, task evidence, or
  business-repository content.
- Keep the runtime root configurable. Root-scoped operations must use the
  selected `--root`; installation must use the selected `--target`. Business
  workspaces remain separate from the runtime root.
- Preserve authorization, canonical-path and symlink validation, fencing,
  binding, owner locks, receipts, report/terminal-marker acceptance, and
  guarded install/rollback checks.
- tmux is the supported runtime substrate. Do not present experimental rmux
  adapters as production support.
- Keep domain-specific extensions outside this repository. Public meeting
  profiles are limited to `idea-review` and `code-design-review`.
- Do not patch or bundle upstream OpenCode, Codex, tmux, model-provider, or AG2
  implementations.

## Companion Skills

`contracts/companion-skills.registry.json` is the managed inventory. When a
registered Skill changes, update its canonical source and every listed
discovery copy in the same change. The corresponding `SKILL.md` files must be
byte-identical. Record an impact disposition for both managed Skills and run the
repository's companion impact/status checks.

## Changes and verification

Use small changes that preserve current contracts. Add compatibility or
migration code only for a named caller and an executable acceptance case. New
dependencies require explicit project approval.

Run the narrowest relevant test first. Runtime-root changes need coverage for
an arbitrary absolute path, a path containing spaces, and isolation between two
roots. Documentation and registry-only changes require link/path scans,
canonical/discovery parity, JSON validation, and `git diff --check`.

Never use a development test as authority to mutate an installed runtime or a
live worker. Installation, live dispatch, publication, and remote writes are
separate operator actions with their own gates.
