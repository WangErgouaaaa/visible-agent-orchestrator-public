from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visible_agent_orchestrator.operation_authorization import (
    OperationAuthorizationError,
    build_trusted_operation_receipt,
    derive_operation_contract,
    validate_mutation_gate_union,
    validate_operation_capability,
    validate_trusted_operation_receipt,
)


def worker_dispatch_capability() -> dict[str, object]:
    contract = derive_operation_contract("worker_dispatch")
    return {
        "schema_version": 1,
        "contract_id": "contract-worker-1",
        "contract_hash": "sha256:" + "1" * 64,
        "authorization_id": "grant-worker-1",
        "authorization_source": "authorization_ledger",
        "operation_kind": "worker_dispatch",
        "scope": {"kind": "worker", "id": "vw-worker-1"},
        "mutation_gates": contract["required_mutation_gates"],
        "task_id": "task-001",
        "attempt_id": "attempt-001",
    }


def test_unknown_operation_and_effect_fail_closed() -> None:
    with pytest.raises(OperationAuthorizationError, match="unknown operation"):
        derive_operation_contract("unlisted_operation")
    with pytest.raises(OperationAuthorizationError, match="unknown effect"):
        derive_operation_contract(
            "worker_dispatch",
            {"operation_constraints": {"known_effects": ["unlisted_effect"]}},
        )


def test_required_mutation_gate_union_cannot_be_lowered() -> None:
    required = derive_operation_contract("worker_dispatch")["required_mutation_gates"]
    with pytest.raises(OperationAuthorizationError, match="mutation gate not enforced"):
        validate_mutation_gate_union(required, required[:-1])
    validate_mutation_gate_union(required, required)


def test_capability_requires_exact_scope_and_task_attempt() -> None:
    contract = derive_operation_contract("worker_dispatch")
    capability = worker_dispatch_capability()
    accepted = validate_operation_capability(
        capability,
        operation_kind="worker_dispatch",
        target_scope={"kind": "worker", "id": "vw-worker-1"},
        required_mutation_gates=contract["required_mutation_gates"],
        task_id="task-001",
        attempt_id="attempt-001",
    )
    assert accepted["authorization_id"] == "grant-worker-1"

    with pytest.raises(OperationAuthorizationError, match="scope mismatch"):
        validate_operation_capability(
            capability,
            operation_kind="worker_dispatch",
            target_scope={"kind": "worker", "id": "vw-worker-2"},
            required_mutation_gates=contract["required_mutation_gates"],
            task_id="task-001",
            attempt_id="attempt-001",
        )
    with pytest.raises(OperationAuthorizationError, match="task-attempt mismatch"):
        validate_operation_capability(
            capability,
            operation_kind="worker_dispatch",
            target_scope={"kind": "worker", "id": "vw-worker-1"},
            required_mutation_gates=contract["required_mutation_gates"],
            task_id="task-002",
            attempt_id="attempt-001",
        )


def test_trusted_receipt_binds_operation_and_observer() -> None:
    receipt = build_trusted_operation_receipt(
        authorization_id="grant-worker-1",
        operation_kind="worker_dispatch",
        resolved_target={"kind": "worker", "id": "vw-worker-1"},
        before_state={"submitted": False},
        after_state={"submitted": True},
        outcome="applied",
        task_id="task-001",
        attempt_id="attempt-001",
        ledger_sequence=1,
        created_at="2026-01-01T00:00:00Z",
    )

    accepted = validate_trusted_operation_receipt(
        receipt,
        authorization_id="grant-worker-1",
        operation_kind="worker_dispatch",
        resolved_target={"kind": "worker", "id": "vw-worker-1"},
        task_id="task-001",
        attempt_id="attempt-001",
        ledger_sequence=1,
    )
    assert accepted["outcome"] == "applied"

    with pytest.raises(OperationAuthorizationError, match="trusted observer"):
        validate_trusted_operation_receipt(
            {
                **receipt,
                "trusted_observer": {"kind": "wrapper", "id": "untrusted-name"},
            },
            authorization_id="grant-worker-1",
            operation_kind="worker_dispatch",
            resolved_target={"kind": "worker", "id": "vw-worker-1"},
            task_id="task-001",
            attempt_id="attempt-001",
            ledger_sequence=1,
        )
