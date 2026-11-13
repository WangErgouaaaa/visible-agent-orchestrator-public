from __future__ import annotations

import hashlib
import json
from typing import Any

from .opencode_runtime_proof_authorization import BACKEND_KIND
from .opencode_runtime_proof_execution_wrapper import (
    WRAPPER_KIND,
    validate_runtime_proof_execution_wrapper,
)


SCHEMA_VERSION = 1
POSTCHECK_KIND = "opencode-runtime-proof-postcheck-contract"
EXECUTION_MODE = "no-execution"
POSTCHECK_STATE = "review_only"
TERMINAL_MARKERS = [".done", ".fail", ".blocked"]


def runtime_proof_postcheck_contract_source_sha256(wrapper: dict[str, Any]) -> str:
    return _hash_json(wrapper)


def build_runtime_proof_postcheck_contract(
    *,
    wrapper: dict[str, Any],
    postcheck_contract_id: str,
    source_wrapper_path: str = "",
    target_worker_id: str = "",
    execution_receipt: dict[str, Any] | None = None,
    cleanup_receipt: dict[str, Any] | None = None,
    operator_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_wrapper = _dict(wrapper)
    wrapper_validation = validate_runtime_proof_execution_wrapper(source_wrapper)
    worker_id = target_worker_id or str(source_wrapper.get("target_worker_id", ""))
    execution = _dict(execution_receipt)
    cleanup = _dict(cleanup_receipt)
    operator = _dict(operator_report)

    contract = {
        "schema_version": SCHEMA_VERSION,
        "postcheck_kind": POSTCHECK_KIND,
        "postcheck_contract_id": str(postcheck_contract_id),
        "source_wrapper_id": str(source_wrapper.get("wrapper_id", "")),
        "source_wrapper_path": str(source_wrapper_path),
        "source_wrapper_sha256": runtime_proof_postcheck_contract_source_sha256(source_wrapper),
        "source_wrapper": source_wrapper,
        "source_wrapper_validation": wrapper_validation,
        "source_runner_plan_id": str(source_wrapper.get("source_runner_plan_id", "")),
        "source_runner_plan_sha256": str(source_wrapper.get("source_runner_plan_sha256", "")),
        "source_package_id": str(source_wrapper.get("source_package_id", "")),
        "source_package_sha256": str(source_wrapper.get("source_package_sha256", "")),
        "source_package_manifest_snapshot_sha256": str(
            source_wrapper.get("source_package_manifest_snapshot_sha256", "")
        ),
        "target_worker_id": worker_id,
        "dispatch_target_worker_id": worker_id,
        "backend_kind": BACKEND_KIND,
        "execution_mode": EXECUTION_MODE,
        "postcheck_state": POSTCHECK_STATE,
        "execution_receipt": execution,
        "cleanup_receipt": cleanup,
        "operator_report": operator,
        "artifact_path_bindings": _artifact_path_bindings(source_wrapper, execution, cleanup, operator),
        "terminal_truth": _terminal_truth(operator),
        "cleanup_verification": _cleanup_verification(cleanup),
        "write_scope_verification": _write_scope_verification(operator, cleanup),
        "allowed_write_roots": _list_str(source_wrapper.get("allowed_write_roots")),
        "forbidden_paths": _list_str(source_wrapper.get("forbidden_paths")),
        "would_execute_opencode": False,
        "would_execute_live": False,
        "would_touch_worker": False,
        "would_create_artifacts": False,
        "would_create_temp_root": False,
        "would_read_live_artifacts": False,
        "would_write_live_config": False,
        "would_write_task_local": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "would_write_cleanup_proof": False,
        "would_collect_runtime_evidence": False,
        "safe_next_actions": [
            "review supplied future receipt data without reading live artifacts",
            "reject missing cleanup proof, missing terminal truth, hash drift, retry evidence, or write-scope drift",
            "request separate explicit authorization before implementing any runtime executor",
        ],
        "forbidden_next_actions": [
            "do not execute OpenCode from this postcheck contract",
            "do not create temp roots from this postcheck contract",
            "do not run cleanup from this postcheck contract",
            "do not read live artifacts from this postcheck contract",
            "do not write cleanup proof, task-local protocol markers, registries, or .omx/state/team",
        ],
        "advisory": (
            "Source-only runtime-proof postcheck contract; it does not execute OpenCode, "
            "does not create temp roots, does not run cleanup, does not read live artifacts, "
            "and does not write cleanup proof. It validates supplied future receipt data only."
        ),
    }
    contract["validation"] = validate_runtime_proof_postcheck_contract(contract)
    contract["blocked_reasons"] = list(contract["validation"]["errors"])
    contract["warnings"] = list(contract["validation"]["warnings"])
    return contract


def validate_runtime_proof_postcheck_contract(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings = [
        "Runtime-proof postcheck contract validation is source-only/no-execution",
        "Validation uses supplied JSON only and does not read live artifacts, run cleanup, execute OpenCode, or touch workers",
    ]

    if payload.get("postcheck_kind") != POSTCHECK_KIND:
        errors.append(f"postcheck_kind must be {POSTCHECK_KIND}")
    if int(payload.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not str(payload.get("postcheck_contract_id", "")):
        errors.append("postcheck_contract_id missing")
    if payload.get("backend_kind") != BACKEND_KIND:
        errors.append(f"backend_kind must be {BACKEND_KIND}")
    if payload.get("execution_mode") != EXECUTION_MODE:
        errors.append(f"execution_mode must be {EXECUTION_MODE}")
    if payload.get("postcheck_state") != POSTCHECK_STATE:
        errors.append("postcheck_state must be review_only")

    for key in _false_flag_keys():
        if bool(payload.get(key, False)):
            errors.append(f"{key} must be false")

    source_wrapper = _dict(payload.get("source_wrapper"))
    if source_wrapper.get("wrapper_kind") != WRAPPER_KIND:
        errors.append(f"source wrapper_kind must be {WRAPPER_KIND}")
    expected_wrapper_hash = runtime_proof_postcheck_contract_source_sha256(source_wrapper)
    if str(payload.get("source_wrapper_sha256", "")) != expected_wrapper_hash:
        errors.append("source_wrapper_sha256 mismatch")
    if str(payload.get("source_wrapper_id", "")) != str(source_wrapper.get("wrapper_id", "")):
        errors.append("source_wrapper_id does not match source wrapper")
    if str(payload.get("source_runner_plan_id", "")) != str(source_wrapper.get("source_runner_plan_id", "")):
        errors.append("source_runner_plan_id does not match source wrapper")
    if str(payload.get("source_runner_plan_sha256", "")) != str(source_wrapper.get("source_runner_plan_sha256", "")):
        errors.append("source_runner_plan_sha256 mismatch")
    if str(payload.get("source_package_id", "")) != str(source_wrapper.get("source_package_id", "")):
        errors.append("source_package_id does not match source wrapper")
    if str(payload.get("source_package_sha256", "")) != str(source_wrapper.get("source_package_sha256", "")):
        errors.append("source_package_sha256 mismatch")
    if str(payload.get("source_package_manifest_snapshot_sha256", "")) != str(
        source_wrapper.get("source_package_manifest_snapshot_sha256", "")
    ):
        errors.append("source_package_manifest_snapshot_sha256 mismatch")

    wrapper_validation = validate_runtime_proof_execution_wrapper(source_wrapper)
    if not wrapper_validation["ok"]:
        errors.append("source wrapper validation failed")
        errors.extend(f"source wrapper: {item}" for item in _list_str(wrapper_validation.get("errors")))
    if not bool(_dict(payload.get("source_wrapper_validation")).get("ok", False)):
        errors.append("embedded source_wrapper_validation must be ok")

    target_worker_id = str(payload.get("target_worker_id", ""))
    if target_worker_id != str(source_wrapper.get("target_worker_id", "")):
        errors.append("target_worker_id does not match source wrapper")
    if str(payload.get("dispatch_target_worker_id", "")) != target_worker_id:
        errors.append("dispatch_target_worker_id must match target_worker_id")

    artifacts = _dict(source_wrapper.get("planned_artifacts"))
    wrapper_hash = expected_wrapper_hash
    execution = _dict(payload.get("execution_receipt"))
    cleanup = _dict(payload.get("cleanup_receipt"))
    operator = _dict(payload.get("operator_report"))

    _validate_execution_receipt(errors, execution, payload, source_wrapper, wrapper_hash, artifacts)
    _validate_cleanup_receipt(errors, cleanup, source_wrapper, wrapper_hash, artifacts)
    _validate_operator_report(errors, operator)

    return {
        "ok": not errors,
        "errors": _dedupe(errors),
        "warnings": warnings,
        "source_wrapper_hash": str(payload.get("source_wrapper_sha256", "")),
        "source_runner_plan_hash": str(payload.get("source_runner_plan_sha256", "")),
        "source_package_hash": str(payload.get("source_package_sha256", "")),
        "source_wrapper_validation_ok": wrapper_validation["ok"],
        "receipt_hash_binding_ok": _receipt_hash_binding_ok(execution, payload, wrapper_hash),
        "cleanup_proof_binding_ok": _cleanup_binding_ok(cleanup, source_wrapper, wrapper_hash, artifacts),
        "artifact_path_binding_ok": _artifact_path_binding_ok(execution, cleanup, artifacts),
        "terminal_truth_ok": _terminal_truth_ok(operator),
        "single_attempt_only": bool(execution.get("single_attempt_only")),
        "no_retry": bool(execution.get("no_retry")),
        "cleanup_completed": bool(cleanup.get("cleanup_completed")),
        "temp_root_only": cleanup.get("cleanup_scope") == "temp_root_only",
        "no_live_config_write": not bool(operator.get("live_config_artifacts_written")),
        "no_task_local_write": not bool(operator.get("task_local_artifacts_written")),
        "no_registry_write": not bool(operator.get("registry_artifacts_written")),
        "no_state_team_write": not bool(operator.get("state_team_artifacts_written")),
        "no_forbidden_path_writes": not bool(
            operator.get("forbidden_path_writes_detected") or cleanup.get("forbidden_path_writes_detected")
        ),
    }


def _validate_execution_receipt(
    errors: list[str],
    receipt: dict[str, Any],
    payload: dict[str, Any],
    wrapper: dict[str, Any],
    wrapper_hash: str,
    artifacts: dict[str, Any],
) -> None:
    if not receipt:
        errors.append("execution receipt missing")
        return
    if str(receipt.get("worker_id", "")) != str(payload.get("target_worker_id", "")):
        errors.append("execution receipt worker_id must match target_worker_id")
    if str(receipt.get("wrapper_id", "")) != str(payload.get("source_wrapper_id", "")):
        errors.append("execution receipt wrapper_id must match source wrapper")
    if str(receipt.get("wrapper_sha256", "")) != wrapper_hash:
        errors.append("execution receipt wrapper_sha256 mismatch")
    if str(receipt.get("runner_plan_sha256", "")) != str(wrapper.get("source_runner_plan_sha256", "")):
        errors.append("execution receipt runner_plan_sha256 mismatch")
    if str(receipt.get("package_sha256", "")) != str(wrapper.get("source_package_sha256", "")):
        errors.append("execution receipt package_sha256 mismatch")
    if str(receipt.get("manifest_snapshot_sha256", "")) != str(
        wrapper.get("source_package_manifest_snapshot_sha256", "")
    ):
        errors.append("execution receipt manifest_snapshot_sha256 mismatch")
    if _list_str(receipt.get("approved_argv")) != _list_str(_dict(wrapper.get("approved_invocation")).get("approved_argv")):
        errors.append("execution receipt approved_argv must match source wrapper")
    if int(receipt.get("attempt_count", 0) or 0) != 1:
        errors.append("attempt_count must be 1")
    if int(receipt.get("attempt_index", 0) or 0) != 1:
        errors.append("attempt_index must be 1")
    if not bool(receipt.get("single_attempt_only")):
        errors.append("execution receipt single_attempt_only must be true")
    if not bool(receipt.get("no_retry")):
        errors.append("execution receipt no_retry must be true")
    if str(receipt.get("cleanup_receipt_path", "")) != str(artifacts.get("cleanup_receipt_path", "")):
        errors.append("cleanup_receipt_path must match planned artifacts")
    if str(receipt.get("postcheck_marker_path", "")) != str(artifacts.get("postcheck_marker_path", "")):
        errors.append("postcheck_marker_path must match planned artifacts")


def _validate_cleanup_receipt(
    errors: list[str],
    receipt: dict[str, Any],
    wrapper: dict[str, Any],
    wrapper_hash: str,
    artifacts: dict[str, Any],
) -> None:
    if not receipt:
        errors.append("cleanup receipt missing")
        return
    if str(receipt.get("cleanup_receipt_path", "")) != str(artifacts.get("cleanup_receipt_path", "")):
        errors.append("cleanup_receipt_path must match planned artifacts")
    if str(receipt.get("postcheck_marker_path", "")) != str(artifacts.get("postcheck_marker_path", "")):
        errors.append("postcheck_marker_path must match planned artifacts")
    if str(receipt.get("temp_root", "")) != str(artifacts.get("temp_root", "")):
        errors.append("temp_root must match planned artifacts")
    if str(receipt.get("wrapper_id", "")) != str(wrapper.get("wrapper_id", "")):
        errors.append("cleanup receipt wrapper_id must match source wrapper")
    if str(receipt.get("wrapper_sha256", "")) != wrapper_hash:
        errors.append("cleanup receipt wrapper_sha256 mismatch")
    if str(receipt.get("worker_id", "")) != str(wrapper.get("target_worker_id", "")):
        errors.append("cleanup receipt worker_id must match source wrapper")
    if not bool(receipt.get("cleanup_required")):
        errors.append("cleanup_required must be true")
    if str(receipt.get("cleanup_scope", "")) != "temp_root_only":
        errors.append("cleanup_scope must be temp_root_only")
    if bool(receipt.get("leak_allowed")):
        errors.append("leak_allowed must be false")
    if not bool(receipt.get("cleanup_completed")):
        errors.append("cleanup_completed must be true")
    if _list(receipt.get("cleanup_leftover_paths")):
        errors.append("cleanup_leftover_paths must be empty")
    if not bool(receipt.get("temp_root_only_writes")):
        errors.append("temp_root_only_writes must be true")
    if bool(receipt.get("forbidden_path_writes_detected")):
        errors.append("forbidden_path_writes_detected must be false")


def _validate_operator_report(errors: list[str], report: dict[str, Any]) -> None:
    if not report:
        errors.append("operator report missing")
        return
    if not bool(report.get("report_md_nonempty")):
        errors.append("report_md_nonempty must be true")
    if int(report.get("terminal_marker_count", 0) or 0) != 1:
        errors.append("terminal_marker_count must be 1")
    terminal_markers = _list_str(report.get("terminal_markers"))
    if len(terminal_markers) != 1:
        errors.append("terminal_markers must contain exactly one marker")
    terminal_marker = str(report.get("terminal_marker", ""))
    if terminal_marker not in TERMINAL_MARKERS:
        errors.append("terminal_marker must be .done/.fail/.blocked")
    if terminal_markers and terminal_marker != terminal_markers[0]:
        errors.append("terminal_marker must match terminal_markers")
    if bool(report.get("terminal_marker_without_report")):
        errors.append("terminal_marker_without_report must be false")
    if bool(report.get("plugin_runtime_load_proven")):
        errors.append("plugin_runtime_load_proven must remain false in source-only postcheck contract")
    if bool(report.get("live_config_artifacts_written")):
        errors.append("live_config_artifacts_written must be false")
    if bool(report.get("task_local_artifacts_written")):
        errors.append("task_local_artifacts_written must be false")
    if bool(report.get("registry_artifacts_written")):
        errors.append("registry_artifacts_written must be false")
    if bool(report.get("state_team_artifacts_written")):
        errors.append("state_team_artifacts_written must be false")
    if bool(report.get("forbidden_path_writes_detected")):
        errors.append("forbidden_path_writes_detected must be false")
    if not bool(report.get("cleanup_completed")):
        errors.append("operator report cleanup_completed must be true")


def _artifact_path_bindings(
    wrapper: dict[str, Any],
    execution: dict[str, Any],
    cleanup: dict[str, Any],
    operator: dict[str, Any],
) -> dict[str, Any]:
    artifacts = _dict(wrapper.get("planned_artifacts"))
    return {
        "temp_root": str(artifacts.get("temp_root", "")),
        "cleanup_receipt_path": str(artifacts.get("cleanup_receipt_path", "")),
        "postcheck_marker_path": str(artifacts.get("postcheck_marker_path", "")),
        "raw_events_jsonl_path": str(artifacts.get("raw_events_jsonl_path", "")),
        "runtime_log_path": str(artifacts.get("runtime_log_path", "")),
        "execution_cleanup_receipt_path": str(execution.get("cleanup_receipt_path", "")),
        "cleanup_receipt_path_observed": str(cleanup.get("cleanup_receipt_path", "")),
        "operator_report_path": str(operator.get("report_path", "")),
    }


def _terminal_truth(operator: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_path": str(operator.get("report_path", "")),
        "report_md_nonempty": bool(operator.get("report_md_nonempty")),
        "terminal_marker": str(operator.get("terminal_marker", "")),
        "terminal_markers": _list_str(operator.get("terminal_markers")),
        "terminal_marker_count": int(operator.get("terminal_marker_count", 0) or 0),
        "terminal_marker_without_report": bool(operator.get("terminal_marker_without_report")),
        "allowed_terminal_marker_set": TERMINAL_MARKERS,
    }


def _cleanup_verification(cleanup: dict[str, Any]) -> dict[str, Any]:
    return {
        "cleanup_receipt_path": str(cleanup.get("cleanup_receipt_path", "")),
        "cleanup_required": bool(cleanup.get("cleanup_required")),
        "cleanup_scope": str(cleanup.get("cleanup_scope", "")),
        "leak_allowed": bool(cleanup.get("leak_allowed")),
        "cleanup_completed": bool(cleanup.get("cleanup_completed")),
        "cleanup_leftover_paths": _list_str(cleanup.get("cleanup_leftover_paths")),
        "temp_root_only_writes": bool(cleanup.get("temp_root_only_writes")),
        "forbidden_path_writes_detected": bool(cleanup.get("forbidden_path_writes_detected")),
    }


def _write_scope_verification(operator: dict[str, Any], cleanup: dict[str, Any]) -> dict[str, Any]:
    return {
        "no_live_config_write": not bool(operator.get("live_config_artifacts_written")),
        "no_task_local_write": not bool(operator.get("task_local_artifacts_written")),
        "no_registry_write": not bool(operator.get("registry_artifacts_written")),
        "no_state_team_write": not bool(operator.get("state_team_artifacts_written")),
        "no_forbidden_path_writes": not bool(
            operator.get("forbidden_path_writes_detected") or cleanup.get("forbidden_path_writes_detected")
        ),
    }


def _receipt_hash_binding_ok(receipt: dict[str, Any], payload: dict[str, Any], wrapper_hash: str) -> bool:
    return (
        str(receipt.get("wrapper_sha256", "")) == wrapper_hash
        and str(receipt.get("runner_plan_sha256", "")) == str(payload.get("source_runner_plan_sha256", ""))
        and str(receipt.get("package_sha256", "")) == str(payload.get("source_package_sha256", ""))
    )


def _cleanup_binding_ok(
    receipt: dict[str, Any],
    wrapper: dict[str, Any],
    wrapper_hash: str,
    artifacts: dict[str, Any],
) -> bool:
    return (
        str(receipt.get("wrapper_sha256", "")) == wrapper_hash
        and str(receipt.get("worker_id", "")) == str(wrapper.get("target_worker_id", ""))
        and str(receipt.get("cleanup_receipt_path", "")) == str(artifacts.get("cleanup_receipt_path", ""))
    )


def _artifact_path_binding_ok(execution: dict[str, Any], cleanup: dict[str, Any], artifacts: dict[str, Any]) -> bool:
    return (
        str(execution.get("cleanup_receipt_path", "")) == str(artifacts.get("cleanup_receipt_path", ""))
        and str(execution.get("postcheck_marker_path", "")) == str(artifacts.get("postcheck_marker_path", ""))
        and str(cleanup.get("cleanup_receipt_path", "")) == str(artifacts.get("cleanup_receipt_path", ""))
        and str(cleanup.get("temp_root", "")) == str(artifacts.get("temp_root", ""))
    )


def _terminal_truth_ok(operator: dict[str, Any]) -> bool:
    return (
        bool(operator.get("report_md_nonempty"))
        and int(operator.get("terminal_marker_count", 0) or 0) == 1
        and str(operator.get("terminal_marker", "")) in TERMINAL_MARKERS
        and _list_str(operator.get("terminal_markers")) == [str(operator.get("terminal_marker", ""))]
        and not bool(operator.get("terminal_marker_without_report"))
    )


def _false_flag_keys() -> list[str]:
    return [
        "would_execute_opencode",
        "would_execute_live",
        "would_touch_worker",
        "would_create_artifacts",
        "would_create_temp_root",
        "would_read_live_artifacts",
        "would_write_live_config",
        "would_write_task_local",
        "would_write_registry",
        "would_write_state_team",
        "would_write_cleanup_proof",
        "would_collect_runtime_evidence",
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
