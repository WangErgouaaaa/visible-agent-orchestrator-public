from __future__ import annotations

import hashlib
import json
from typing import Any

from .opencode_runtime_proof_authorization import BACKEND_KIND
from .opencode_runtime_proof_runner_plan import (
    PLAN_KIND as RUNNER_PLAN_KIND,
    validate_runtime_proof_runner_plan,
)


SCHEMA_VERSION = 1
WRAPPER_KIND = "opencode-runtime-proof-execution-wrapper"
EXECUTION_MODE = "no-execution"
EXECUTION_STATE = "pending"


def runtime_proof_execution_wrapper_source_sha256(runner_plan: dict[str, Any]) -> str:
    return _hash_json(runner_plan)


def build_runtime_proof_execution_wrapper(
    *,
    runner_plan: dict[str, Any],
    wrapper_id: str,
    source_runner_plan_path: str = "",
    target_worker_id: str = "",
) -> dict[str, Any]:
    source_runner_plan = _dict(runner_plan)
    runner_validation = validate_runtime_proof_runner_plan(source_runner_plan)
    worker_id = target_worker_id or str(source_runner_plan.get("target_worker_id", ""))
    approved_invocation = dict(_dict(source_runner_plan.get("approved_invocation")))
    planned_artifacts = dict(_dict(source_runner_plan.get("planned_artifacts")))
    postcheck_requirements = dict(_dict(source_runner_plan.get("postcheck_requirements")))

    wrapper = {
        "schema_version": SCHEMA_VERSION,
        "wrapper_kind": WRAPPER_KIND,
        "wrapper_id": str(wrapper_id),
        "source_runner_plan_id": str(source_runner_plan.get("runner_plan_id", "")),
        "source_runner_plan_path": str(source_runner_plan_path),
        "source_runner_plan_sha256": runtime_proof_execution_wrapper_source_sha256(source_runner_plan),
        "source_runner_plan": source_runner_plan,
        "source_runner_plan_validation": runner_validation,
        "source_package_id": str(source_runner_plan.get("source_package_id", "")),
        "source_package_sha256": str(source_runner_plan.get("source_package_sha256", "")),
        "source_package_manifest_snapshot_sha256": str(source_runner_plan.get("source_package_manifest_snapshot_sha256", "")),
        "target_worker_id": worker_id,
        "dispatch_target_worker_id": worker_id,
        "backend_kind": BACKEND_KIND,
        "execution_mode": EXECUTION_MODE,
        "execution_state": EXECUTION_STATE,
        "approved_invocation": approved_invocation,
        "planned_sequence": _planned_sequence(source_runner_plan),
        "planned_artifacts": planned_artifacts,
        "cleanup_receipt_schema_hint": _cleanup_receipt_schema_hint(planned_artifacts),
        "postcheck_report_schema_hint": _postcheck_report_schema_hint(planned_artifacts, postcheck_requirements),
        "operator_report_schema_hint": _operator_report_schema_hint(postcheck_requirements),
        "allowed_write_roots": _list_str(source_runner_plan.get("allowed_write_roots")),
        "forbidden_paths": _list_str(source_runner_plan.get("forbidden_paths")),
        "would_execute_opencode": False,
        "would_execute_live": False,
        "would_touch_worker": False,
        "would_create_artifacts": False,
        "would_create_temp_root": False,
        "would_write_live_config": False,
        "would_write_task_local": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "would_write_cleanup_proof": False,
        "would_collect_runtime_evidence": False,
        "safe_next_actions": [
            "review and validate this execution wrapper plan as source-only",
            "request separate explicit user authorization before any future runtime execution",
            "keep cleanup proof, post-check proof, and terminal truth as future evidence requirements only",
        ],
        "forbidden_next_actions": [
            "do not execute OpenCode from this wrapper plan",
            "do not create disposable temp roots from this wrapper plan",
            "do not start TUI/headless/serve/session runtime from this wrapper plan",
            "do not load plugins from this wrapper plan",
            "do not call tmux or direct /tui paths from this wrapper plan",
            "do not write cleanup proof, task-local protocol markers, registries, or .omx/state/team",
            "do not collect evidence from a live run from this wrapper plan",
        ],
        "advisory": (
            "Source-only runtime-proof execution wrapper; it does not execute OpenCode, "
            "does not create disposable temp roots, does not write cleanup proof, "
            "and does not collect evidence from a live run. Future execution still "
            "needs separate explicit user authorization."
        ),
    }
    wrapper["validation"] = validate_runtime_proof_execution_wrapper(wrapper)
    wrapper["blocked_reasons"] = list(wrapper["validation"]["errors"])
    wrapper["warnings"] = list(wrapper["validation"]["warnings"])
    return wrapper


