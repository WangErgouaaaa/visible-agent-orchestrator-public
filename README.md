# Visible Agent Orchestrator

Visible Agent Orchestrator (VAO) is a Linux control plane for coordinating
terminal-visible AI workers. It keeps task authorization, worker binding,
fencing, locks, receipts, reports, and terminal acceptance outside the worker
itself.

The public repository contains the generic multi-agent framework and two
companion Skills. It does not contain domain-specific workflows, provider credentials,
private prompts, live session data, or host-specific worker registrations.

## What it provides

- a tmux-backed reference runtime for visible OpenCode workers;
- explicit worker/session binding and per-task authorization;
- owner locks and fencing against duplicate or stale submissions;
- durable receipts plus non-empty reports and exactly one terminal marker;
- guarded installation and rollback planning;
- an offline AG2-style structured self-review helper;
- immutable, registry-managed companion Skill packages.

VAO does not install or modify OpenCode, Codex, model providers, tmux, or
credentials. `rmux` adapters are experimental surfaces; tmux is the supported
reference runtime.

## Requirements

- Linux
- Python 3.11 or newer
- tmux
- OpenCode only when using the visible-worker runtime
- Git for source installation and companion Skill release checks

The default meeting path is deterministic and offline. A real AG2 model call is
optional and requires an explicit per-run approval plus host-local configuration.

## Runtime layout

Choose one runtime root. The examples use `~/vao-runtime`:

```bash
export VAO_ROOT="$HOME/vao-runtime"
```

The installer maintains this layout below that prefix:

```text
$VAO_ROOT/
├── bin/
├── lib/visible_agent_orchestrator/
└── .omx/
```

Business repositories stay outside the runtime root. Pass the selected root to
root-scoped commands with `--root "$VAO_ROOT"`; installation uses
`--target "$VAO_ROOT"`. Quoting is required when the path contains spaces.

## Install into an isolated prefix

From a clean checkout:

```bash
export VAO_ROOT="$HOME/vao-runtime"
export VAO_BACKUP="$HOME/.local/state/vao-install-backups/initial"

# Once per user: initialize the host-local authorization lifecycle.
./bin/omx_install_sync bootstrap-companion-skills \
  --task-id initial-setup --attempt-id bootstrap-1 \
  --authorize-leader --allow-write --json

./bin/omx_install_sync plan \
  --source "$PWD" \
  --target "$VAO_ROOT" \
  --manifest install/visible-agent-orchestrator.install-manifest.json

./bin/omx_install_sync check \
  --source "$PWD" \
  --target "$VAO_ROOT" \
  --manifest install/visible-agent-orchestrator.install-manifest.json

./bin/omx_install_sync apply \
  --source "$PWD" \
  --target "$VAO_ROOT" \
  --backup-dir "$VAO_BACKUP" \
  --manifest install/visible-agent-orchestrator.install-manifest.json \
  --allow-write --smoke
```

`plan` and `check` are non-mutating. The one-time bootstrap records and consumes
a task-scoped authorization under the exclusive admission and zero-lock gates.
Its state lives in the effective user's `~/.local/state/visible-agent-orchestrator/companion-skills` directory.
`apply` requires that initialized lifecycle plus `--allow-write`; publishing
runtime identity or installing companion Skills has additional authorization
and quiescence gates. Existing attempt ledgers are preserved; after a failed
bootstrap, inspect its result and use a new attempt identifier when retrying.

The supplied manifest uses `offline_bootstrap`: it requires absent install
targets and no existing worker state, then checks every installed command with
`--help`. It does not contact a worker or a provider. Updates to an existing
runtime require a reviewed manifest with `post_install_smoke_profile` set to
`default`, plus `--smoke --smoke-worker <verified-worker-id>`; the ordinary
binding, quiescence, backup, parity and rollback gates still apply.

## First checks

Inventory is read-only:

```bash
"$VAO_ROOT/bin/omx_visible_dispatch" status \
  --root "$VAO_ROOT" \
  --all-workers \
  --json

"$VAO_ROOT/bin/omx_visible_health" check \
  --root "$VAO_ROOT" \
  --all-workers \
  --json
```

A worker-scoped action requires a registered worker and a strong current-window
binding. Discovering a pane does not bind or authorize it:

```bash
"$VAO_ROOT/bin/omx_visible_rebind" discover \
  --root "$VAO_ROOT" \
  --port 4096 \
  --json

"$VAO_ROOT/bin/omx_visible_rebind" check \
  --root "$VAO_ROOT" \
  --worker example-worker \
  --runtime-backend tmux-reference \
  --json
```

See [Operator guide](docs/OPERATOR_GUIDE.md) before dispatching a real task and
[Portability](docs/PORTABILITY.md) before installing outside the default path.

## Offline meeting helper

The two public profiles are `idea-review` and `code-design-review`:

```bash
"$VAO_ROOT/bin/omx_ag2_meeting" \
  --root "$PWD" \
  --profile code-design-review \
  --task "Review this design" \
  --json
```

This path does not call a model, tmux, or visible workers. It writes meeting
artifacts under the supplied workspace root.

## Safety model

The worker is not the authority. A valid run still needs current binding,
task-scoped authorization, an available target lock, a selected session action,
and a terminal fence. Completion requires a non-empty `report.md` and exactly
one of `.done`, `.fail`, or `.blocked`; marker presence alone does not imply
semantic acceptance by the leader.

Do not place provider configuration, approval JSON, credentials, or live task
evidence in this repository. Keep host-local registration and runtime state
under the selected runtime root.

## Development

Read [AGENTS.md](AGENTS.md) before changing contracts or runtime behavior. Run
the narrowest relevant tests and verify that the published tree contains no
host paths, credentials, domain-specific extensions, caches, or runtime evidence.

## License

MIT. See [LICENSE](LICENSE).
