from __future__ import annotations

import hashlib
import json
from typing import Any

from .opencode_runtime_proof_authorization import BACKEND_KIND
from .opencode_runtime_proof_postcheck_contract import (
    POSTCHECK_KIND,
    TERMINAL_MARKERS,
    validate_runtime_proof_postcheck_contract,
)


SCHEMA_VERSION = 1
EVIDENCE_POSTCHECK_KIND = "opencode-runtime-proof-evidence-postcheck"
RAW_EVENT_EVIDENCE_KIND = "opencode-runtime-proof-raw-event-evidence"
EVIDENCE_MODE = "supplied-json-only"
EVIDENCE_STATE = "review_only"


def runtime_proof_evidence_postcheck_source_sha256(postcheck_contract: dict[str, Any]) -> str:
    return _hash_json(postcheck_contract)


def build_runtime_proof_evidence_postcheck(
    *,
    postcheck_contract: dict[str, Any],
    evidence_postcheck_id: str,
    source_postcheck_contract_path: str = "",
    target_worker_id: str = "",
    raw_event_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_postcheck = _dict(postcheck_contract)
    postcheck_validation = validate_runtime_proof_postcheck_contract(source_postcheck)
    worker_id = target_worker_id or str(source_postcheck.get("target_worker_id", ""))
    raw_evidence = _dict(raw_event_evidence)

    evidence_postcheck = {
        "schema_version": SCHEMA_VERSION,
        "evidence_postcheck_kind": EVIDENCE_POSTCHECK_KIND,
        "evidence_postcheck_id": str(evidence_postcheck_id),
        "source_postcheck_contract_id": str(source_postcheck.get("postcheck_contract_id", "")),
        "source_postcheck_contract_path": str(source_postcheck_contract_path),
        "source_postcheck_contract_sha256": runtime_proof_evidence_postcheck_source_sha256(source_postcheck),
        "source_postcheck_contract": source_postcheck,
        "source_postcheck_contract_validation": postcheck_validation,
        "source_wrapper_id": str(source_postcheck.get("source_wrapper_id", "")),
        "source_wrapper_sha256": str(source_postcheck.get("source_wrapper_sha256", "")),
        "source_runner_plan_id": str(source_postcheck.get("source_runner_plan_id", "")),
        "source_runner_plan_sha256": str(source_postcheck.get("source_runner_plan_sha256", "")),
        "source_package_id": str(source_postcheck.get("source_package_id", "")),
        "source_package_sha256": str(source_postcheck.get("source_package_sha256", "")),
        "target_worker_id": worker_id,
        "dispatch_target_worker_id": worker_id,
        "backend_kind": BACKEND_KIND,
        "evidence_mode": EVIDENCE_MODE,
        "evidence_state": EVIDENCE_STATE,
        "raw_event_evidence": raw_evidence,
        "plugin_runtime_evidence": _plugin_runtime_evidence(source_postcheck, raw_evidence),
        "terminal_truth": _dict(source_postcheck.get("terminal_truth")),
        "write_scope_verification": _write_scope_verification(source_postcheck, raw_evidence),
        "allowed_write_roots": _list_str(source_postcheck.get("allowed_write_roots")),
        "forbidden_paths": _list_str(source_postcheck.get("forbidden_paths")),
        "would_execute_opencode": False,
        "would_execute_live": False,
        "would_touch_worker": False,
        "would_create_temp_root": False,
        "would_read_live_artifacts": False,
        "would_write_live_config": False,
        "would_write_task_local": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "would_collect_runtime_evidence": False,
        "would_touch_tmux": False,
        "would_call_tui": False,
        "safe_next_actions": [
            "review supplied raw-event evidence without reading live artifacts",
            "reject plugin or hook claims unless raw event evidence is present and path-bound",
            "request separate explicit authorization before running any runtime/plugin proof attempt",
        ],
        "forbidden_next_actions": [
            "do not execute OpenCode from this evidence postcheck",
            "do not read raw-events.jsonl from live or disposable roots from this validator",
            "do not collect runtime evidence, call /tui, touch tmux, or mutate workers",
            "do not write task-local protocol markers, registries, or .omx/state/team",
            "do not infer native subagent lifecycle from process trees or plugin import alone",
        ],
        "advisory": (
            "Supplied-json-only runtime-proof evidence postcheck; it does not execute OpenCode, "
            "does not read live artifacts, does not collect runtime evidence, and does not write "
            "task-local markers. It validates externally supplied raw event evidence before any "
            "plugin import or hook invocation claim is accepted."
        ),
    }
    evidence_postcheck["validation"] = validate_runtime_proof_evidence_postcheck(evidence_postcheck)
    evidence_postcheck["blocked_reasons"] = list(evidence_postcheck["validation"]["errors"])
    evidence_postcheck["warnings"] = list(evidence_postcheck["validation"]["warnings"])
    return evidence_postcheck


def validate_runtime_proof_evidence_postcheck(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings = [
        "Runtime-proof evidence postcheck validation is supplied-json-only/no-execution",
        "Validation does not execute OpenCode, read live artifacts, collect runtime evidence, call /tui, or touch tmux",
    ]

    if payload.get("evidence_postcheck_kind") != EVIDENCE_POSTCHECK_KIND:
        errors.append(f"evidence_postcheck_kind must be {EVIDENCE_POSTCHECK_KIND}")
    if int(payload.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not str(payload.get("evidence_postcheck_id", "")):
        errors.append("evidence_postcheck_id missing")
    if payload.get("backend_kind") != BACKEND_KIND:
        errors.append(f"backend_kind must be {BACKEND_KIND}")
    if payload.get("evidence_mode") != EVIDENCE_MODE:
        errors.append(f"evidence_mode must be {EVIDENCE_MODE}")
    if payload.get("evidence_state") != EVIDENCE_STATE:
        errors.append("evidence_state must be review_only")

    for key in _false_flag_keys():
        if bool(payload.get(key, False)):
            errors.append(f"{key} must be false")

    source_postcheck = _dict(payload.get("source_postcheck_contract"))
    if source_postcheck.get("postcheck_kind") != POSTCHECK_KIND:
        errors.append(f"source postcheck_kind must be {POSTCHECK_KIND}")
    expected_hash = runtime_proof_evidence_postcheck_source_sha256(source_postcheck)
    if str(payload.get("source_postcheck_contract_sha256", "")) != expected_hash:
        errors.append("source_postcheck_contract_sha256 mismatch")
    if str(payload.get("source_postcheck_contract_id", "")) != str(source_postcheck.get("postcheck_contract_id", "")):
        errors.append("source_postcheck_contract_id does not match source postcheck contract")

    postcheck_validation = validate_runtime_proof_postcheck_contract(source_postcheck)
    if not postcheck_validation["ok"]:
        errors.append("source postcheck contract validation failed")
        errors.extend(f"source postcheck contract: {item}" for item in _list_str(postcheck_validation.get("errors")))
    if not bool(_dict(payload.get("source_postcheck_contract_validation")).get("ok", False)):
        errors.append("embedded source_postcheck_contract_validation must be ok")

    target_worker_id = str(payload.get("target_worker_id", ""))
    if target_worker_id != str(source_postcheck.get("target_worker_id", "")):
        errors.append("target_worker_id does not match source postcheck contract")
    if str(payload.get("dispatch_target_worker_id", "")) != target_worker_id:
        errors.append("dispatch_target_worker_id must match target_worker_id")

    raw_evidence = _dict(payload.get("raw_event_evidence"))
    _validate_raw_event_evidence(errors, raw_evidence, source_postcheck)

    terminal = _dict(payload.get("terminal_truth"))
    if terminal.get("terminal_marker") not in TERMINAL_MARKERS:
        errors.append("terminal_truth terminal_marker must be .done/.fail/.blocked")
    if int(terminal.get("terminal_marker_count", 0) or 0) != 1:
        errors.append("terminal_truth terminal_marker_count must be 1")

    write_scope = _dict(payload.get("write_scope_verification"))
    for key in [
        "no_live_config_write",
        "no_task_local_write",
        "no_registry_write",
        "no_state_team_write",
        "no_forbidden_path_writes",
    ]:
        if write_scope.get(key) is not True:
            errors.append(f"write_scope_verification.{key} must be true")

    plugin_evidence = _plugin_runtime_evidence(source_postcheck, raw_evidence)
    return {
        "ok": not errors,
        "errors": _dedupe(errors),
        "warnings": warnings,
        "source_postcheck_contract_hash": str(payload.get("source_postcheck_contract_sha256", "")),
        "source_postcheck_contract_validation_ok": postcheck_validation["ok"],
        "raw_event_evidence_ok": _raw_event_evidence_ok(raw_evidence, source_postcheck),
        "plugin_runtime_load_proven": plugin_evidence["plugin_runtime_load_proven"],
        "plugin_import_observed": plugin_evidence["plugin_import_observed"],
        "hook_invocation_observed": plugin_evidence["hook_invocation_observed"],
        "native_subagent_lifecycle_proven": plugin_evidence["native_subagent_lifecycle_proven"],
        "no_runtime_execution": not bool(payload.get("would_execute_opencode", False)),
        "no_live_artifact_reads": not bool(payload.get("would_read_live_artifacts", False)),
        "no_runtime_evidence_collection": not bool(payload.get("would_collect_runtime_evidence", False)),
        "no_task_local_write": not bool(payload.get("would_write_task_local", False)),
        "no_registry_write": not bool(payload.get("would_write_registry", False)),
        "no_state_team_write": not bool(payload.get("would_write_state_team", False)),
    }


def _validate_raw_event_evidence(
    errors: list[str],
    evidence: dict[str, Any],
    source_postcheck: dict[str, Any],
) -> None:
    if not evidence:
        errors.append("raw event evidence missing")
        return
    if evidence.get("evidence_kind") != RAW_EVENT_EVIDENCE_KIND:
        errors.append(f"raw event evidence_kind must be {RAW_EVENT_EVIDENCE_KIND}")

    expected_raw_events = str(_dict(source_postcheck.get("artifact_path_bindings")).get("raw_events_jsonl_path", ""))
    if str(evidence.get("raw_events_jsonl_path", "")) != expected_raw_events:
        errors.append("raw_events_jsonl_path must match source postcheck artifact binding")
    if bool(evidence.get("forbidden_path_writes_detected")):
        errors.append("raw event evidence forbidden_path_writes_detected must be false")
    if evidence.get("read_from_disposable_root_only") is not True:
        errors.append("raw event evidence read_from_disposable_root_only must be true")

    plugin_claimed = bool(evidence.get("plugin_runtime_load_proven"))
    if plugin_claimed:
        if int(evidence.get("raw_event_count", 0) or 0) <= 0:
            errors.append("plugin_runtime_load_proven requires raw event evidence")
        if not bool(evidence.get("plugin_import_observed")):
            errors.append("plugin_import_observed must be true when plugin_runtime_load_proven is true")
        if not bool(evidence.get("hook_invocation_observed")):
            errors.append("hook_invocation_observed must be true when plugin_runtime_load_proven is true")
        observed = set(_list_str(evidence.get("observed_event_kinds")))
        for required in ["plugin_import", "hook_invocation"]:
            if required not in observed:
                errors.append(f"observed_event_kinds must include {required}")

    if bool(evidence.get("native_subagent_lifecycle_proven")) and not bool(
        evidence.get("native_subagent_lifecycle_events_observed")
    ):
        errors.append("native_subagent_lifecycle_proven requires explicit lifecycle event evidence")


def _plugin_runtime_evidence(source_postcheck: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    expected_raw_events = str(_dict(source_postcheck.get("artifact_path_bindings")).get("raw_events_jsonl_path", ""))
    return {
        "raw_events_jsonl_path": str(evidence.get("raw_events_jsonl_path", "")),
        "expected_raw_events_jsonl_path": expected_raw_events,
        "raw_events_path_bound": str(evidence.get("raw_events_jsonl_path", "")) == expected_raw_events,
        "raw_event_count": int(evidence.get("raw_event_count", 0) or 0),
        "observed_event_kinds": _list_str(evidence.get("observed_event_kinds")),
        "plugin_import_observed": bool(evidence.get("plugin_import_observed")),
        "hook_invocation_observed": bool(evidence.get("hook_invocation_observed")),
        "plugin_runtime_load_proven": bool(evidence.get("plugin_runtime_load_proven")),
        "native_subagent_lifecycle_proven": bool(evidence.get("native_subagent_lifecycle_proven")),
    }


def _write_scope_verification(source_postcheck: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    source_write_scope = _dict(source_postcheck.get("write_scope_verification"))
    return {
        "no_live_config_write": source_write_scope.get("no_live_config_write") is True,
        "no_task_local_write": source_write_scope.get("no_task_local_write") is True,
        "no_registry_write": source_write_scope.get("no_registry_write") is True,
        "no_state_team_write": source_write_scope.get("no_state_team_write") is True,
        "no_forbidden_path_writes": source_write_scope.get("no_forbidden_path_writes") is True
        and not bool(evidence.get("forbidden_path_writes_detected")),
    }


def _raw_event_evidence_ok(evidence: dict[str, Any], source_postcheck: dict[str, Any]) -> bool:
    errors: list[str] = []
    _validate_raw_event_evidence(errors, evidence, source_postcheck)
    return not errors


def _false_flag_keys() -> list[str]:
    return [
        "would_execute_opencode",
        "would_execute_live",
        "would_touch_worker",
        "would_create_temp_root",
        "would_read_live_artifacts",
        "would_write_live_config",
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