def validate_runtime_proof_execution_wrapper(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings = [
        "Runtime-proof execution wrapper validation is source-only/no-execution",
        "Validation does not execute OpenCode, create temp roots, write cleanup proof, collect evidence, or touch workers",
    ]

    if payload.get("wrapper_kind") != WRAPPER_KIND:
        errors.append(f"wrapper_kind must be {WRAPPER_KIND}")
    if int(payload.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not str(payload.get("wrapper_id", "")):
        errors.append("wrapper_id missing")
    if payload.get("backend_kind") != BACKEND_KIND:
        errors.append(f"backend_kind must be {BACKEND_KIND}")
    if payload.get("execution_mode") != EXECUTION_MODE:
        errors.append(f"execution_mode must be {EXECUTION_MODE}")
    if payload.get("execution_state") != EXECUTION_STATE:
        errors.append("execution_state must be pending")

    for key in _false_flag_keys():
        if bool(payload.get(key, False)):
            errors.append(f"{key} must be false")

    source_runner_plan = _dict(payload.get("source_runner_plan"))
    if source_runner_plan.get("plan_kind") != RUNNER_PLAN_KIND:
        errors.append(f"source runner plan_kind must be {RUNNER_PLAN_KIND}")
    expected_hash = runtime_proof_execution_wrapper_source_sha256(source_runner_plan)
    if str(payload.get("source_runner_plan_sha256", "")) != expected_hash:
        errors.append("source_runner_plan_sha256 mismatch")
    if str(payload.get("source_runner_plan_id", "")) != str(source_runner_plan.get("runner_plan_id", "")):
        errors.append("source_runner_plan_id does not match source runner plan")
    if str(payload.get("source_package_id", "")) != str(source_runner_plan.get("source_package_id", "")):
        errors.append("source_package_id does not match source runner plan")
    if str(payload.get("source_package_sha256", "")) != str(source_runner_plan.get("source_package_sha256", "")):
        errors.append("source_package_sha256 mismatch")
    if str(payload.get("source_package_manifest_snapshot_sha256", "")) != str(
        source_runner_plan.get("source_package_manifest_snapshot_sha256", "")
    ):
        errors.append("source_package_manifest_snapshot_sha256 mismatch")

    runner_validation = validate_runtime_proof_runner_plan(source_runner_plan)
    if not runner_validation["ok"]:
        errors.append("source runner plan validation failed")
        errors.extend(f"source runner plan: {item}" for item in _list_str(runner_validation.get("errors")))

    target_worker_id = str(payload.get("target_worker_id", ""))
    if target_worker_id != str(source_runner_plan.get("target_worker_id", "")):
        errors.append("target_worker_id does not match source runner plan")
    if str(payload.get("dispatch_target_worker_id", "")) != target_worker_id:
        errors.append("dispatch_target_worker_id must match target_worker_id")

    approved_invocation = _dict(payload.get("approved_invocation"))
    if approved_invocation != _dict(source_runner_plan.get("approved_invocation")):
        errors.append("approved invocation must match source runner plan")
    if int(approved_invocation.get("attempt_limit", 0) or 0) != 1:
        errors.append("approved invocation attempt_limit must be 1")
    if not bool(approved_invocation.get("single_attempt_only")):
        errors.append("approved invocation single_attempt_only must be true")
    if not bool(approved_invocation.get("no_retry")):
        errors.append("approved invocation no_retry must be true")

    if _dict(payload.get("planned_artifacts")) != _dict(source_runner_plan.get("planned_artifacts")):
        errors.append("planned_artifacts must match source runner plan")
    if _list_str(payload.get("allowed_write_roots")) != _list_str(source_runner_plan.get("allowed_write_roots")):
        errors.append("allowed_write_roots must match source runner plan")

    sequence_steps = {str(_dict(item).get("step", "")) for item in _list(payload.get("planned_sequence"))}
    for step in _runner_steps(source_runner_plan):
        if step not in sequence_steps:
            errors.append(f"planned sequence step missing: {step}")

    artifacts = _dict(payload.get("planned_artifacts"))
    cleanup_hint = _dict(payload.get("cleanup_receipt_schema_hint"))
    if cleanup_hint.get("cleanup_receipt_path") != artifacts.get("cleanup_receipt_path"):
        errors.append("cleanup_receipt_path must match planned artifacts")
    if not bool(cleanup_hint.get("cleanup_required")):
        errors.append("cleanup_required must be true")
    if cleanup_hint.get("cleanup_scope") != "temp_root_only":
        errors.append("cleanup_scope must be temp_root_only")
    if bool(cleanup_hint.get("leak_allowed")):
        errors.append("leak_allowed must be false")

    postcheck_hint = _dict(payload.get("postcheck_report_schema_hint"))
    if postcheck_hint.get("postcheck_marker_path") != artifacts.get("postcheck_marker_path"):
        errors.append("postcheck_marker_path must match planned artifacts")
    expected_postchecks = _dict(postcheck_hint.get("expected_post_checks"))
    if bool(expected_postchecks.get("plugin_runtime_load_proven", True)):
        errors.append("plugin_runtime_load_proven must remain false before execution")
    if not bool(expected_postchecks.get("temp_root_only_writes")):
        errors.append("temp_root_only_writes must be true")

    operator_hint = _dict(payload.get("operator_report_schema_hint"))
    if not bool(operator_hint.get("report_md_plus_exactly_one_terminal_marker")):
        errors.append("report_md_plus_exactly_one_terminal_marker must be true")
    if _list_str(operator_hint.get("terminal_marker_set")) != [".done", ".fail", ".blocked"]:
        errors.append("terminal_marker_set must be .done/.fail/.blocked")

    forbidden_paths = " ".join(_list_str(payload.get("forbidden_paths")))
    if "/.omx" not in forbidden_paths:
        errors.append("forbidden_paths must include the selected runtime .omx directory")

    return {
        "ok": not errors,
        "errors": _dedupe(errors),
        "warnings": warnings,
        "no_execution": not bool(payload.get("would_execute_opencode", False)),
        "no_live_execution": not bool(payload.get("would_execute_live", False)),
        "no_worker_touch": not bool(payload.get("would_touch_worker", False)),
        "no_artifacts": not bool(payload.get("would_create_artifacts", False)),
        "no_temp_root_creation": not bool(payload.get("would_create_temp_root", False)),
        "no_live_config_write": not bool(payload.get("would_write_live_config", False)),
        "no_task_local_write": not bool(payload.get("would_write_task_local", False)),
        "no_registry_write": not bool(payload.get("would_write_registry", False)),
        "no_state_team_write": not bool(payload.get("would_write_state_team", False)),
        "no_cleanup_proof_write": not bool(payload.get("would_write_cleanup_proof", False)),
        "no_runtime_evidence_collection": not bool(payload.get("would_collect_runtime_evidence", False)),
        "source_runner_plan_hash": str(payload.get("source_runner_plan_sha256", "")),
        "source_package_hash": str(payload.get("source_package_sha256", "")),
        "source_runner_plan_validation_ok": runner_validation["ok"] if "runner_validation" in locals() else False,
        "approved_invocation_matches_runner_plan": approved_invocation == _dict(source_runner_plan.get("approved_invocation")),
        "planned_artifacts_match_runner_plan": _dict(payload.get("planned_artifacts")) == _dict(source_runner_plan.get("planned_artifacts")),
        "single_attempt_only": bool(approved_invocation.get("single_attempt_only")),
        "no_retry": bool(approved_invocation.get("no_retry")),
    }


def _planned_sequence(runner_plan: dict[str, Any]) -> list[dict[str, Any]]:
    existing = [_dict(item) for item in _list(runner_plan.get("future_execution_order"))]
    if existing:
        return [
            {
                "step": str(item.get("step", "")),
                "future_only": True,
                "description": str(item.get("description", "")),
                "source": "source_runner_plan.future_execution_order",
            }
            for item in existing
            if str(item.get("step", ""))
        ]
    return [
        {
            "step": step,
            "future_only": True,
            "description": "future wrapper step copied from runtime-proof runner-plan contract",
            "source": "runtime_proof_execution_wrapper.required_steps",
        }
        for step in _runner_steps(runner_plan)
    ]


def _cleanup_receipt_schema_hint(artifacts: dict[str, Any]) -> dict[str, Any]:
    return {
        "cleanup_receipt_path": str(artifacts.get("cleanup_receipt_path", "")),
        "temp_root": str(artifacts.get("temp_root", "")),
        "cleanup_required": bool(artifacts.get("cleanup_required")),
        "cleanup_scope": str(artifacts.get("cleanup_scope", "")),
        "leak_allowed": bool(artifacts.get("leak_allowed")),
        "required_fields": [
            "cleanup_completed",
            "cleanup_leftover_paths",
            "temp_root_only_writes",
            "forbidden_path_writes_detected",
        ],
    }


def _postcheck_report_schema_hint(artifacts: dict[str, Any], postchecks: dict[str, Any]) -> dict[str, Any]:
    return {
        "postcheck_marker_path": str(artifacts.get("postcheck_marker_path", "")),
        "expected_post_checks": dict(postchecks),
        "must_match": [
            "temp_root_only_writes",
            "live_config_artifacts_written",
            "task_local_artifacts_written",
            "forbidden_path_writes_detected",
            "plugin_runtime_load_proven",
            "cleanup_completed",
        ],
    }


def _operator_report_schema_hint(postchecks: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_md_plus_exactly_one_terminal_marker": bool(
            postchecks.get("report_md_plus_exactly_one_terminal_marker")
        ),
        "terminal_marker_set": [".done", ".fail", ".blocked"],
        "non_empty_report_md_required": True,
        "multiple_terminal_markers_forbidden": True,
        "terminal_marker_without_report_forbidden": True,
    }


def _runner_steps(runner_plan: dict[str, Any]) -> list[str]:
    steps = [
        str(_dict(item).get("step", ""))
        for item in _list(runner_plan.get("future_execution_order"))
        if str(_dict(item).get("step", ""))
    ]
    if steps:
        return steps
    return [
        "validate_authorization_package",
        "prepare_disposable_root",
        "write_isolated_config",
        "run_exact_approved_argv_once",
        "capture_raw_events_and_runtime_log",
        "run_post_checks",
        "cleanup_disposable_root",
        "write_cleanup_proof_and_operator_report",
    ]


def _false_flag_keys() -> list[str]:
    return [
        "would_execute_opencode",
        "would_execute_live",
        "would_touch_worker",
        "would_create_artifacts",
        "would_create_temp_root",
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
