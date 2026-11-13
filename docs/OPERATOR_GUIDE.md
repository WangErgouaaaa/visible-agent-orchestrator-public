# Operator guide

This guide describes the public tmux reference path. Replace the example worker
and port with values from your own host-local registry.

## 1. Select the runtime root

```bash
export VAO_ROOT="$HOME/vao-runtime"
```

Keep the business workspace elsewhere. Use the same quoted root for every
command in one operation.

## 2. Inspect before mutation

Root inventory is read-only and does not prove worker binding:

```bash
"$VAO_ROOT/bin/omx_visible_dispatch" status \
  --root "$VAO_ROOT" --all-workers --json
"$VAO_ROOT/bin/omx_visible_dispatch" doctor \
  --root "$VAO_ROOT" --worker example-worker --json
"$VAO_ROOT/bin/omx_visible_dispatch" locks \
  --root "$VAO_ROOT" --worker example-worker --json
```

Use `omx_visible_rebind discover` for read-only current-window discovery and
`omx_visible_rebind check` to verify a known registry binding. Discovery alone
is not authorization to bind, rebind, or dispatch.

## 3. Choose the session action

Every dispatch records a `session_action` and reason:

- `keep`: same domain and role with useful, healthy context;
- `compact`: same task/domain where context length is the problem;
- `rotate`: new domain, role isolation, incompatible contract, or contaminated
  context;
- `recovery`: an authorized continuation of a specific interrupted attempt.

Rotation and recovery require their additional identity and rehydration fields.
Do not use them to release locks or declare a prior task complete.

## 4. Dispatch one task

Put long instructions in a task-owned file so they do not appear in the process
list:

```bash
"$VAO_ROOT/bin/omx_visible_dispatch" run-one \
  --root "$VAO_ROOT" \
  --worker example-worker \
  --title "Review change" \
  --body-file "$PWD/review-task.md" \
  --session-action keep \
  --session-action-reason same_domain_continuation \
  --timeout 300 \
  --receipt-out "$PWD/review-receipt.json" \
  --allow-real
```

`--timeout` is the caller's wait budget. Expiry can leave the same attempt
running with its owner lock retained; it does not authorize a retry, rotation,
or second dispatch.

## 5. Accept completion

Protocol completion requires:

- a non-empty `report.md`;
- exactly one of `.done`, `.fail`, or `.blocked`;
- matching task, attempt, worker, fence and owner evidence;
- any configured terminal-acceptance gate.

Use the task-state/receipt surfaces to read this state. A quiet pane, HTTP
success, an idle worker, or a marker by itself is insufficient. The leader must
still inspect the report and make the content decision required by the task.

## Watcher boundary

The watcher observes task directories and writes watcher-only artifacts. It
does not send input, settle tasks, release locks, or decide acceptance:

```bash
"$VAO_ROOT/bin/omx_visible_watcher" status \
  --root "$VAO_ROOT" --worker example-worker --json
```

## Installation boundary

Source checks, installation, companion Skill activation, and live dispatch are
separate operations. Review `omx_install_sync plan` and `check` before an
authorized `apply`. Do not copy files into an active runtime by hand.
