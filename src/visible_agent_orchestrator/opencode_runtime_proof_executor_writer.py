from __future__ import annotations

import hashlib
import json
from typing import Any

from .opencode_runtime_proof_authorization import BACKEND_KIND
from .opencode_runtime_proof_executor_contract import (
    EXECUTOR_KIND,
    TERMINAL_MARKERS,
    validate_runtime_proof_executor_contract,
)


SCHEMA_VERSION = 1
WRITER_KIND = "opencode-runtime-proof-executor-writer-contract"
WRITER_MODE = "no-execution"
WRITER_STATE = "review_only"


def runtime_proof_executor_writer_source_sha256(executor_contract: dict[str, Any]) -> str:
    return _hash_json(executor_contract)


def build_runtime_proof_executor_writer_contract(
    *,
    executor_contract: dict[str, Any],
    writer_contract_id: str,
    source_executor_contract_path: str = "",
    target_worker_id: str = "",
) -> dict[str, Any]:
    source_executor = _dict(executor_contract)
    executor_validation = validate_runtime_proof_executor_contract(source_executor)
    worker_id = target_worker_id or str(source_executor.get("target_worker_id", ""))
    executor_hash = runtime_proof_executor_writer_source_sha256(source_executor)
    single_attempt = _dict(source_executor.get("single_attempt_contract"))
    cleanup_template = _dict(source_executor.get("future_cleanup_receipt_template"))
    operator_template = _dict(source_executor.get("future_operator_report_template"))
    artifacts = _dict(source_executor.get("artifact_path_bindings"))

    contract = {
        "schema_version": SCHEMA_VERSION,
        "writer_kind": WRITER_KIND,
        "writer_contract_id": str(writer_contract_id),
        "source_executor_contract_id": str(source_executor.get("executor_contract_id", "")),
        "source_executor_contract_path": str(source_executor_contract_path),
        "source_executor_contract_sha256": executor_hash,
        "source_executor_contract": source_executor,
        "source_executor_contract_validation": executor_validation,
        "source_wrapper_id": str(source_executor.get("source_wrapper_id", "")),
        "source_wrapper_sha256": str(source_executor.get("source_wrapper_sha256", "")),
        "source_runner_plan_id": str(source_executor.get("source_runner_plan_id", "")),
        "source_runner_plan_sha256": str(source_executor.get("source_runner_plan_sha256", "")),
        "source_package_id": str(source_executor.get("source_package_id", "")),
        "source_package_sha256": str(source_executor.get("source_package_sha256", "")),
        "target_worker_id": worker_id,
        "dispatch_target_worker_id": worker_id,
        "backend_kind": BACKEND_KIND,
        "writer_mode": WRITER_MODE,
        "writer_state": WRITER_STATE,
        "single_attempt_writer_contract": _single_attempt_writer_contract(single_attempt),
        "cleanup_proof_writer_contract": _cleanup_proof_writer_contract(cleanup_template, artifacts),
        "failure_timeout_contract": _failure_timeout_contract(),
        "terminal_truth_writer_contract": _terminal_truth_writer_contract(operator_template),
        "future_writer_receipt_template": _future_writer_receipt_template(
            writer_contract_id=str(writer_contract_id),
            executor_contract=source_executor,
            executor_hash=executor_hash,
            worker_id=worker_id,
            single_attempt=single_attempt,
            cleanup_template=cleanup_template,
            operator_template=operator_template,
        ),
        "allowed_write_roots": _list_str(source_executor.get("allowed_write_roots")),
        "forbidden_paths": _list_str(source_executor.get("forbidden_paths")),
        "would_execute_opencode": False,
        "would_execute_live": False,
        "would_touch_worker": False,
        "would_create_temp_root": False,
        "would_run_cleanup": False,
        "would_read_live_artifacts": False,
        "would_write_live_config": False,
        "would_write_cleanup_proof": False,
        "would_write_task_local": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "would_collect_runtime_evidence": False,
        "would_touch_tmux": False,
        "would_call_tui": False,
        "safe_next_actions": [
            "review and validate this executor-writer contract as source-only",
            "use this contract as the future cleanup-proof writer acceptance shape only",
            "request separate explicit user authorization before any future OpenCode runtime/plugin proof",
        ],
        "forbidden_next_actions": [
            "do not execute OpenCode from this executor-writer contract",
            "do not create temp roots or run cleanup from this executor-writer contract",
            "do not write cleanup proof, task-local markers, registries, or .omx/state/team",
            "do not touch tmux directly or call /tui",
            "do not claim plugin import, hook invocation, native subagent lifecycle, or runtime proof",
        ],
        "advisory": (
            "Source-only runtime-proof executor-writer contract; it does not execute OpenCode, "
            "does not create temp roots, does not run cleanup, and does not write cleanup proof. "
            "Future runtime/plugin proof still needs separate explicit authorization."
        ),
    }
    contract["validation"] = validate_runtime_proof_executor_writer_contract(contract)
    contract["blocked_reasons"] = list(contract["validation"]["errors"])
    contract["warnings"] = list(contract["validation"]["warnings"])
    return contract


