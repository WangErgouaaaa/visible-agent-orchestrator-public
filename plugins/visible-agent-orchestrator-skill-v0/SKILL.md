---
name: visible-agent-orchestrator
description: Safely inspect and operate a portable tmux-backed visible-worker control plane with explicit roots, binding, authorization, locks, receipts, and terminal acceptance.
---

# Visible Agent Orchestrator

Use this Skill when the task explicitly involves visible CLI workers managed by
Visible Agent Orchestrator.

## Root and scope

1. Resolve the exact runtime root from the task or `VAO_ROOT`; if neither is
   set, use `~/vao-runtime`.
2. Pass that same absolute root to every root-scoped command with `--root`.
3. Keep business workspaces and task artifacts separate from the runtime root.
4. Treat source, installed runtime, discovery package, worker binding, protocol
   completion and content acceptance as separate states.


Omitted optional runtime roots use `VAO_ROOT` and then `~/vao-runtime`; an explicit `--root`, including `.`, takes precedence. Worker IDs come from the operator-selected registry and task contract. Required execution-boundary guidance is shipped in [worker-contracts.md](references/worker-contracts.md).

## Read-only first

For root inventory:

```bash
"$VAO_ROOT/bin/omx_visible_dispatch" status --root "$VAO_ROOT" --all-workers --json
```

For a worker-scoped task, resolve exactly one worker and then run the supported
binding and readiness checks:

```bash
"$VAO_ROOT/bin/omx_visible_rebind" check --root "$VAO_ROOT" --worker example-worker --runtime-backend tmux-reference --json
"$VAO_ROOT/bin/omx_visible_dispatch" doctor --root "$VAO_ROOT" --worker example-worker --json
"$VAO_ROOT/bin/omx_visible_dispatch" locks --root "$VAO_ROOT" --worker example-worker --json
```

Validate command success, required field types, and the semantic predicate.
Parseable JSON or a discovered pane does not establish a valid binding.

## Dispatch

Use the sanctioned wrapper. Never substitute raw tmux input or direct TUI HTTP
calls.

Before dispatch, require:

- current strong binding for the exact worker;
- task-scoped authorization;
- no blocking target lock;
- an explicit `session_action` and reason;
- a task-owned instruction file and receipt path;
- the user's authority for the real worker action.

Typical continuing task:

```bash
"$VAO_ROOT/bin/omx_visible_dispatch" run-one \
  --root "$VAO_ROOT" \
  --worker example-worker \
  --title "Task title" \
  --body-file "$PWD/task.md" \
  --session-action keep \
  --session-action-reason same_domain_continuation \
  --receipt-out "$PWD/receipt.json" \
  --timeout 300 \
  --allow-real
```

`keep` requires compatible domain, role and contract with useful context.
Choose `compact`, `rotate`, or `recovery` only when its documented gate and
required identity fields apply. A timeout retains the task and lock; observe the
same attempt instead of redispatching it.

## Completion

Completion requires a non-empty `report.md` and exactly one of `.done`, `.fail`,
or `.blocked`, with matching fence/owner and any configured terminal acceptance.
The leader reads the report and makes the required content decision. A quiet
pane, idle process, HTTP success, watcher state, or marker alone is not enough.

## Hard boundaries

- First installation uses the documented offline bootstrap manifest only for an unused runtime; updates keep the default worker smoke gates.
- Do not guess worker identity, repair a binding, dispatch, install, or publish
  from root inventory alone.
- Do not release non-terminal locks or manufacture terminal markers.
- Do not place credentials, provider configuration, live identities, prompts,
  receipts or task evidence in the source repository.
- Do not use an unrelated environment root for a task already bound to an
  explicit root.
- tmux is the supported reference substrate; experimental adapters do not
  inherit that support status.
