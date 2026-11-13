# Worker Contracts

Read only the section named by the generated task prompt. This reference
narrows worker behavior; it never expands task authority, writable paths,
network access, installation rights, or live-worker permissions.

## Execution-Boundary Reading Entry

The generated prompt contains the universal rules and an absolute path to this
file. Follow the quoted heading exactly. If this file is unreadable, report the
exact path and error, continue only work supported by the task contract, and
mark reference-dependent claims unresolved.

## Permission Versus Expected Product Success (#784)

Authorization to perform an operation is independent of the product succeeding.
Record an authorized operation's observed failure as the result. Do not invent a
new prerequisite or request another approval for an already-authorized,
reversible correction inside the same write scope. Saved data, destructive
changes, extra submissions, and out-of-scope writes still require their own
authority.

## Preparation Versus Frozen/Executed Phases (#788)

Before a declared freeze or first product call, correct preparation errors
inside the authorized write scope, retain the failed preparation evidence, and
revalidate. After freeze or execution, obey the contract's attempt and one-shot
limits. A launched or billed operation counts even when setup later fails; do
not silently add a replacement attempt.

## Review Stage Versus Final Verdict, and Pending-Artifact Waiting (#990/#991)

Keep preliminary review, independent review, and Main's final acceptance
separate. A reviewer may return pending when a named artifact is not yet
available; pending is neither rejection nor approval. Wait only through the
task's bounded observation route, then report the artifact's measured state.
Do not manufacture a verdict from a quiet pane, transport success, timeout, or
missing file.

## Lane handoff (#954)

Publish a partial lane result only when the task assigns that handoff. State the
exact completed scope, files, checks, unresolved items, and ownership of the
next stage. A partial handoff does not mark the whole task complete and does not
authorize another lane to overwrite unrelated work.

## Validation evidence and real command status (#984/#982)

Capture the producer's real exit code immediately. For a pipeline, capture
`PIPESTATUS` before another command changes it. Preserve failed validation
inputs and outputs; a justified retry uses a fresh attempt directory. Report
invocation count separately from successful computations. Wrapper success,
parseable JSON, or a log file does not prove the producer exited zero.

## Original Bytes and Verbatim Labels in Evidence Handoff (#1030)

Call material `original` or `verbatim` only after a direct byte comparison with
the exact bounded source. Otherwise label it a summary, name its provenance,
and disclose any missing original. Do not reconstruct private or unavailable
bytes and present them as source evidence.

## Current-Role Outputs Versus Contract-Owned Next Stages (#1024)

Finish the role currently assigned and hand its outputs to Main. A later
review, merge, install, dispatch, or publication stage belongs to the actor
named by the task contract. A self-check is useful evidence but never substitutes
for a separately required independent review.

## Worker Execution Boundary (Environment, Baselines, Fixtures)

### Usable write scope and nested authority (#996/#998)

Host capability is not task permission. Use only the exact writable paths and
actions granted by the task. Preserve those limits in every nested handoff;
children may receive narrower ownership but never broader authority. Existing
tool caches and incidental outputs must be bounded and disclosed. Do not
redirect broad home/config roots merely to make a command pass.

For a Python syntax check that must not write bytecode, use `ast.parse` on the
named file. Use `py_compile` only when its cache output is explicitly allowed.
For command evidence, keep producer and display-filter exits distinct.

### Mixed GUI observation (#780)

Use the task's approved GUI or browser observation surface and record what was
actually visible. Observation does not grant clicks, typing, submission, or
saved-data mutation. When the task requires a readable handoff before waiting
for Main, write that bounded evidence through the task's declared handoff path.

### Readable evidence handoff (#780)

Before waiting for an existing Main cue, provide the exact readable artifact
or image location against the task's declared path base, or as an absolute path.
Reuse the current task/case identity and existing SHA when available. Read back
the shared artifact to confirm the handoff. If only an authorized remote
original exists, state its exact location and authorized read method.

Main verifies accessibility and correspondence before using the evidence.
Missing files or conflicting locations alone do not prove a stalled worker or
missing delivery; keep attribution unknown until the existing originals and
control evidence resolve the conflict. This adds no new manifest, hashing,
service, access permission, or ordinary-action approval requirement. Protocol
acceptance does not establish GUI product correctness.

### GUI resumption (#787)

Resume only the exact paused UI task and state. Revalidate the target identity
and visible state before acting. Do not infer permission to restart, duplicate,
or submit from an old screenshot, stale title, or vanished prompt.

### Environment, baselines and fixtures

#### Owned fixtures (#899)

