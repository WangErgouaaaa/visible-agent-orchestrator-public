from __future__ import annotations

import hashlib
import json
from typing import Any

from .opencode_runtime_proof_authorization import (
    BACKEND_KIND,
    PACKAGE_KIND,
    validate_runtime_proof_authorization_package,
)


SCHEMA_VERSION = 1
PLAN_KIND = "opencode-runtime-proof-runner-plan"
EXECUTION_MODE = "no-execution"


def runtime_proof_runner_source_sha256(package: dict[str, Any]) -> str:
    return _hash_json(package)


def build_runtime_proof_runner_plan(
    *,
    package: dict[str, Any],
    runner_plan_id: str,
    source_package_path: str = "",
    target_worker_id: str = "",
) -> dict[str, Any]:
    source_package = _dict(package)
    approval = _dict(source_package.get("approval_record"))
    artifacts = _dict(source_package.get("disposable_artifacts"))
    postchecks = _dict(source_package.get("expected_post_checks"))
    package_worker_id = str(source_package.get("target_worker_id") or approval.get("target_worker_id") or "")
    worker_id = target_worker_id or package_worker_id
    source_validation = validate_runtime_proof_authorization_package(source_package)

    plan = {
        "schema_version": SCHEMA_VERSION,
        "plan_kind": PLAN_KIND,
        "runner_plan_id": runner_plan_id,
        "source_package_id": str(source_package.get("package_id", "")),
        "source_package_path": str(source_package_path),
        "source_package_sha256": runtime_proof_runner_source_sha256(source_package),
        "source_package_manifest_snapshot_sha256": str(source_package.get("manifest_snapshot_sha256", "")),
        "source_package": source_package,
        "source_package_validation": source_validation,
        "source_package_target_worker_id": package_worker_id,
        "target_worker_id": worker_id,
        "dispatch_target_worker_id": worker_id,
        "backend_kind": BACKEND_KIND,
        "execution_mode": EXECUTION_MODE,
        "approved_invocation": {
            "backend_kind": str(approval.get("backend_kind", "")),
            "target_worker_id": str(approval.get("target_worker_id", "")),
            "approved_argv": _list_str(approval.get("approved_argv")),
            "approved_env": dict(_dict(approval.get("approved_env"))),
            "unset_env": _list_str(approval.get("unset_env")),
            "approved_write_roots": _list_str(approval.get("approved_write_roots")),
            "timebox_seconds": int(approval.get("timebox_seconds", 0) or 0),
            "attempt_limit": int(approval.get("attempt_limit", 0) or 0),
            "single_attempt_only": bool(approval.get("single_attempt_only")),
            "no_retry": bool(approval.get("no_retry")),
            "required_stop_conditions": _list_str(approval.get("required_stop_conditions")),
        },
        "planned_artifacts": dict(artifacts),
        "allowed_write_roots": _list_str(artifacts.get("allowed_write_roots")) or _list_str(approval.get("approved_write_roots")),
        "preflight_gates": _preflight_gates(),
        "future_execution_order": _future_execution_order(),
        "validation_requirements": {
            "required_preflight_gates": _required_gates(),
            "required_future_steps": _required_steps(),
            "source_package_hash_binding": True,
            "source_package_manifest_snapshot_hash_binding": True,
            "single_attempt_only": True,
            "no_retry": True,
            "future_runtime_execution_authorization": "separate explicit user authorization required",
        },
        "postcheck_requirements": {
            **postchecks,
            "cleanup_proof_required": True,
            "report_md_plus_exactly_one_terminal_marker": True,
            "source_package_hash_must_match": True,
            "target_worker_must_match_approval": True,
            "no_retry_after_attempt": True,
        },
        "forbidden_paths": _list_str(source_package.get("forbidden_paths")) or _list_str(approval.get("forbidden_paths")),
        "would_execute_opencode": False,
        "would_execute_live": False,
        "would_touch_worker": False,
        "would_create_artifacts": False,
        "would_write_live_config": False,
        "would_write_task_local": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "safe_next_actions": [
            "review and validate this runner plan as source-only",
            "request separate explicit user authorization before any future runtime execution",
            "run future proof at most once through the approved wrapper and target worker only",
        ],
        "forbidden_next_actions": [
            "do not execute OpenCode from this plan",
            "do not start TUI/headless/serve runtime from this plan",
            "do not load plugins from this plan",
            "do not use raw tmux or direct /tui submit paths",
            "do not write live OpenCode config, registries, task-local markers, or .omx/state/team",
        ],
        "advisory": (
            "Source-only runtime-proof runner plan; it does not execute OpenCode, "
            "does not create artifacts, and does not grant runtime execution. "
            "Future execution still needs separate explicit user authorization."
        ),
    }
    plan["validation"] = validate_runtime_proof_runner_plan(plan)
    plan["blocked_reasons"] = list(plan["validation"]["errors"])
    plan["warnings"] = list(plan["validation"]["warnings"])
    return plan


