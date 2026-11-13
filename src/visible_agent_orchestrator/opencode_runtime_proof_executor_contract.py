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
EXECUTOR_KIND = "opencode-runtime-proof-executor-contract"
EXECUTION_MODE = "no-execution"
EXECUTOR_STATE = "review_only"
TERMINAL_MARKERS = [".done", ".fail", ".blocked"]


def runtime_proof_executor_contract_source_sha256(wrapper: dict[str, Any]) -> str:
    return _hash_json(wrapper)


def build_runtime_proof_executor_contract(
    *,
    wrapper: dict[str, Any],
    executor_contract_id: str,
    source_wrapper_path: str = "",
    target_worker_id: str = "",
) -> dict[str, Any]:
    source_wrapper = _dict(wrapper)
    wrapper_validation = validate_runtime_proof_execution_wrapper(source_wrapper)
    worker_id = target_worker_id or str(source_wrapper.get("target_worker_id", ""))
    wrapper_hash = runtime_proof_executor_contract_source_sha256(source_wrapper)
    invocation = dict(_dict(source_wrapper.get("approved_invocation")))
    artifacts = dict(_dict(source_wrapper.get("planned_artifacts")))

    contract = {
        "schema_version": SCHEMA_VERSION,
        "executor_kind": EXECUTOR_KIND,
        "executor_contract_id": str(executor_contract_id),
        "source_wrapper_id": str(source_wrapper.get("wrapper_id", "")),
        "source_wrapper_path": str(source_wrapper_path),
        "source_wrapper_sha256": wrapper_hash,
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
        "executor_state": EXECUTOR_STATE,
        "approved_invocation_binding": invocation,
        "single_attempt_contract": _single_attempt_contract(invocation),
        "future_execution_receipt_template": _future_execution_receipt_template(
            source_wrapper,
            wrapper_hash,
            invocation,
            artifacts,
            worker_id,
        ),
        "future_cleanup_receipt_template": _future_cleanup_receipt_template(
            source_wrapper,
            wrapper_hash,
            artifacts,
            worker_id,
        ),
        "future_operator_report_template": _future_operator_report_template(artifacts),
        "artifact_path_bindings": _artifact_path_bindings(artifacts),
        "terminal_truth_template": _terminal_truth_template(artifacts),
        "cleanup_verification_template": _cleanup_verification_template(artifacts),
        "write_scope_verification_template": _write_scope_verification_template(),
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
            "review and validate this executor contract as source-only",
            "use the receipt templates as future executor output requirements only",
            "request separate explicit user authorization before any future runtime execution",
        ],
        "forbidden_next_actions": [
            "do not execute OpenCode from this executor contract",
            "do not create temp roots from this executor contract",
            "do not run cleanup from this executor contract",
            "do not read live artifacts from this executor contract",
            "do not write cleanup proof, task-local protocol markers, registries, or .omx/state/team",
        ],
        "advisory": (
            "Source-only runtime-proof executor contract; it does not execute OpenCode, "
            "does not create temp roots, does not run cleanup, does not read live artifacts, "
            "and does not write cleanup proof. Future execution still needs separate explicit user authorization."
        ),
    }
    contract["validation"] = validate_runtime_proof_executor_contract(contract)
    contract["blocked_reasons"] = list(contract["validation"]["errors"])
    contract["warnings"] = list(contract["validation"]["warnings"])
    return contract