def validate_runtime_proof_executor_writer_contract(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings = [
        "Runtime-proof executor-writer validation is source-only/no-execution",
        "Validation does not execute OpenCode, create temp roots, run cleanup, write cleanup proof, or touch workers",
    ]

    if payload.get("writer_kind") != WRITER_KIND:
        errors.append(f"writer_kind must be {WRITER_KIND}")
    if int(payload.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not str(payload.get("writer_contract_id", "")):
        errors.append("writer_contract_id missing")
    if payload.get("backend_kind") != BACKEND_KIND:
        errors.append(f"backend_kind must be {BACKEND_KIND}")
    if payload.get("writer_mode") != WRITER_MODE:
        errors.append(f"writer_mode must be {WRITER_MODE}")
    if payload.get("writer_state") != WRITER_STATE:
        errors.append("writer_state must be review_only")

    for key in _false_flag_keys():
        if bool(payload.get(key, False)):
            errors.append(f"{key} must be false")

    source_executor = _dict(payload.get("source_executor_contract"))
    if source_executor.get("executor_kind") != EXECUTOR_KIND:
        errors.append(f"source executor_kind must be {EXECUTOR_KIND}")
    expected_executor_hash = runtime_proof_executor_writer_source_sha256(source_executor)
    if str(payload.get("source_executor_contract_sha256", "")) != expected_executor_hash:
        errors.append("source_executor_contract_sha256 mismatch")
    if str(payload.get("source_executor_contract_id", "")) != str(source_executor.get("executor_contract_id", "")):
        errors.append("source_executor_contract_id does not match source executor contract")
    if str(payload.get("source_wrapper_id", "")) != str(source_executor.get("source_wrapper_id", "")):
        errors.append("source_wrapper_id does not match source executor contract")
    if str(payload.get("source_wrapper_sha256", "")) != str(source_executor.get("source_wrapper_sha256", "")):
        errors.append("source_wrapper_sha256 mismatch")

    executor_validation = validate_runtime_proof_executor_contract(source_executor)
    if not executor_validation["ok"]:
        errors.append("source executor contract validation failed")
        errors.extend(f"source executor contract: {item}" for item in _list_str(executor_validation.get("errors")))
    if not bool(_dict(payload.get("source_executor_contract_validation")).get("ok", False)):
        errors.append("embedded source_executor_contract_validation must be ok")

    target_worker_id = str(payload.get("target_worker_id", ""))
    if target_worker_id != str(source_executor.get("target_worker_id", "")):
        errors.append("target_worker_id does not match source executor contract")
    if str(payload.get("dispatch_target_worker_id", "")) != target_worker_id:
        errors.append("dispatch_target_worker_id must match target_worker_id")

    single_attempt = _dict(payload.get("single_attempt_writer_contract"))
    if int(single_attempt.get("attempt_limit", 0) or 0) != 1:
        errors.append("single_attempt_writer_contract attempt_limit must be 1")
    if int(single_attempt.get("attempt_count", 0) or 0) != 1:
        errors.append("single_attempt_writer_contract attempt_count must be 1")
    if int(single_attempt.get("attempt_index", 0) or 0) != 1:
        errors.append("single_attempt_writer_contract attempt_index must be 1")
    if not bool(single_attempt.get("single_attempt_only")):
        errors.append("single_attempt_writer_contract single_attempt_only must be true")
    if not bool(single_attempt.get("no_retry")):
        errors.append("single_attempt_writer_contract no_retry must be true")

    cleanup = _dict(payload.get("cleanup_proof_writer_contract"))
    source_cleanup = _dict(source_executor.get("future_cleanup_receipt_template"))
    _validate_cleanup_proof_contract(errors, cleanup, source_cleanup)

    failure_timeout = _dict(payload.get("failure_timeout_contract"))
    if not bool(failure_timeout.get("write_cleanup_proof_on_timeout")):
        errors.append("write_cleanup_proof_on_timeout must be true")
    if not bool(failure_timeout.get("write_operator_report_on_failure")):
        errors.append("write_operator_report_on_failure must be true")
    if not bool(failure_timeout.get("no_retry_after_failure")):
        errors.append("no_retry_after_failure must be true")

    terminal = _dict(payload.get("terminal_truth_writer_contract"))
    _validate_terminal_truth_contract(errors, terminal)

    writer_receipt = _dict(payload.get("future_writer_receipt_template"))
    _validate_future_writer_receipt(errors, writer_receipt, payload, source_executor)

    if _list_str(payload.get("allowed_write_roots")) != _list_str(source_executor.get("allowed_write_roots")):
        errors.append("allowed_write_roots must match source executor contract")
    forbidden_paths = " ".join(_list_str(payload.get("forbidden_paths")))
    if "/.omx" not in forbidden_paths:
        errors.append("forbidden_paths must include the selected runtime .omx directory")

    return {
        "ok": not errors,
        "errors": _dedupe(errors),
        "warnings": warnings,
        "source_executor_contract_hash": str(payload.get("source_executor_contract_sha256", "")),
        "source_executor_contract_validation_ok": executor_validation["ok"],
        "receipt_hash_binding_ok": _receipt_hash_binding_ok(writer_receipt, payload),
        "single_attempt_only": bool(single_attempt.get("single_attempt_only")),
        "no_retry": bool(single_attempt.get("no_retry")),
        "cleanup_scope_temp_root_only": str(cleanup.get("cleanup_scope", "")) == "temp_root_only",
        "cleanup_leftovers_forbidden": bool(cleanup.get("cleanup_leftover_paths_must_be_empty")),
        "terminal_truth_template_ok": int(terminal.get("terminal_marker_count", 0) or 0) == 1,
        "no_runtime_execution": not bool(payload.get("would_execute_opencode", False)),
        "no_cleanup_proof_write": not bool(payload.get("would_write_cleanup_proof", False)),
        "no_task_local_write": not bool(payload.get("would_write_task_local", False)),
        "no_registry_write": not bool(payload.get("would_write_registry", False)),
        "no_state_team_write": not bool(payload.get("would_write_state_team", False)),
    }


def _single_attempt_writer_contract(single_attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt_limit": int(single_attempt.get("attempt_limit", 0) or 0),
        "attempt_count": 1,
        "attempt_index": 1,
        "single_attempt_only": bool(single_attempt.get("single_attempt_only")),
        "no_retry": bool(single_attempt.get("no_retry")),
        "timebox_seconds": int(single_attempt.get("timebox_seconds", 0) or 0),
    }


def _cleanup_proof_writer_contract(cleanup_template: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    cleanup_receipt_path = str(cleanup_template.get("cleanup_receipt_path", "")) or str(
        artifacts.get("cleanup_receipt_path", "")
    )
    return {
        "cleanup_receipt_path": cleanup_receipt_path,
        "postcheck_marker_path": str(cleanup_template.get("postcheck_marker_path", "")),
        "temp_root": str(cleanup_template.get("temp_root", "")) or str(artifacts.get("temp_root", "")),
        "cleanup_required": True,
        "cleanup_scope": "temp_root_only",
        "cleanup_leftover_paths_must_be_empty": True,
        "temp_root_only_writes_required": True,
        "forbidden_path_writes_detected_must_be_false": True,
        "atomic_write_required": True,
        "write_after_execution_attempt": True,
        "proof_written_only_after_attempt": True,
    }


def _failure_timeout_contract() -> dict[str, Any]:
    return {
        "covered_terminal_conditions": ["success", "failure", "timeout", "blocked"],
        "write_cleanup_proof_on_timeout": True,
        "write_operator_report_on_failure": True,
        "no_retry_after_failure": True,
        "terminal_marker_required": True,
    }


def _terminal_truth_writer_contract(operator_template: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_path": str(operator_template.get("report_path", "")),
        "report_md_nonempty": True,
        "terminal_marker": str(operator_template.get("terminal_marker", ".done")),
        "terminal_markers": _list_str(operator_template.get("terminal_markers")) or [".done"],
        "terminal_marker_count": 1,
        "terminal_marker_without_report": False,
        "allowed_terminal_marker_set": list(TERMINAL_MARKERS),
    }


def _future_writer_receipt_template(
    *,
    writer_contract_id: str,
    executor_contract: dict[str, Any],
    executor_hash: str,
    worker_id: str,
    single_attempt: dict[str, Any],
    cleanup_template: dict[str, Any],
    operator_template: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_kind": "opencode-runtime-proof-writer-receipt",
        "source": "opencode-runtime-proof-executor-writer",
        "writer_contract_id": writer_contract_id,
        "executor_contract_id": str(executor_contract.get("executor_contract_id", "")),
        "executor_contract_sha256": executor_hash,
        "worker_id": worker_id,
        "attempt_count": 1,
        "attempt_index": 1,
        "single_attempt_only": bool(single_attempt.get("single_attempt_only")),
        "no_retry": bool(single_attempt.get("no_retry")),
        "cleanup_receipt_path": str(cleanup_template.get("cleanup_receipt_path", "")),
        "report_path": str(operator_template.get("report_path", "")),
        "terminal_marker_count": 1,
        "cleanup_scope": "temp_root_only",
        "cleanup_leftover_paths": [],
        "forbidden_path_writes_detected": False,
    }


def _validate_cleanup_proof_contract(
    errors: list[str],
    cleanup: dict[str, Any],
    source_cleanup: dict[str, Any],
) -> None:
    if str(cleanup.get("cleanup_receipt_path", "")) != str(source_cleanup.get("cleanup_receipt_path", "")):
        errors.append("cleanup_receipt_path must match source executor contract")
    if str(cleanup.get("postcheck_marker_path", "")) != str(source_cleanup.get("postcheck_marker_path", "")):
        errors.append("postcheck_marker_path must match source executor contract")
    if str(cleanup.get("temp_root", "")) != str(source_cleanup.get("temp_root", "")):
        errors.append("temp_root must match source executor contract")
    if not bool(cleanup.get("cleanup_required")):
        errors.append("cleanup_required must be true")
    if str(cleanup.get("cleanup_scope", "")) != "temp_root_only":
        errors.append("cleanup_scope must be temp_root_only")
    if not bool(cleanup.get("cleanup_leftover_paths_must_be_empty")):
        errors.append("cleanup_leftover_paths_must_be_empty must be true")
    if not bool(cleanup.get("temp_root_only_writes_required")):
        errors.append("temp_root_only_writes_required must be true")
    if not bool(cleanup.get("forbidden_path_writes_detected_must_be_false")):
        errors.append("forbidden_path_writes_detected_must_be_false must be true")
    if not bool(cleanup.get("atomic_write_required")):
        errors.append("atomic_write_required must be true")
    if not bool(cleanup.get("write_after_execution_attempt")):
        errors.append("write_after_execution_attempt must be true")


def _validate_terminal_truth_contract(errors: list[str], terminal: dict[str, Any]) -> None:
    if not bool(terminal.get("report_md_nonempty")):
        errors.append("report_md_nonempty must be true")
    if int(terminal.get("terminal_marker_count", 0) or 0) != 1:
        errors.append("terminal_marker_count must be 1")
    markers = _list_str(terminal.get("terminal_markers"))
    if len(markers) != 1:
        errors.append("terminal_markers must contain exactly one marker")
    marker = str(terminal.get("terminal_marker", ""))
    if marker not in TERMINAL_MARKERS:
        errors.append("terminal_marker must be .done/.fail/.blocked")
    if markers and marker != markers[0]:
        errors.append("terminal_marker must match terminal_markers")
    if _list_str(terminal.get("allowed_terminal_marker_set")) != TERMINAL_MARKERS:
        errors.append("allowed_terminal_marker_set must be .done/.fail/.blocked")
    if bool(terminal.get("terminal_marker_without_report")):
        errors.append("terminal_marker_without_report must be false")


def _validate_future_writer_receipt(
    errors: list[str],
    receipt: dict[str, Any],
    payload: dict[str, Any],
    source_executor: dict[str, Any],
) -> None:
    if str(receipt.get("receipt_kind", "")) != "opencode-runtime-proof-writer-receipt":
        errors.append("future writer receipt receipt_kind must be opencode-runtime-proof-writer-receipt")
    if str(receipt.get("source", "")) != "opencode-runtime-proof-executor-writer":
        errors.append("future writer receipt source must be opencode-runtime-proof-executor-writer")
    if str(receipt.get("writer_contract_id", "")) != str(payload.get("writer_contract_id", "")):
        errors.append("future writer receipt writer_contract_id must match writer_contract_id")
    if str(receipt.get("executor_contract_id", "")) != str(source_executor.get("executor_contract_id", "")):
        errors.append("future writer receipt executor_contract_id must match source executor contract")
    if str(receipt.get("executor_contract_sha256", "")) != str(payload.get("source_executor_contract_sha256", "")):
        errors.append("future writer receipt executor_contract_sha256 mismatch")
    if str(receipt.get("worker_id", "")) != str(payload.get("target_worker_id", "")):
        errors.append("future writer receipt worker_id must match target_worker_id")
    if int(receipt.get("attempt_count", 0) or 0) != 1:
        errors.append("future writer receipt attempt_count must be 1")
    if int(receipt.get("attempt_index", 0) or 0) != 1:
        errors.append("future writer receipt attempt_index must be 1")
    if not bool(receipt.get("single_attempt_only")):
        errors.append("future writer receipt single_attempt_only must be true")
    if not bool(receipt.get("no_retry")):
        errors.append("future writer receipt no_retry must be true")
    source_cleanup = _dict(source_executor.get("future_cleanup_receipt_template"))
    if str(receipt.get("cleanup_receipt_path", "")) != str(source_cleanup.get("cleanup_receipt_path", "")):
        errors.append("future writer receipt cleanup_receipt_path must match source executor contract")
    source_operator = _dict(source_executor.get("future_operator_report_template"))
    if str(receipt.get("report_path", "")) != str(source_operator.get("report_path", "")):
        errors.append("future writer receipt report_path must match source executor contract")
    if str(receipt.get("cleanup_scope", "")) != "temp_root_only":
        errors.append("future writer receipt cleanup_scope must be temp_root_only")
    if _list(receipt.get("cleanup_leftover_paths")):
        errors.append("future writer receipt cleanup_leftover_paths must be empty")
    if bool(receipt.get("forbidden_path_writes_detected")):
        errors.append("future writer receipt forbidden_path_writes_detected must be false")


def _receipt_hash_binding_ok(receipt: dict[str, Any], payload: dict[str, Any]) -> bool:
    return str(receipt.get("executor_contract_sha256", "")) == str(payload.get("source_executor_contract_sha256", ""))


def _false_flag_keys() -> list[str]:
    return [
        "would_execute_opencode",
        "would_execute_live",
        "would_touch_worker",
        "would_create_temp_root",
        "would_run_cleanup",
        "would_read_live_artifacts",
        "would_write_live_config",
        "would_write_cleanup_proof",
        "would_write_task_local",
        "would_write_registry",
        "would_write_state_team",
        "would_collect_runtime_evidence",
        "would_touch_tmux",
        "would_call_tui",
    ]


def _hash_json(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _list_str(value: Any) -> list[str]:
    return [str(item) for item in _list(value)]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