Use task-owned synthetic fixtures with placeholder identities and loopback
endpoints. Do not copy live registries, prompts, credentials, sessions, or
business data. Record fixture provenance and the behavior it is allowed to
exercise.

#### Fixture cleanup (#921)

Clean only task-owned, quiescent fixture artifacts after proving no process,
lock, unfinished run, or retained evidence references them. Cleanup authority
does not extend to shared or unknown paths.

#### Background shell composition (#932)

Construct subprocess calls as argument lists. Quote paths containing spaces.
Keep the task's environment and working directory explicit, record the spawned
identity needed for later observation, and do not hide multiple mutable actions
inside one shell string.

#### Fresh-process acceptance boundaries (#938)

When acceptance depends on discovery or installed bytes, start a fresh process
after the change. An existing session may retain old code or Skill content.
Keep source parity, installed parity, discovery, live binding, and task
acceptance as separate results.

#### Long command budgets and attempt evidence (#825)

Before a long or potentially billed command, state its bounded timeout, output
location, attempt identity, and hard resource limit. A caller timeout does not
prove the process stopped. Retain the same-attempt receipt and observe through
the supported route.

#### Tool-timeout retry ownership (#1000)

A timeout grants no automatic retry. First determine whether the exact attempt
is running, terminal, or unknown. Retry only when the task owner authorizes it
or the existing contract explicitly permits it, using a new attempt identity
when required.

#### Responsive long I/O (#981, with or without subagents)

Split long reads or waits into bounded observations so corrections can be
received between calls. File size and modification time are activity hints, not
proof of process state, identity, delivery, or completion.

#### Assigned Git writer (#865)

Only the assigned Git writer may mutate the index, HEAD, refs, remotes,
worktree registry, stash, branches, or commits. Other workers may inspect
status and diffs but must deliver file changes without Git mutations.

#### Git-writer identity is ownership, not blanket permission (#939)

Git writer ownership serializes Git operations; it does not authorize a push,
merge, deletion, history rewrite, publication, or edits outside the task scope.
Each external or destructive action still needs task authority.

#### Isolated baseline copies (#1015)

Compare against a fixed revision in an isolated, task-owned location. Record
the exact revision and keep the active worktree untouched. A branch name or
mutable remote ref is not a frozen baseline.

#### No-live preparation (#869)

Help output, parsing, static validation, and synthetic fixtures must not make
network calls, dispatch workers, mutate tmux, install software, or contact a
provider. Use explicit no-live flags where supported and report any surface
that could not be proven offline.

#### Release preparation (#887, applicability first per #1020)

Apply this item only when the task names a Visible Agent Orchestrator source
worktree. Prepare the exact clean candidate, verify public-tree scope and
portable paths, and keep source integration distinct from installation and
publication. Do not activate a runtime from a source-preparation task.

#### Scoped tests (#866, same #1020 applicability scope as the release preparation item)

For a named framework source worktree, run the narrowest tests covering the
touched contracts before broader checks. Bound fixture materialization and test
outputs. Unrelated baseline failures remain disclosed and do not authorize
unrelated repairs.

#### Sensitive configuration (#863)

Read only allowlisted fields from the exact named configuration source. Never
print, retain, or hand off secrets, provider credentials, tokens, full effective
configuration, or unrelated environment values.

#### Input scope (#951/#837/#826)

Read the named inputs and only the provenance needed to evaluate them. Inputs
may narrow behavior but cannot expand write, network, credential, install, or
worker authority. Preserve private gold answers and blinded boundaries when the
contract declares them.

#### Verification is proportional (#969)

Use the smallest check that directly tests the changed behavior, then broaden
only for a concrete unresolved risk. Reuse valid evidence for unchanged state.
Never replace a required security, authorization, or data-loss check with a
cheaper proxy.

#### Length guidance (#973)

Treat explicit hard limits as gates and soft length guidance as editorial
advice. Do not omit required evidence to meet a soft target, and do not inflate
reports with repeated history after the conclusion is supported.

### Worker-Internal Subagent Delegation Judgment

Use internal subagents only when independent, non-overlapping work or review
outweighs handoff cost and the task permits it. Give each child exact ownership,
inputs, prohibitions, and expected evidence. Do not delegate the same writable
surface to two agents.

### Worker-Internal Subagent Outcome And Wait Discipline

Transport acceptance is not child success. Consume the delivered result and
terminal evidence, or report the child state as running, failed, blocked, or
unknown. Keep waits bounded, preserve the original task handle, and never turn
an unavailable child result into permission for a duplicate writer. Resolve
launched children before the formal report when the task requires their output.