def validate_runtime_proof_runner_plan(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings = [
        "Runtime-proof runner plan validation is source-only/no-execution",
        "Validation does not execute OpenCode, start TUI/headless/serve runtime, load plugins, or write artifacts",
    ]

    if payload.get("plan_kind") != PLAN_KIND:
        errors.append(f"plan_kind must be {PLAN_KIND}")
    if int(payload.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not str(payload.get("runner_plan_id", "")):
        errors.append("runner_plan_id missing")
    if not str(payload.get("source_package_id", "")):
        errors.append("source_package_id missing")
    if not str(payload.get("target_worker_id", "")):
        errors.append("target_worker_id missing")
    if payload.get("backend_kind") != BACKEND_KIND:
        errors.append(f"backend_kind must be {BACKEND_KIND}")
    if payload.get("execution_mode") != EXECUTION_MODE:
        errors.append(f"execution_mode must be {EXECUTION_MODE}")

    for key in (
        "would_execute_opencode",
        "would_execute_live",
        "would_touch_worker",
        "would_create_artifacts",
        "would_write_live_config",
        "would_write_task_local",
        "would_write_registry",
        "would_write_state_team",
    ):
        if bool(payload.get(key, False)):
            errors.append(f"{key} must be false")

    source_package = _dict(payload.get("source_package"))
    if source_package.get("package_kind") != PACKAGE_KIND:
        errors.append(f"source package_kind must be {PACKAGE_KIND}")
    expected_hash = runtime_proof_runner_source_sha256(source_package)
    if str(payload.get("source_package_sha256", "")) != expected_hash:
        errors.append("source_package_sha256 mismatch")
    if str(payload.get("source_package_id", "")) != str(source_package.get("package_id", "")):
        errors.append("source_package_id does not match source package")
    if str(payload.get("source_package_manifest_snapshot_sha256", "")) != str(source_package.get("manifest_snapshot_sha256", "")):
        errors.append("source_package_manifest_snapshot_sha256 mismatch")

    source_validation = validate_runtime_proof_authorization_package(source_package)
    if not source_validation["ok"]:
        errors.append("source package validation failed")
        errors.extend(f"source package: {item}" for item in _list_str(source_validation.get("errors")))

    approval = _dict(source_package.get("approval_record"))
    invocation = _dict(payload.get("approved_invocation"))
    approval_worker_id = str(approval.get("target_worker_id", ""))
    target_worker_id = str(payload.get("target_worker_id", ""))
    if target_worker_id != approval_worker_id:
        errors.append("target_worker_id does not match source approval record")
    if str(invocation.get("backend_kind", "")) != BACKEND_KIND:
        errors.append(f"approved invocation backend_kind must be {BACKEND_KIND}")
    if _list_str(invocation.get("approved_argv")) != _list_str(approval.get("approved_argv")):
        errors.append("approved invocation argv must match source approval record")
    if _dict(invocation.get("approved_env")) != _dict(approval.get("approved_env")):
        errors.append("approved invocation env must match source approval record")
    if _list_str(invocation.get("unset_env")) != _list_str(approval.get("unset_env")):
        errors.append("approved invocation unset_env must match source approval record")
    if int(invocation.get("attempt_limit", 0) or 0) != 1:
        errors.append("approved invocation attempt_limit must be 1")
    if not bool(invocation.get("single_attempt_only")):
        errors.append("approved invocation single_attempt_only must be true")
    if not bool(invocation.get("no_retry")):
        errors.append("approved invocation no_retry must be true")

    artifacts = _dict(payload.get("planned_artifacts"))
    if not str(artifacts.get("temp_root", "")):
        errors.append("planned_artifacts.temp_root missing")
    if not bool(artifacts.get("cleanup_required")):
        errors.append("planned_artifacts cleanup_required must be true")
    if bool(artifacts.get("leak_allowed")):
        errors.append("planned_artifacts leak_allowed must be false")
    if bool(artifacts.get("task_local_marker_write_allowed")):
        errors.append("planned_artifacts task_local_marker_write_allowed must be false")

    gate_names = {str(_dict(item).get("gate", "")) for item in _list(payload.get("preflight_gates"))}
    for gate in _required_gates():
        if gate not in gate_names:
            errors.append(f"preflight gate missing: {gate}")

    step_names = {str(_dict(item).get("step", "")) for item in _list(payload.get("future_execution_order"))}
    for step in _required_steps():
        if step not in step_names:
            errors.append(f"future execution step missing: {step}")

    postchecks = _dict(payload.get("postcheck_requirements"))
    if not bool(postchecks.get("cleanup_proof_required")):
        errors.append("cleanup_proof_required must be true")
    if not bool(postchecks.get("report_md_plus_exactly_one_terminal_marker")):
        errors.append("report_md_plus_exactly_one_terminal_marker must be true")

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
        "no_live_config_write": not bool(payload.get("would_write_live_config", False)),
        "no_task_local_write": not bool(payload.get("would_write_task_local", False)),
        "no_registry_write": not bool(payload.get("would_write_registry", False)),
        "no_state_team_write": not bool(payload.get("would_write_state_team", False)),
        "source_package_hash": str(payload.get("source_package_sha256", "")),
    }


def _preflight_gates() -> list[dict[str, Any]]:
    descriptions = {
        "source_package_validation_ok": "authorization package must validate before future execution",
        "source_package_hash_matches": "embedded authorization package hash must match the plan binding",
        "target_worker_matches_approval": "target worker must match the approval record",
        "single_attempt_only": "attempt_limit is exactly 1 and no_retry remains true",
        "disposable_root_path_guard": "future writes are restricted to the disposable temp root",
        "no_live_config_write": "live OpenCode config writes are forbidden",
        "no_task_local_write": "task-local protocol marker writes are forbidden",
        "no_registry_write": "registry updates are forbidden",
        "no_state_team_write": ".omx/state/team writes are forbidden",
        "cleanup_required": "future execution must write cleanup proof for disposable artifacts",
    }
    return [
        {"gate": gate, "required": True, "fail_closed": True, "description": descriptions[gate]}
        for gate in _required_gates()
    ]


def _future_execution_order() -> list[dict[str, Any]]:
    descriptions = {
        "validate_authorization_package": "validate package and source hash before doing anything else",
        "prepare_disposable_root": "create only the approved temp root after separate execution authorization",
        "write_isolated_config": "write isolated OpenCode config under XDG_CONFIG_HOME only",
        "run_exact_approved_argv_once": "run exactly one approved argv attempt against the target worker",
        "capture_raw_events_and_runtime_log": "capture raw events and runtime log inside the disposable root",
        "run_post_checks": "verify no forbidden writes and no interactive prompt/runtime drift",
        "cleanup_disposable_root": "remove or account for disposable artifacts",
        "write_cleanup_proof_and_operator_report": "write cleanup proof and report.md plus exactly one terminal marker",
    }
    return [
        {"step": step, "future_only": True, "description": descriptions[step]}
        for step in _required_steps()
    ]


def _required_gates() -> list[str]:
    return [
        "source_package_validation_ok",
        "source_package_hash_matches",
        "target_worker_matches_approval",
        "single_attempt_only",
        "disposable_root_path_guard",
        "no_live_config_write",
        "no_task_local_write",
        "no_registry_write",
        "no_state_team_write",
        "cleanup_required",
    ]


def _required_steps() -> list[str]:
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
