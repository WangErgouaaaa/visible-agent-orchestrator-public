# Portability

VAO uses one operator-selected runtime root. The source checkout, runtime root,
business workspaces, and user-level companion Skill stores are distinct.

## Path model

| Path | Purpose | Example |
|---|---|---|
| Source checkout | Reviewed code and install manifest | `~/src/visible-agent-orchestrator` |
| Runtime root | Installed `bin/`, `lib/`, locks, receipts and `.omx/` state | `~/vao-runtime` |
| Business workspace | The repository a worker operates on | `~/work/example-project` |
| Backup directory | Task-owned install rollback material | `~/.local/state/vao-install-backups/run-001` |

Set `VAO_ROOT` for shell convenience and continue to pass the root explicitly:

```bash
export VAO_ROOT="$HOME/vao-runtime"
"$VAO_ROOT/bin/omx_visible_dispatch" status --root "$VAO_ROOT" --json
```

When no root is selected by an entrypoint that supports the shared default, the
portable fallback is `~/vao-runtime`. Explicit `--root` or `--target` remains
the clearest choice for automation. A root-specific operation must use its
actual selected root; an unrelated environment default must never redirect it.

## Paths containing spaces

Always quote shell paths:

```bash
export VAO_ROOT="$HOME/VAO Runtime"
./bin/omx_install_sync plan --source "$PWD" --target "$VAO_ROOT"
"$VAO_ROOT/bin/omx_visible_dispatch" status --root "$VAO_ROOT" --json
```

Programmatic callers should pass argument lists rather than constructing shell
strings.

## Root isolation

Two runtime roots are two independent control planes. Their worker registry,
locks, receipts, task directories, watcher observations, and runtime identity
must not cross. Do not point one root at another root's `.omx` directory or
reuse task/lock paths between roots.

Changing `VAO_ROOT` does not migrate state. Plan a new installation, register
workers again, and retain the old root until its tasks are terminal and its
rollback obligations are complete.

## Host-local data

Keep these outside the source repository:

- worker ports, pane identifiers, session identifiers and local registry data;
- provider/model endpoints and credentials;
- live approval and runtime JSON;
- task reports, prompts, receipts, locks and watcher telemetry;
- install backups and immutable active Skill generations.

Synthetic examples may use placeholder worker names and loopback ports. They
do not establish a valid live binding or grant permission to dispatch.

## Platform boundary

The supported reference environment is Linux with tmux. OpenCode is needed only
for visible OpenCode worker operation. The offline meeting path needs neither a
running worker nor a model provider. Experimental runtime adapters may be useful
for development, but do not carry the reference runtime's support claim.