def validate_runtime_proof_executor_contract(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings = [
        "Runtime-proof executor contract validation is source-only/no-execution",
        "Validation does not execute OpenCode, create temp roots, run cleanup, read live artifacts, or touch workers",
    ]

    if payload.get("executor_kind") != EXECUTOR_KIND:
        errors.append(f"executor_kind must be {EXECUTOR_KIND}")
    if int(payload.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not str(payload.get("executor_contract_id", "")):
        errors.append("executor_contract_id missing")
    if payload.get("backend_kind") != BACKEND_KIND:
        errors.append(f"backend_kind must be {BACKEND_KIND}")
    if payload.get("execution_mode") != EXECUTION_MODE:
        errors.append(f"execution_mode must be {EXECUTION_MODE}")
    if payload.get("executor_state") != EXECUTOR_STATE:
        errors.append("executor_state must be review_only")

    for key in _false_flag_keys():
        if bool(payload.get(key, False)):
            errors.append(f"{key} must be false")

    source_wrapper = _dict(payload.get("source_wrapper"))
    if source_wrapper.get("wrapper_kind") != WRAPPER_KIND:
        errors.append(f"source wrapper_kind must be {WRAPPER_KIND}")
    expected_wrapper_hash = runtime_proof_executor_contract_source_sha256(source_wrapper)
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

    invocation = _dict(source_wrapper.get("approved_invocation"))
    approved_binding = _dict(payload.get("approved_invocation_binding"))
    if approved_binding != invocation:
        errors.append("approved invocation must match source wrapper")
    if int(approved_binding.get("attempt_limit", 0) or 0) != 1:
        errors.append("approved invocation attempt_limit must be 1")
    if not bool(approved_binding.get("single_attempt_only")):
        errors.append("approved invocation single_attempt_only must be true")
    if not bool(approved_binding.get("no_retry")):
        errors.append("approved invocation no_retry must be true")

    single_attempt = _dict(payload.get("single_attempt_contract"))
    if int(single_attempt.get("attempt_limit", 0) or 0) != 1:
        errors.append("single_attempt_contract attempt_limit must be 1")
    if not bool(single_attempt.get("single_attempt_only")):
        errors.append("single_attempt_contract single_attempt_only must be true")
    if not bool(single_attempt.get("no_retry")):
        errors.append("single_attempt_contract no_retry must be true")

    artifacts = _dict(source_wrapper.get("planned_artifacts"))
    execution = _dict(payload.get("future_execution_receipt_template"))
    cleanup = _dict(payload.get("future_cleanup_receipt_template"))
    operator = _dict(payload.get("future_operator_report_template"))

    _validate_execution_receipt_template(errors, execution, payload, source_wrapper, expected_wrapper_hash, artifacts)
    _validate_cleanup_receipt_template(errors, cleanup, source_wrapper, expected_wrapper_hash, artifacts)
    _validate_operator_report_template(errors, operator, artifacts)

    if _list_str(payload.get("allowed_write_roots")) != _list_str(source_wrapper.get("allowed_write_roots")):
        errors.append("allowed_write_roots must match source wrapper")
    forbidden_paths = " ".join(_list_str(payload.get("forbidden_paths")))
    if "/.omx" not in forbidden_paths:
        errors.append("forbidden_paths must include the selected runtime .omx directory")

    return {
        "ok": not errors,
        "errors": _dedupe(errors),
        "warnings": warnings,
        "source_wrapper_hash": str(payload.get("source_wrapper_sha256", "")),
        "source_runner_plan_hash": str(payload.get("source_runner_plan_sha256", "")),
        "source_package_hash": str(payload.get("source_package_sha256", "")),
        "source_wrapper_validation_ok": wrapper_validation["ok"],
        "receipt_hash_binding_ok": _receipt_hash_binding_ok(execution, payload, expected_wrapper_hash),
        "artifact_path_binding_ok": _artifact_path_binding_ok(execution, cleanup, operator, artifacts),
        "single_attempt_only": bool(single_attempt.get("single_attempt_only")),
        "no_retry": bool(single_attempt.get("no_retry")),
        "cleanup_template_ok": _cleanup_template_ok(cleanup),
        "terminal_truth_template_ok": _terminal_truth_ok(operator),
        "no_live_config_write": not bool(operator.get("live_config_artifacts_written")),
        "no_task_local_write": not bool(operator.get("task_local_artifacts_written")),
        "no_registry_write": not bool(operator.get("registry_artifacts_written")),
        "no_state_team_write": not bool(operator.get("state_team_artifacts_written")),
        "no_forbidden_path_writes": not bool(
            operator.get("forbidden_path_writes_detected") or cleanup.get("forbidden_path_writes_detected")
        ),
    }


def _single_attempt_contract(invocation: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt_limit": int(invocation.get("attempt_limit", 0) or 0),
        "attempt_count": 1,
        "attempt_index": 1,
        "single_attempt_only": bool(invocation.get("single_attempt_only")),
        "no_retry": bool(invocation.get("no_retry")),
        "timebox_seconds": int(invocation.get("timebox_seconds", 0) or 0),
    }


def _future_execution_receipt_template(
    wrapper: dict[str, Any],
    wrapper_hash: str,
    invocation: dict[str, Any],
    artifacts: dict[str, Any],
    worker_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_kind": "opencode-runtime-proof-execution-receipt",
        "source": "opencode-runtime-proof-executor",
        "worker_id": worker_id,
        "run": "future-visible-run",
        "task": "task-001",
        "task_dir": _path_join(str(artifacts.get("temp_root", "")), "task-001"),
        "received_at": "future-runtime-execution",
        "wrapper_id": str(wrapper.get("wrapper_id", "")),
        "wrapper_sha256": wrapper_hash,
        "runner_plan_id": str(wrapper.get("source_runner_plan_id", "")),
        "runner_plan_sha256": str(wrapper.get("source_runner_plan_sha256", "")),
        "package_id": str(wrapper.get("source_package_id", "")),
        "package_sha256": str(wrapper.get("source_package_sha256", "")),
        "manifest_snapshot_sha256": str(wrapper.get("source_package_manifest_snapshot_sha256", "")),
        "approved_argv": _list_str(invocation.get("approved_argv")),
        "approved_env": dict(_dict(invocation.get("approved_env"))),
        "unset_env": _list_str(invocation.get("unset_env")),
        "attempt_count": 1,
        "attempt_index": 1,
        "single_attempt_only": bool(invocation.get("single_attempt_only")),
        "no_retry": bool(invocation.get("no_retry")),
        "timebox_seconds": int(invocation.get("timebox_seconds", 0) or 0),
        "postcheck_marker_path": str(artifacts.get("postcheck_marker_path", "")),
        "cleanup_receipt_path": str(artifacts.get("cleanup_receipt_path", "")),
        "report_path": _path_join(str(artifacts.get("temp_root", "")), "report.md"),
    }


def _future_cleanup_receipt_template(
    wrapper: dict[str, Any],
    wrapper_hash: str,
    artifacts: dict[str, Any],
    worker_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_kind": "opencode-runtime-proof-cleanup-receipt",
        "cleanup_receipt_path": str(artifacts.get("cleanup_receipt_path", "")),
        "postcheck_marker_path": str(artifacts.get("postcheck_marker_path", "")),
        "temp_root": str(artifacts.get("temp_root", "")),
        "wrapper_id": str(wrapper.get("wrapper_id", "")),
        "wrapper_sha256": wrapper_hash,
        "worker_id": worker_id,
        "run": "future-visible-run",
        "task": "task-001",
        "cleanup_required": True,
        "cleanup_scope": "temp_root_only",
        "leak_allowed": False,
        "cleanup_completed": True,
        "cleanup_leftover_paths": [],
        "temp_root_only_writes": True,
        "forbidden_path_writes_detected": False,
        "runtime_log_path": str(artifacts.get("runtime_log_path", "")),
        "raw_events_jsonl_path": str(artifacts.get("raw_events_jsonl_path", "")),
    }


def _future_operator_report_template(artifacts: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "report_path": _path_join(str(artifacts.get("temp_root", "")), "report.md"),
        "report_md_nonempty": True,
        "terminal_marker": ".done",
        "terminal_markers": [".done"],
        "terminal_marker_count": 1,
        "terminal_marker_path": _path_join(str(artifacts.get("temp_root", "")), ".done"),
        "terminal_marker_without_report": False,
        "receipt_state": "done",
        "plugin_runtime_load_proven": False,
        "temp_root_only_writes": True,
        "live_config_artifacts_written": False,
        "task_local_artifacts_written": False,
        "registry_artifacts_written": False,
        "state_team_artifacts_written": False,
        "forbidden_path_writes_detected": False,
        "cleanup_completed": True,
    }


def _artifact_path_bindings(artifacts: dict[str, Any]) -> dict[str, Any]:
    return {
        "temp_root": str(artifacts.get("temp_root", "")),
        "cleanup_receipt_path": str(artifacts.get("cleanup_receipt_path", "")),
        "postcheck_marker_path": str(artifacts.get("postcheck_marker_path", "")),
        "raw_events_jsonl_path": str(artifacts.get("raw_events_jsonl_path", "")),
        "runtime_log_path": str(artifacts.get("runtime_log_path", "")),
        "future_report_path": _path_join(str(artifacts.get("temp_root", "")), "report.md"),
        "future_terminal_marker_path": _path_join(str(artifacts.get("temp_root", "")), ".done"),
    }


def _terminal_truth_template(artifacts: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_path": _path_join(str(artifacts.get("temp_root", "")), "report.md"),
        "report_md_nonempty": True,
        "terminal_marker": ".done",
        "terminal_markers": [".done"],
        "terminal_marker_count": 1,
        "terminal_marker_without_report": False,
        "allowed_terminal_marker_set": TERMINAL_MARKERS,
    }


def _cleanup_verification_template(artifacts: dict[str, Any]) -> dict[str, Any]:
    return {
        "cleanup_receipt_path": str(artifacts.get("cleanup_receipt_path", "")),
        "cleanup_required": True,
        "cleanup_scope": "temp_root_only",
        "leak_allowed": False,
        "cleanup_completed": True,
        "cleanup_leftover_paths": [],
        "temp_root_only_writes": True,
        "forbidden_path_writes_detected": False,
    }


def _write_scope_verification_template() -> dict[str, Any]:
    return {
        "no_live_config_write": True,
        "no_task_local_write": True,
        "no_registry_write": True,
        "no_state_team_write": True,
        "no_forbidden_path_writes": True,
    }


def _validate_execution_receipt_template(
    errors: list[str],
    receipt: dict[str, Any],
    payload: dict[str, Any],
    wrapper: dict[str, Any],
    wrapper_hash: str,
    artifacts: dict[str, Any],
) -> None:
    if not receipt:
        errors.append("future execution receipt template missing")
        return
    if str(receipt.get("receipt_kind", "")) != "opencode-runtime-proof-execution-receipt":
        errors.append("future execution receipt receipt_kind must be opencode-runtime-proof-execution-receipt")
    if str(receipt.get("source", "")) != "opencode-runtime-proof-executor":
        errors.append("future execution receipt source must be opencode-runtime-proof-executor")
    if str(receipt.get("worker_id", "")) != str(payload.get("target_worker_id", "")):
        errors.append("future execution receipt worker_id must match target_worker_id")
    if str(receipt.get("wrapper_id", "")) != str(payload.get("source_wrapper_id", "")):
        errors.append("future execution receipt wrapper_id must match source wrapper")
    if str(receipt.get("wrapper_sha256", "")) != wrapper_hash:
        errors.append("future execution receipt wrapper_sha256 mismatch")
    if str(receipt.get("runner_plan_sha256", "")) != str(wrapper.get("source_runner_plan_sha256", "")):
        errors.append("future execution receipt runner_plan_sha256 mismatch")
    if str(receipt.get("package_sha256", "")) != str(wrapper.get("source_package_sha256", "")):
        errors.append("future execution receipt package_sha256 mismatch")
    if str(receipt.get("manifest_snapshot_sha256", "")) != str(
        wrapper.get("source_package_manifest_snapshot_sha256", "")
    ):
        errors.append("future execution receipt manifest_snapshot_sha256 mismatch")
    invocation = _dict(wrapper.get("approved_invocation"))
    if _list_str(receipt.get("approved_argv")) != _list_str(invocation.get("approved_argv")):
        errors.append("future execution receipt approved_argv must match source wrapper")
    if _dict(receipt.get("approved_env")) != _dict(invocation.get("approved_env")):
        errors.append("future execution receipt approved_env must match source wrapper")
    if _list_str(receipt.get("unset_env")) != _list_str(invocation.get("unset_env")):
        errors.append("future execution receipt unset_env must match source wrapper")
    if int(receipt.get("attempt_count", 0) or 0) != 1:
        errors.append("future execution receipt attempt_count must be 1")
    if int(receipt.get("attempt_index", 0) or 0) != 1:
        errors.append("future execution receipt attempt_index must be 1")
    if not bool(receipt.get("single_attempt_only")):
        errors.append("future execution receipt single_attempt_only must be true")
    if not bool(receipt.get("no_retry")):
        errors.append("future execution receipt no_retry must be true")
    if int(receipt.get("timebox_seconds", 0) or 0) != int(invocation.get("timebox_seconds", 0) or 0):
        errors.append("future execution receipt timebox_seconds must match source wrapper")
    if str(receipt.get("cleanup_receipt_path", "")) != str(artifacts.get("cleanup_receipt_path", "")):
        errors.append("cleanup_receipt_path must match planned artifacts")
    if str(receipt.get("postcheck_marker_path", "")) != str(artifacts.get("postcheck_marker_path", "")):
        errors.append("postcheck_marker_path must match planned artifacts")
    if str(receipt.get("report_path", "")) != _path_join(str(artifacts.get("temp_root", "")), "report.md"):
        errors.append("future execution receipt report_path must match planned artifacts")


def _validate_cleanup_receipt_template(
    errors: list[str],
    receipt: dict[str, Any],
    wrapper: dict[str, Any],
    wrapper_hash: str,
    artifacts: dict[str, Any],
) -> None:
    if not receipt:
        errors.append("future cleanup receipt template missing")
        return
    if str(receipt.get("receipt_kind", "")) != "opencode-runtime-proof-cleanup-receipt":
        errors.append("future cleanup receipt receipt_kind must be opencode-runtime-proof-cleanup-receipt")
    if str(receipt.get("cleanup_receipt_path", "")) != str(artifacts.get("cleanup_receipt_path", "")):
        errors.append("cleanup_receipt_path must match planned artifacts")
    if str(receipt.get("postcheck_marker_path", "")) != str(artifacts.get("postcheck_marker_path", "")):
        errors.append("postcheck_marker_path must match planned artifacts")
    if str(receipt.get("temp_root", "")) != str(artifacts.get("temp_root", "")):
        errors.append("temp_root must match planned artifacts")
    if str(receipt.get("wrapper_id", "")) != str(wrapper.get("wrapper_id", "")):
        errors.append("future cleanup receipt wrapper_id must match source wrapper")
    if str(receipt.get("wrapper_sha256", "")) != wrapper_hash:
        errors.append("future cleanup receipt wrapper_sha256 mismatch")
    if str(receipt.get("worker_id", "")) != str(wrapper.get("target_worker_id", "")):
        errors.append("future cleanup receipt worker_id must match source wrapper")
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
    if str(receipt.get("runtime_log_path", "")) != str(artifacts.get("runtime_log_path", "")):
        errors.append("runtime_log_path must match planned artifacts")
    if str(receipt.get("raw_events_jsonl_path", "")) != str(artifacts.get("raw_events_jsonl_path", "")):
        errors.append("raw_events_jsonl_path must match planned artifacts")


def _validate_operator_report_template(errors: list[str], report: dict[str, Any], artifacts: dict[str, Any]) -> None:
    if not report:
        errors.append("future operator report template missing")
        return
    if str(report.get("report_path", "")) != _path_join(str(artifacts.get("temp_root", "")), "report.md"):
        errors.append("future operator report report_path must match planned artifacts")
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
        errors.append("plugin_runtime_load_proven must remain false in source-only executor contract")
    if not bool(report.get("temp_root_only_writes")):
        errors.append("temp_root_only_writes must be true")
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


def _receipt_hash_binding_ok(receipt: dict[str, Any], payload: dict[str, Any], wrapper_hash: str) -> bool:
    return (
        str(receipt.get("wrapper_sha256", "")) == wrapper_hash
        and str(receipt.get("runner_plan_sha256", "")) == str(payload.get("source_runner_plan_sha256", ""))
        and str(receipt.get("package_sha256", "")) == str(payload.get("source_package_sha256", ""))
    )


def _artifact_path_binding_ok(
    execution: dict[str, Any],
    cleanup: dict[str, Any],
    operator: dict[str, Any],
    artifacts: dict[str, Any],
) -> bool:
    return (
        str(execution.get("cleanup_receipt_path", "")) == str(artifacts.get("cleanup_receipt_path", ""))
        and str(execution.get("postcheck_marker_path", "")) == str(artifacts.get("postcheck_marker_path", ""))
        and str(cleanup.get("cleanup_receipt_path", "")) == str(artifacts.get("cleanup_receipt_path", ""))
        and str(cleanup.get("temp_root", "")) == str(artifacts.get("temp_root", ""))
        and str(operator.get("report_path", "")) == _path_join(str(artifacts.get("temp_root", "")), "report.md")
    )


def _cleanup_template_ok(cleanup: dict[str, Any]) -> bool:
    return (
        bool(cleanup.get("cleanup_completed"))
        and str(cleanup.get("cleanup_scope", "")) == "temp_root_only"
        and not _list(cleanup.get("cleanup_leftover_paths"))
        and bool(cleanup.get("temp_root_only_writes"))
        and not bool(cleanup.get("forbidden_path_writes_detected"))
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


def _path_join(root: str, name: str) -> str:
    if not root:
        return name
    return f"{root.rstrip('/')}/{name}"


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
