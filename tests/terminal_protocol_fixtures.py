from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.terminal_acceptance import build_terminal_output_claim
from visible_agent_orchestrator.terminal_protocol import (
    build_legacy_terminal_baseline,
    creation_record_path,
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def write_modern_task_records(
    task_dir: Path,
    *,
    run_id: str,
    task_id: str,
    worker_id: str,
    attempt_id: str,
    contract_hash: str,
    session_id: str,
    instance_id: str,
    binding_generation: int,
    fencing_generation: int,
    state: str = "",
) -> None:
    identity = {
        "task_protocol_version": 2,
        "terminal_protocol_version": 2,
        "terminal_acceptance_required": True,
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "contract_hash": contract_hash,
        "worker_id": worker_id,
        "session_id": session_id,
        "instance_id": instance_id,
        "binding_generation": binding_generation,
        "fencing_generation": fencing_generation,
    }
    fence = {
        "schema_version": 1,
        "fence_kind": "visible_terminal_output_acceptance",
        **identity,
        "session_fence_state": "live_observed",
    }
    _write_json(
        task_dir / "task.json",
        {
            "schema_version": 1,
            "id": task_id,
            "run": run_id,
            "owner": worker_id,
            "backend": "opencode_tui",
            **identity,
            "acceptance_fence": fence,
        },
    )
    _write_json(
        task_dir / "dispatch.json",
        {
            "schema_version": 1,
            "task": task_id,
            "run": run_id,
            "worker": worker_id,
            "backend": "opencode_tui",
            **identity,
            "acceptance_fence": fence,
        },
    )
    _write_json(task_dir / "acceptance_fence.json", fence)
    _write_json(
        creation_record_path(task_dir),
        {
            "schema_version": 1,
            "record_kind": "terminal_protocol_creation",
            **identity,
        },
    )
    if not state:
        return
    claim = build_terminal_output_claim(
        fence,
        identity,
        state=state,
        created_at="2026-01-01T00:00:00Z",
    )
    claim.update(
        {
            "run_id": run_id,
            "live_session_observation": {
                "source": "synthetic_fixture",
                "expected_session_id": session_id,
                "session_ids": [session_id],
                "matches": True,
            },
        }
    )
    _write_json(task_dir / "terminal_output_claim.json", claim)


def write_legacy_baseline(task_dir: Path) -> None:
    run_root = task_dir.parent.parent
    _write_json(
        run_root / "legacy_terminal_baseline.json",
        build_legacy_terminal_baseline(run_root),
    )
