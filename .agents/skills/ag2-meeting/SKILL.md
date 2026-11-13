---
name: ag2-meeting
description: Run a deterministic offline AG2-style idea or code-design self-review; live model mode requires explicit per-run approval and host-local configuration.
---

# AG2 Meeting

Use this Skill for structured proposer, skeptic and synthesizer review of an
idea or code design.

## Default offline path

Resolve the selected runtime root and run the profile wrapper from the target
workspace:

```bash
"$VAO_ROOT/bin/omx_ag2_meeting" \
  --root "$PWD" \
  --profile code-design-review \
  --task "Review this design" \
  --json
```


For workspace-scoped meeting artifacts, omitted `--root` uses `VAO_WORKSPACE_ROOT` and then `~/vao-workspace`. An explicit `--root`, including `.`, takes precedence and stays independent of `VAO_ROOT`.

Public profiles:

- `idea-review`
- `code-design-review`

The wrapper is deterministic and mock-only. It does not call a model, tmux,
the visible-worker dispatcher, or TUI APIs. Verify these workspace artifacts:

- `meeting-result.json`
- `meeting-synthesis.json`
- `meeting-audit.json`
- `meeting-board-provenance.json`
- `transcript.md`
- `meeting-request-audit.json` when using `--profile`

## Optional live path

Real model execution is a separate lower-level smoke path. Use it only when the
user explicitly authorizes that individual run and identifies both a host-local
effective-approval JSON file and a host-local runtime-config JSON file. Check
that the paths exist; do not read, copy, quote or summarize their contents.

The live command uses `omx_meeting_plan ag2-run --runtime real
--real-runtime-mode live --allow-real-model-call` plus the two host-local JSON
paths. Never infer approval from a prior run, a profile, or file existence.

## Output

Report:

- surface: offline structured self-review or authorized real AG2 runtime;
- profile/preset and workspace root;
- artifacts produced;
- whether model calls, tmux and visible dispatch were skipped;
- any incomplete or missing artifact.

## Hard boundaries

- Do not create, modify, copy, sync or print provider configuration,
  credentials, approval JSON or runtime JSON.
- Do not store provider, model, endpoint or credential values in synced files.
- Do not install Python dependencies or patch upstream AG2/autogen.
- Do not call tmux, visible dispatch, raw TUI APIs or live workers from the
  offline profile wrapper.
- Do not write worker terminal markers, locks or registries.
- Do not claim the offline self-review is a multi-worker meeting or a live model
  evaluation.
