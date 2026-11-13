"""Serializable models for no-live meeting artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CONTROL_PLANE_DRY_RUN_KIND = "control_plane_dry_run"
CONTROL_PLANE_DRY_RUN_ACTION_FIELDS = frozenset({"kind", "workers", "title", "body", "timeout"})
AG2_DEPENDENCY_REVIEW_REQUIRED_GATES = (
    "package_choice_pinned",
    "optional_import_guard_defined",
    "provider_secrets_out_of_repo",
    "model_routing_disabled_by_default",
    "no_live_fallback_defined",
    "autogen_maintenance_status_recorded",
)
AG2_DEPENDENCY_REVIEW_SOURCE_URLS = (
    "https://github.com/ag2ai/ag2",
    "https://docs.ag2.ai/",
    "https://github.com/microsoft/autogen",
    "https://learn.microsoft.com/en-us/agent-framework/migration-guide/from-autogen/",
)
AG2_DEPENDENCY_REVIEW_DECISION_STATES = frozenset(
    {"unreviewed", "blocked_pending_policy", "optional_candidate", "rejected"}
)
AG2_DEPENDENCY_REVIEW_RECORD_FIELDS = frozenset(
    {
        "decision_state",
        "package_name",
        "import_name",
        "review_scope",
        "required_gates",
        "source_urls",
        "reference_commit",
        "notes",
        "would_install_dependency",
        "would_import_ag2",
        "would_execute_live",
        "provider_or_model_config_required",
    }
)
AG2_OPTIONAL_IMPORT_GATE_DECISION_STATES = frozenset({"blocked_pending_policy", "rejected", "allowed"})
AG2_OPTIONAL_IMPORT_GATE_FIELDS = frozenset(
    {
        "decision_state",
        "package_name",
        "import_name",
        "gate_scope",
        "required_gates",
        "satisfied_gates",
        "missing_gates",
        "import_allowed",
        "decision_reason",
        "would_install_dependency",
        "would_import_ag2",
        "would_execute_live",
        "provider_or_model_config_required",
    }
)
AG2_RUNTIME_IMPORT_POLICY_KIND = "ag2_runtime_import_policy"
AG2_RUNTIME_IMPORT_POLICY_STATES = frozenset({"blocked_pending_policy", "rejected"})
AG2_RUNTIME_IMPORT_POLICY_FIELDS = frozenset(
    {
        "schema_version",
        "policy_kind",
        "policy_scope",
        "policy_state",
        "approval_required",
        "approval_artifact_required",
        "human_approval_required",
        "runtime_import_allowed",
        "decision_reason",
        "would_install_dependency",
        "would_import_ag2",
        "would_execute_live",
        "provider_or_model_config_required",
        "ag2_import_gate",
    }
)
AG2_HOST_LOCAL_ADAPTER_PLAN_KIND = "ag2_host_local_adapter_plan"
AG2_HOST_LOCAL_ADAPTER_PLAN_STATES = frozenset({"blocked_pending_policy", "rejected"})
AG2_HOST_LOCAL_ADAPTER_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "plan_kind",
        "plan_scope",
        "plan_state",
        "host_local_only",
        "runtime_import_policy_required",
        "runtime_import_policy_gate_satisfied",
        "adapter_enabled",
        "adapter_eligible",
        "supports_live_submit",
        "dispatches_via_control_plane",
        "uses_tmux_substrate",
        "uses_model_routing",
        "model_routing_disabled_by_default",
        "decision_reason",
        "would_install_dependency",
        "would_import_ag2",
        "would_execute_live",
        "provider_or_model_config_required",
        "runtime_import_policy",
    }
)
AG2_HUMAN_APPROVAL_ARTIFACT_KIND = "ag2_human_approval_artifact"
AG2_HUMAN_APPROVAL_ARTIFACT_STATES = frozenset({"absent", "pending", "rejected", "expired", "revoked"})
AG2_HUMAN_APPROVAL_ARTIFACT_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_kind",
        "approval_scope",
        "approval_state",
        "approval_id",
        "approved_by",
        "approved_at",
        "expires_at",
        "policy_snapshot_sha256",
        "decision_reason",
        "advisory_only",
        "runtime_import_allowed",
        "adapter_eligibility_granted",
        "would_install_dependency",
        "would_import_ag2",
        "would_execute_live",
        "provider_or_model_config_required",
    }
)
AG2_HOST_LOCAL_CONFIG_CONTRACT_KIND = "ag2_host_local_config_contract"
AG2_HOST_LOCAL_CONFIG_CONTRACT_STATES = frozenset({"absent", "rejected"})
AG2_HOST_LOCAL_CONFIG_CONTRACT_FIELDS = frozenset(
    {
        "schema_version",
        "contract_kind",
        "contract_scope",
        "contract_state",
        "host_local_only",
        "advisory_only",
        "config_reference",
        "config_values_recorded",
        "provider_config_present",
        "model_config_present",
        "endpoint_config_present",
        "secret_config_present",
        "synced_to_repo",
        "model_routing_disabled_by_default",
        "adapter_eligibility_granted",
        "decision_reason",
        "would_install_dependency",
        "would_import_ag2",
        "would_execute_live",
        "provider_or_model_config_required",
    }
)
AG2_REAL_RUNTIME_POLICY_ARTIFACT_KIND = "ag2_real_runtime_policy_artifact"
AG2_REAL_RUNTIME_POLICY_ARTIFACT_STATES = frozenset({"blocked_pending_policy", "rejected"})
AG2_REAL_RUNTIME_POLICY_ARTIFACT_FIELDS = frozenset(
    {
        "schema_version",
        "policy_kind",
        "policy_scope",
        "policy_state",
        "decision_reason",
        "required_policy_gates",
        "missing_gates",
        "satisfied_gates",
        "requires_human_approval",
        "requires_host_local_config",
        "provider_policy",
        "model_policy",
        "redaction_policy",
        "cost_policy",
        "timeout_policy",
        "sync_boundary_policy",
        "approval_format_policy",
        "host_local_config_format_policy",
        "would_install_dependency",
        "would_import_ag2",
        "would_execute_live",
        "provider_or_model_config_required",
    }
)
AG2_REAL_RUNTIME_APPROVAL_FORMAT_ALLOWED_STATES = ("absent", "pending", "rejected", "expired", "revoked")
AG2_REAL_RUNTIME_APPROVAL_IDENTITY_FIELDS = (
    "approval_id",
    "approved_by",
    "approved_at",
    "expires_at",
    "policy_snapshot_sha256",
)
AG2_REAL_RUNTIME_FORMAT_FORBIDDEN_PAYLOAD_FIELDS = (
    "allow_real",
    "base_url",
    "api_key",
    "model",
    "provider",
    "host_local_config",
    "sync_config",
    "dynamic_plugin",
    "worker",
    "report.md",
    ".done",
)
AG2_REAL_RUNTIME_CONFIG_FORBIDDEN_PATH_ROOTS = (
    "synced repository root",
    "runtime root",
    "business repository root",
)
AG2_REAL_RUNTIME_CONFIG_FORBIDDEN_PATH_PARTS = (
    "multi-agent_questions",
    "multi-agent_question_artifacts",
    "report.md",
    ".done",
    ".fail",
    ".blocked",
)
AG2_REAL_RUNTIME_POLICY_SECTION_FIELDS = {
    "provider_policy": frozenset(
        {
            "policy_state",
            "gate_name",
            "gate_satisfied",
            "provider_family_allowed",
            "credentials_location_kind",
            "base_url_allowed",
            "api_key_reference_required",
            "provider_values_recorded",
            "decision_reason",
        }
    ),
    "model_policy": frozenset(
        {
            "policy_state",
            "gate_name",
            "gate_satisfied",
            "non_v4_pro_required",
            "model_routing_allowed",
            "allowed_model_class",
            "v4_pro_allowed",
            "decision_reason",
        }
    ),
    "redaction_policy": frozenset(
        {
            "policy_state",
            "gate_name",
            "gate_satisfied",
            "redaction_required",
            "transcript_redaction_required",
            "secret_echo_allowed",
            "decision_reason",
        }
    ),
    "cost_policy": frozenset(
        {
            "policy_state",
            "gate_name",
            "gate_satisfied",
            "budget_required",
            "spend_cap_recorded",
            "decision_reason",
        }
    ),
    "timeout_policy": frozenset(
        {
            "policy_state",
            "gate_name",
            "gate_satisfied",
            "timeout_required",
            "max_timeout_recorded",
            "decision_reason",
        }
    ),
    "sync_boundary_policy": frozenset(
        {
            "policy_state",
            "gate_name",
            "gate_satisfied",
            "host_local_only",
            "synced_runtime_config_allowed",
            "stignore_boundary_reviewed",
            "decision_reason",
        }
    ),
    "approval_format_policy": frozenset(
        {
            "policy_state",
            "gate_name",
            "gate_satisfied",
            "approval_state_allowed_values",
            "approval_identity_fields",
            "approval_identity_fields_must_remain_empty",
            "advisory_only_required",
            "effective_runtime_import_allowed_must_remain_false",
            "effective_adapter_eligibility_granted_must_remain_false",
            "forbidden_payload_fields",
            "decision_reason",
        }
    ),
    "host_local_config_format_policy": frozenset(
        {
            "policy_state",
            "gate_name",
            "gate_satisfied",
            "contract_scope_required",
            "host_local_only_required",
            "advisory_only_required",
            "content_blind_reference_only",
            "absolute_path_required",
            "forbidden_path_roots",
            "forbidden_path_parts",
            "config_reference_must_remain_empty_in_contract",
            "config_values_recorded_must_remain_false",
            "synced_to_repo_must_remain_false",
            "forbidden_payload_fields",
            "decision_reason",
        }
    ),
}
MEETING_SYNTHESIS_METHOD = "deterministic"
MEETING_AUDIT_METHOD = "deterministic"
MEETING_SYNTHESIS_SUMMARY_FIELDS = frozenset(
    {"method", "verdict", "risks", "open_questions", "recommended_next_actions"}
)
MEETING_SYNTHESIS_FORBIDDEN_TEXT = (
    "send-keys",
    "paste-buffer",
    "/tui/",
    "tmux",
    "rmux",
    "subprocess",
    "omx_task_signal",
    "report.md",
    ".done",
    ".fail",
    ".blocked",
    "--allow-real",
)
AG2_VALIDATION_FALSE_FIELDS = (
    "effective_runtime_import_allowed",
    "effective_adapter_eligibility_granted",
    "adapter_eligibility_granted",
    "config_values_recorded",
    "synced_to_repo",
    "would_install_dependency",
    "would_import_ag2",
    "would_execute_live",
    "provider_or_model_config_required",
)
AG2_REAL_RUNTIME_PREFLIGHT_KIND = "ag2_real_runtime_preflight"
AG2_REAL_RUNTIME_PREFLIGHT_MISSING_GATES = (
    "runtime_import_policy_not_allowed",
    "adapter_plan_disabled",
    "human_approval_not_effective",
    "host_local_config_not_effective",
    "provider_policy_missing",
    "non_v4_pro_model_policy_missing",
    "redaction_policy_missing",
    "cost_timeout_policy_missing",
)
AG2_EFFECTIVE_RUNTIME_BOUNDARY_KIND = "ag2_effective_runtime_boundary_contract"
AG2_EFFECTIVE_RUNTIME_BOUNDARY_REQUIRED_GATES = (
    ("runtime_import_policy", "runtime_import_policy_not_allowed"),
    ("adapter_plan", "adapter_plan_disabled"),
    ("human_approval_semantics", "human_approval_not_effective"),
    ("host_local_config_storage", "host_local_config_not_effective"),
    ("provider_policy", "provider_policy_missing"),
    ("non_v4_pro_model_policy", "non_v4_pro_model_policy_missing"),
    ("redaction_policy", "redaction_policy_missing"),
    ("cost_timeout_policy", "cost_timeout_policy_missing"),
    ("sync_boundary_policy", "sync_boundary_policy_missing"),
)
AG2_RELEASE_READINESS_SUPPORTED_MODES = (
    "mock_ag2_meeting",
    "control_plane_dry_run_bridge",
    "ag2_status",
    "safety_check",
    "release_readiness_status",
)
AG2_RELEASE_READINESS_UNSUPPORTED_MODES = (
    "real_ag2_runtime",
    "provider_calls",
    "v4_pro",
    "live_worker_dispatch_from_meeting",
    "raw_tmux_tui",
    "synced_provider_config",
)
AG2_RELEASE_READINESS_REQUIRED_CHECKS = (
    "pytest_meeting_prototype",
    "related_control_plane_regression",
    "compileall_meeting_layer",
    "safety_check",
    "bridge_dry_run_smoke",
    "markdown_only_process_directory",
    "worker_locks_clean",
)
AG2_EFFECTIVE_APPROVAL_KIND = "ag2_effective_runtime_approval"
AG2_EFFECTIVE_APPROVAL_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_kind",
        "approval_state",
        "approval_id",
        "approved_by",
        "approved_at",
        "expires_at",
        "policy_snapshot_sha256",
        "decision_reason",
    }
)
AG2_RUNTIME_CONFIG_FIELDS = frozenset(
    {
        "provider",
        "model",
        "base_url",
        "api_key_env",
        "timeout_seconds",
        "max_cost_usd",
        "empty_response_retries",
    }
)
AG2_REAL_RUNTIME_DEPENDENCY_DECISION_KIND = "ag2_real_runtime_dependency_decision"
AG2_LIVE_SMOKE_EVIDENCE_KIND = "ag2_live_model_smoke_evidence"
AG2_LIVE_SMOKE_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_kind",
        "smoke_state",
        "provider_endpoint_kind",
        "runtime_mode",
        "meeting_artifacts_only",
        "report_or_marker_writes",
        "worker_lock_writes",
        "live_worker_dispatch",
        "v4_pro_used",
        "secret_values_recorded",
        "provider_values_recorded",
        "redaction_proof",
        "cost_timeout_proof",
        "artifact_paths",
        "result_summary",
    }
)
AG2_LIVE_SMOKE_REQUIRED_ARTIFACTS = (
    "meeting-result.json",
    "meeting-synthesis.json",
    "meeting-audit.json",
    "transcript.md",
)
AG2_RELEASE_PROOF_BUNDLE_KIND = "ag2_real_runtime_release_proof_bundle"
AG2_RELEASE_PROOF_BUNDLE_FIELDS = frozenset(
    {
        "schema_version",
        "proof_kind",
        "proof_state",
        "rollback_instructions_present",
        "no_secret_sync_proof",
        "no_v4_pro_proof",
        "no_live_worker_dispatch_proof",
        "regression_tests_passed",
        "safety_check_passed",
        "compileall_passed",
        "markdown_only_passed",
        "evidence_paths",
    }
)


def build_ag2_real_runtime_dependency_decision() -> dict[str, Any]:
    """Return the Phase R1 dependency/package decision for real-runtime work."""
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_kind": AG2_REAL_RUNTIME_DEPENDENCY_DECISION_KIND,
        "decision_state": "decided",
        "runtime_backend": "host_local_openai_compatible_client",
        "ag2_sdk_import_decision": "defer",
        "package_name": "ag2",
        "import_name": "autogen",
        "optional_dependency_strategy": "defer_ag2_sdk_until_sdk_specific_adapter_is_required",
        "version_pin": "",
        "install_sync_scope": "none",
        "would_install_dependency": False,
        "would_import_ag2": False,
        "would_execute_live": False,
        "synced_provider_config_allowed": False,
        "rollback_plan": (
            "AG2 SDK import remains deferred; disable --runtime real or use --real-runtime-mode fake to return to "
            "the project-local deterministic/fake-client path without uninstalling dependencies."
        ),
        "source_urls": list(AG2_DEPENDENCY_REVIEW_SOURCE_URLS),
        "notes": [
            "Use the host-local OpenAI-compatible client seam for the first controlled live smoke.",
            "Do not add AG2/autogen imports until a later SDK-specific adapter is explicitly required.",
            "Keep provider/model/base_url/api_key material outside synced repository artifacts.",
        ],
    }


def build_ag2_real_runtime_release_gate(
    live_smoke_evidence: dict[str, Any] | None = None,
    release_proof_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the current R1-R7 real-runtime release gate decision."""
    evidence_validation = None
    if live_smoke_evidence is not None:
        if live_smoke_evidence.get("validation_kind") == "ag2_live_smoke_evidence_validation":
            normalized_evidence = live_smoke_evidence.get("normalized_payload")
            evidence_validation = validate_ag2_live_smoke_evidence_payload(
                normalized_evidence if isinstance(normalized_evidence, dict) else {}
            )
        else:
            evidence_validation = validate_ag2_live_smoke_evidence_payload(live_smoke_evidence)
    proof_validation = None
    if release_proof_bundle is not None:
        if release_proof_bundle.get("validation_kind") == "ag2_release_proof_bundle_validation":
            normalized_proof = release_proof_bundle.get("normalized_payload")
            proof_validation = validate_ag2_release_proof_bundle_payload(
                normalized_proof if isinstance(normalized_proof, dict) else {}
            )
        else:
            proof_validation = validate_ag2_release_proof_bundle_payload(release_proof_bundle)
    live_smoke_eligible = bool(evidence_validation and evidence_validation["release_eligible"])
    proof_eligible = bool(proof_validation and proof_validation["release_eligible"])
    release_eligible = live_smoke_eligible and proof_eligible
    if release_eligible:
        release_state = "ready_after_controlled_live_smoke"
        go_no_go = "go_real_runtime"
        r6_status = "complete"
        r7_status = "complete"
        missing_release_evidence: list[str] = []
    elif live_smoke_eligible:
        release_state = "no_go_pending_final_proof_bundle"
        go_no_go = "no_go_real_runtime"
        r6_status = "complete"
        r7_status = "pending_final_proof_bundle"
        missing_release_evidence = [
            "release_proof_bundle_missing",
            "rollback_instructions_pending",
            "no_secret_sync_proof_pending",
            "no_v4_pro_proof_pending",
            "no_live_worker_dispatch_proof_pending",
        ]
    else:
        release_state = "no_go_pending_controlled_live_smoke"
        go_no_go = "no_go_real_runtime"
        r6_status = "pending_explicit_user_approval"
        r7_status = "blocked_by_r6"
        missing_release_evidence = [
            "controlled_live_model_smoke_not_run",
            "live_cost_timeout_redaction_record_missing",
            "rollback_instructions_pending",
            "no_secret_sync_proof_pending",
            "no_v4_pro_proof_pending",
            "no_live_worker_dispatch_proof_pending",
        ]
    return {
        "schema_version": SCHEMA_VERSION,
        "gate_kind": "ag2_real_runtime_release_gate",
        "release_state": release_state,
        "go_no_go": go_no_go,
        "ready_for_real_runtime_release": release_eligible,
        "would_install_dependency": False,
        "would_import_ag2": False,
        "would_execute_live": False,
        "provider_or_model_config_required": False,
        "dependency_decision": build_ag2_real_runtime_dependency_decision(),
        "phase_status": {
            "R1": {
                "status": "complete",
                "evidence": "ag2_real_runtime_dependency_decision",
            },
            "R2": {
                "status": "complete",
                "evidence": "load_ag2_host_local_runtime_config redaction tests",
            },
            "R3": {
                "status": "complete",
                "evidence": "validate_ag2_effective_approval_payload enforcement tests",
            },
            "R4": {
                "status": "complete",
                "evidence": "RealAg2RuntimeAdapter fake-client tests and ag2-run --runtime real --real-runtime-mode fake",
            },
            "R5": {
                "status": "complete",
                "evidence": "local OpenAI-compatible HTTP dry-run test",
            },
            "R6": {
                "status": r6_status,
                "evidence": "controlled live external provider smoke has not been run",
            },
            "R7": {
                "status": r7_status,
                "evidence": "real runtime release requires R6 smoke and final proof bundle",
            },
        },
        "live_smoke_evidence_validation": evidence_validation,
        "release_proof_bundle_validation": proof_validation,
        "missing_release_evidence": missing_release_evidence,
        "notes": [
            "fake-client and local HTTP dry-run evidence are not a substitute for controlled external provider smoke",
            "do not mark real runtime released until R6 and R7 evidence are present",
        ],
    }


def validate_ag2_release_proof_bundle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    normalized_payload: dict[str, Any] | None = None
    if not isinstance(payload, dict):
        errors.append("AG2 release proof bundle payload must be an object")
    else:
        unsupported = sorted(set(payload) - AG2_RELEASE_PROOF_BUNDLE_FIELDS)
        if unsupported:
            errors.append(f"unsupported AG2 release proof bundle fields: {', '.join(unsupported)}")
        if payload.get("schema_version") != SCHEMA_VERSION:
            errors.append("AG2 release proof bundle schema_version must match the meeting schema version")
        if payload.get("proof_kind") != AG2_RELEASE_PROOF_BUNDLE_KIND:
            errors.append(f"AG2 release proof bundle proof_kind must be {AG2_RELEASE_PROOF_BUNDLE_KIND}")
        if payload.get("proof_state") != "passed":
            errors.append("AG2 release proof bundle proof_state must be passed")
        required_true = (
            "rollback_instructions_present",
            "no_secret_sync_proof",
            "no_v4_pro_proof",
            "no_live_worker_dispatch_proof",
            "regression_tests_passed",
            "safety_check_passed",
            "compileall_passed",
            "markdown_only_passed",
        )
        for field_name in required_true:
            if payload.get(field_name) is not True:
                errors.append(f"AG2 release proof bundle {field_name} must be true")
        try:
            evidence_paths = _normalize_string_tuple(payload.get("evidence_paths", ()), "evidence_paths")
        except ValueError as exc:
            errors.append(str(exc))
            evidence_paths = ()
        if not evidence_paths:
            errors.append("AG2 release proof bundle evidence_paths must not be empty")
        forbidden_text = json.dumps(payload, sort_keys=True).lower()
        for token in ("api_key", "base_url", "v4-pro", "secret=", "bearer "):
            if token in forbidden_text:
                errors.append(f"AG2 release proof bundle must not record {token.strip()}")
    valid = not errors
    if valid:
        normalized_payload = {
            "schema_version": SCHEMA_VERSION,
            "proof_kind": AG2_RELEASE_PROOF_BUNDLE_KIND,
            "proof_state": "passed",
            "rollback_instructions_present": True,
            "no_secret_sync_proof": True,
            "no_v4_pro_proof": True,
            "no_live_worker_dispatch_proof": True,
            "regression_tests_passed": True,
            "safety_check_passed": True,
            "compileall_passed": True,
            "markdown_only_passed": True,
            "evidence_paths": list(evidence_paths),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "validation_kind": "ag2_release_proof_bundle_validation",
        "valid": valid,
        "release_eligible": valid,
        "normalized_payload": normalized_payload,
        "errors": errors,
        "warnings": [] if errors else ["release proof bundle is sufficient for the real-runtime release gate"],
    }


def validate_ag2_live_smoke_evidence_payload(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    normalized_payload: dict[str, Any] | None = None
    if not isinstance(payload, dict):
        errors.append("AG2 live smoke evidence payload must be an object")
    else:
        unsupported = sorted(set(payload) - AG2_LIVE_SMOKE_EVIDENCE_FIELDS)
        if unsupported:
            errors.append(f"unsupported AG2 live smoke evidence fields: {', '.join(unsupported)}")
        if payload.get("schema_version") != SCHEMA_VERSION:
            errors.append("AG2 live smoke evidence schema_version must match the meeting schema version")
        if payload.get("evidence_kind") != AG2_LIVE_SMOKE_EVIDENCE_KIND:
            errors.append(f"AG2 live smoke evidence_kind must be {AG2_LIVE_SMOKE_EVIDENCE_KIND}")
        if payload.get("smoke_state") != "passed":
            errors.append("AG2 live smoke evidence smoke_state must be passed")
        if payload.get("provider_endpoint_kind") != "external_provider":
            errors.append("AG2 live smoke evidence provider_endpoint_kind must be external_provider")
        if payload.get("runtime_mode") != "live":
            errors.append("AG2 live smoke evidence runtime_mode must be live")
        required_true = ("meeting_artifacts_only",)
        required_false = (
            "report_or_marker_writes",
            "worker_lock_writes",
            "live_worker_dispatch",
            "v4_pro_used",
            "secret_values_recorded",
            "provider_values_recorded",
        )
        for field_name in required_true:
            if payload.get(field_name) is not True:
                errors.append(f"AG2 live smoke evidence {field_name} must be true")
        for field_name in required_false:
            if payload.get(field_name) is not False:
                errors.append(f"AG2 live smoke evidence {field_name} must be false")
        for field_name in ("redaction_proof", "cost_timeout_proof"):
            if not isinstance(payload.get(field_name), str) or not payload[field_name].strip():
                errors.append(f"AG2 live smoke evidence {field_name} must be a non-empty string")
        artifact_paths = _normalize_string_tuple(payload.get("artifact_paths", ()), "artifact_paths")
        artifact_names = tuple(Path(path).name for path in artifact_paths)
        if artifact_names != AG2_LIVE_SMOKE_REQUIRED_ARTIFACTS:
            errors.append("AG2 live smoke evidence artifact_paths must contain only meeting artifact names")
        result_summary = payload.get("result_summary")
        if not isinstance(result_summary, dict):
            errors.append("AG2 live smoke evidence result_summary must be an object")
        else:
            if type(result_summary.get("message_count")) is not int or result_summary["message_count"] < 1:
                errors.append("AG2 live smoke evidence result_summary.message_count must be a positive integer")
            for field_name in ("termination_reason", "verdict"):
                if not isinstance(result_summary.get(field_name), str) or not result_summary[field_name].strip():
                    errors.append(f"AG2 live smoke evidence result_summary.{field_name} must be a non-empty string")
        forbidden_text = json.dumps(payload, sort_keys=True).lower()
        if "v4-pro" in forbidden_text:
            errors.append("AG2 live smoke evidence must not record v4-pro")
        if payload.get("secret_values_recorded") is not False:
            errors.append("AG2 live smoke evidence must not record secret values")
    valid = not errors
    if valid:
        normalized_payload = {
            "schema_version": SCHEMA_VERSION,
            "evidence_kind": AG2_LIVE_SMOKE_EVIDENCE_KIND,
            "smoke_state": "passed",
            "provider_endpoint_kind": "external_provider",
            "runtime_mode": "live",
            "meeting_artifacts_only": True,
            "report_or_marker_writes": False,
            "worker_lock_writes": False,
            "live_worker_dispatch": False,
            "v4_pro_used": False,
            "secret_values_recorded": False,
            "provider_values_recorded": False,
            "redaction_proof": payload["redaction_proof"].strip(),
            "cost_timeout_proof": payload["cost_timeout_proof"].strip(),
            "artifact_paths": list(artifact_paths),
            "result_summary": {
                "message_count": result_summary["message_count"],
                "termination_reason": result_summary["termination_reason"].strip(),
                "verdict": result_summary["verdict"].strip(),
            },
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "validation_kind": "ag2_live_smoke_evidence_validation",
        "valid": valid,
        "release_eligible": valid,
        "normalized_payload": normalized_payload,
        "errors": errors,
        "warnings": [] if errors else ["live smoke evidence is sufficient for the real-runtime release gate"],
    }


@dataclass(frozen=True)
class Ag2HostLocalRuntimeConfig:
    """Host-local runtime config kept out of synced artifacts."""

    config_reference: Path
    provider: str
    model: str
    base_url: str
    api_key_env: str
    timeout_seconds: int
    max_cost_usd: float
    empty_response_retries: int = 1

    def to_redacted_payload(self) -> dict[str, Any]:
        provider_present = bool(self.provider)
        model_present = bool(self.model)
        endpoint_present = bool(self.base_url)
        secret_present = bool(self.api_key_env)
        timeout_recorded = self.timeout_seconds > 0
        spend_recorded = self.max_cost_usd > 0
        return {
            "schema_version": SCHEMA_VERSION,
            "config_kind": "ag2_host_local_runtime_config_redacted",
            "config_reference": "<host-local-redacted>",
            "config_reference_sha256": _sha256_text(str(self.config_reference)),
            "ready_for_model_client": all(
                [provider_present, model_present, endpoint_present, secret_present, timeout_recorded, spend_recorded]
            ),
            "provider_config_present": provider_present,
            "model_config_present": model_present,
            "endpoint_config_present": endpoint_present,
            "secret_config_present": secret_present,
            "synced_to_repo": False,
            "provider_family_allowed": provider_present,
            "provider_values_recorded": False,
            "model_routing_allowed": False,
            "v4_pro_allowed": False,
            "secret_echo_allowed": False,
            "spend_cap_recorded": spend_recorded,
            "max_timeout_recorded": timeout_recorded,
            "empty_response_retry_recorded": self.empty_response_retries >= 0,
            "empty_response_retries": self.empty_response_retries,
            "synced_runtime_config_allowed": False,
            "model_class": "non_v4_pro",
        }


def _ag2_real_runtime_provider_policy() -> dict[str, Any]:
    return {
        "policy_state": "blocked_pending_policy",
        "gate_name": "provider_policy_missing",
        "gate_satisfied": False,
        "provider_family_allowed": False,
        "credentials_location_kind": "host_local_only",
        "base_url_allowed": False,
        "api_key_reference_required": True,
        "provider_values_recorded": False,
        "decision_reason": "Provider policy is not approved for real AG2 runtime.",
    }


def _ag2_real_runtime_model_policy() -> dict[str, Any]:
    return {
        "policy_state": "blocked_pending_policy",
        "gate_name": "non_v4_pro_model_policy_missing",
        "gate_satisfied": False,
        "non_v4_pro_required": True,
        "model_routing_allowed": False,
        "allowed_model_class": "unset",
        "v4_pro_allowed": False,
        "decision_reason": "Model policy is not approved; v4_pro remains disallowed.",
    }


def _ag2_real_runtime_redaction_policy() -> dict[str, Any]:
    return {
        "policy_state": "blocked_pending_policy",
        "gate_name": "redaction_policy_missing",
        "gate_satisfied": False,
        "redaction_required": True,
        "transcript_redaction_required": True,
        "secret_echo_allowed": False,
        "decision_reason": "Redaction policy is missing for provider transcripts and config references.",
    }


def _ag2_real_runtime_cost_policy() -> dict[str, Any]:
    return {
        "policy_state": "blocked_pending_policy",
        "gate_name": "cost_timeout_policy_missing",
        "gate_satisfied": False,
        "budget_required": True,
        "spend_cap_recorded": False,
        "decision_reason": "Cost policy and spend cap are not recorded.",
    }


def _ag2_real_runtime_timeout_policy() -> dict[str, Any]:
    return {
        "policy_state": "blocked_pending_policy",
        "gate_name": "cost_timeout_policy_missing",
        "gate_satisfied": False,
        "timeout_required": True,
        "max_timeout_recorded": False,
        "decision_reason": "Timeout policy is not recorded.",
    }


def _ag2_real_runtime_sync_boundary_policy() -> dict[str, Any]:
    return {
        "policy_state": "blocked_pending_policy",
        "gate_name": "host_local_config_not_effective",
        "gate_satisfied": False,
        "host_local_only": True,
        "synced_runtime_config_allowed": False,
        "stignore_boundary_reviewed": False,
        "decision_reason": "Syncthing and host-local config boundaries are not approved for real runtime.",
    }


def _ag2_real_runtime_approval_format_policy() -> dict[str, Any]:
    return {
        "policy_state": "blocked_pending_policy",
        "gate_name": "human_approval_not_effective",
        "gate_satisfied": False,
        "approval_state_allowed_values": list(AG2_REAL_RUNTIME_APPROVAL_FORMAT_ALLOWED_STATES),
        "approval_identity_fields": list(AG2_REAL_RUNTIME_APPROVAL_IDENTITY_FIELDS),
        "approval_identity_fields_must_remain_empty": True,
        "advisory_only_required": True,
        "effective_runtime_import_allowed_must_remain_false": True,
        "effective_adapter_eligibility_granted_must_remain_false": True,
        "forbidden_payload_fields": list(AG2_REAL_RUNTIME_FORMAT_FORBIDDEN_PAYLOAD_FIELDS),
        "decision_reason": "Approval format policy is descriptive only; approval remains non-effective.",
    }


def _ag2_real_runtime_host_local_config_format_policy() -> dict[str, Any]:
    return {
        "policy_state": "blocked_pending_policy",
        "gate_name": "host_local_config_not_effective",
        "gate_satisfied": False,
        "contract_scope_required": "host_local_config_no_sync",
        "host_local_only_required": True,
        "advisory_only_required": True,
        "content_blind_reference_only": True,
        "absolute_path_required": True,
        "forbidden_path_roots": list(AG2_REAL_RUNTIME_CONFIG_FORBIDDEN_PATH_ROOTS),
        "forbidden_path_parts": list(AG2_REAL_RUNTIME_CONFIG_FORBIDDEN_PATH_PARTS),
        "config_reference_must_remain_empty_in_contract": True,
        "config_values_recorded_must_remain_false": True,
        "synced_to_repo_must_remain_false": True,
        "forbidden_payload_fields": list(AG2_REAL_RUNTIME_FORMAT_FORBIDDEN_PAYLOAD_FIELDS),
        "decision_reason": "Host-local config format policy is content-blind and records no config values.",
    }


def _normalize_ag2_policy_gate_tuple(value: Any, field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"AG2 real runtime policy {field_name} must be a list of strings")
    normalized = tuple(str(item).strip() for item in value if str(item).strip())
    if not allow_empty and not normalized:
        raise ValueError(f"AG2 real runtime policy {field_name} must not be empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"AG2 real runtime policy {field_name} must not contain duplicates")
    return normalized


def _validate_ag2_real_runtime_policy_section(section_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"AG2 real runtime {section_name} must be an object")
    allowed_fields = AG2_REAL_RUNTIME_POLICY_SECTION_FIELDS[section_name]
    unsupported = sorted(set(payload) - allowed_fields)
    if unsupported:
        raise ValueError(f"unsupported AG2 real runtime {section_name} fields: {', '.join(unsupported)}")
    normalized = dict(payload)
    if normalized.get("policy_state") != "blocked_pending_policy":
        raise ValueError(f"AG2 real runtime {section_name} policy_state must remain blocked_pending_policy")
    if normalized.get("gate_satisfied") is not False:
        raise ValueError(f"AG2 real runtime {section_name} gate_satisfied must remain false")
    if not isinstance(normalized.get("decision_reason"), str) or not normalized["decision_reason"].strip():
        raise ValueError(f"AG2 real runtime {section_name} decision_reason must be a non-empty string")

    required_true = {
        "provider_policy": ("api_key_reference_required",),
        "model_policy": ("non_v4_pro_required",),
        "redaction_policy": ("redaction_required", "transcript_redaction_required"),
        "cost_policy": ("budget_required",),
        "timeout_policy": ("timeout_required",),
        "sync_boundary_policy": ("host_local_only",),
        "approval_format_policy": (
            "approval_identity_fields_must_remain_empty",
            "advisory_only_required",
            "effective_runtime_import_allowed_must_remain_false",
            "effective_adapter_eligibility_granted_must_remain_false",
        ),
        "host_local_config_format_policy": (
            "host_local_only_required",
            "advisory_only_required",
            "content_blind_reference_only",
            "absolute_path_required",
            "config_reference_must_remain_empty_in_contract",
            "config_values_recorded_must_remain_false",
            "synced_to_repo_must_remain_false",
        ),
    }[section_name]
    required_false = {
        "provider_policy": ("provider_family_allowed", "base_url_allowed", "provider_values_recorded"),
        "model_policy": ("model_routing_allowed", "v4_pro_allowed"),
        "redaction_policy": ("secret_echo_allowed",),
        "cost_policy": ("spend_cap_recorded",),
        "timeout_policy": ("max_timeout_recorded",),
        "sync_boundary_policy": ("synced_runtime_config_allowed", "stignore_boundary_reviewed"),
        "approval_format_policy": (),
        "host_local_config_format_policy": (),
    }[section_name]
    for field_name in required_true:
        if normalized.get(field_name) is not True:
            raise ValueError(f"AG2 real runtime {section_name} {field_name} must remain true")
    for field_name in required_false:
        if normalized.get(field_name) is not False:
            raise ValueError(f"AG2 real runtime {section_name} {field_name} must remain false")
    expected_gate = {
        "provider_policy": "provider_policy_missing",
        "model_policy": "non_v4_pro_model_policy_missing",
        "redaction_policy": "redaction_policy_missing",
        "cost_policy": "cost_timeout_policy_missing",
        "timeout_policy": "cost_timeout_policy_missing",
        "sync_boundary_policy": "host_local_config_not_effective",
        "approval_format_policy": "human_approval_not_effective",
        "host_local_config_format_policy": "host_local_config_not_effective",
    }[section_name]
    if normalized.get("gate_name") != expected_gate:
        raise ValueError(f"AG2 real runtime {section_name} gate_name must be {expected_gate}")
    if section_name == "provider_policy" and normalized.get("credentials_location_kind") != "host_local_only":
        raise ValueError("AG2 real runtime provider_policy credentials_location_kind must be host_local_only")
    if section_name == "model_policy" and normalized.get("allowed_model_class") != "unset":
        raise ValueError("AG2 real runtime model_policy allowed_model_class must remain unset")
    if section_name == "approval_format_policy":
        approval_states = _normalize_ag2_policy_gate_tuple(
            normalized.get("approval_state_allowed_values"),
            "approval_state_allowed_values",
        )
        identity_fields = _normalize_ag2_policy_gate_tuple(
            normalized.get("approval_identity_fields"),
            "approval_identity_fields",
        )
        forbidden_fields = _normalize_ag2_policy_gate_tuple(
            normalized.get("forbidden_payload_fields"),
            "forbidden_payload_fields",
        )
        if approval_states != AG2_REAL_RUNTIME_APPROVAL_FORMAT_ALLOWED_STATES:
            raise ValueError("AG2 real runtime approval_format_policy allowed states must match approval artifact states")
        if identity_fields != AG2_REAL_RUNTIME_APPROVAL_IDENTITY_FIELDS:
            raise ValueError("AG2 real runtime approval_format_policy identity fields must match approval artifact fields")
        if forbidden_fields != AG2_REAL_RUNTIME_FORMAT_FORBIDDEN_PAYLOAD_FIELDS:
            raise ValueError("AG2 real runtime approval_format_policy forbidden fields must match policy")
        normalized["approval_state_allowed_values"] = list(approval_states)
        normalized["approval_identity_fields"] = list(identity_fields)
        normalized["forbidden_payload_fields"] = list(forbidden_fields)
    if section_name == "host_local_config_format_policy":
        forbidden_roots = _normalize_ag2_policy_gate_tuple(
            normalized.get("forbidden_path_roots"),
            "forbidden_path_roots",
        )
        forbidden_parts = _normalize_ag2_policy_gate_tuple(
            normalized.get("forbidden_path_parts"),
            "forbidden_path_parts",
        )
        forbidden_fields = _normalize_ag2_policy_gate_tuple(
            normalized.get("forbidden_payload_fields"),
            "forbidden_payload_fields",
        )
        if normalized.get("contract_scope_required") != "host_local_config_no_sync":
            raise ValueError(
                "AG2 real runtime host_local_config_format_policy contract_scope_required must be host_local_config_no_sync"
            )
        if forbidden_roots != AG2_REAL_RUNTIME_CONFIG_FORBIDDEN_PATH_ROOTS:
            raise ValueError("AG2 real runtime host_local_config_format_policy forbidden roots must match policy")
        if forbidden_parts != AG2_REAL_RUNTIME_CONFIG_FORBIDDEN_PATH_PARTS:
            raise ValueError("AG2 real runtime host_local_config_format_policy forbidden path parts must match policy")
        if forbidden_fields != AG2_REAL_RUNTIME_FORMAT_FORBIDDEN_PAYLOAD_FIELDS:
            raise ValueError("AG2 real runtime host_local_config_format_policy forbidden fields must match policy")
        normalized["forbidden_path_roots"] = list(forbidden_roots)
        normalized["forbidden_path_parts"] = list(forbidden_parts)
        normalized["forbidden_payload_fields"] = list(forbidden_fields)
    normalized["decision_reason"] = normalized["decision_reason"].strip()
    return normalized


@dataclass(frozen=True)
class Ag2DependencyReviewRecord:
    decision_state: str = "blocked_pending_policy"
    package_name: str = "ag2"
    import_name: str = "autogen"
    review_scope: str = "optional_dependency_no_live"
    required_gates: tuple[str, ...] = AG2_DEPENDENCY_REVIEW_REQUIRED_GATES
    source_urls: tuple[str, ...] = AG2_DEPENDENCY_REVIEW_SOURCE_URLS
    reference_commit: str = ""
    notes: tuple[str, ...] = ()
    would_install_dependency: bool = False
    would_import_ag2: bool = False
    would_execute_live: bool = False
    provider_or_model_config_required: bool = False

    def __post_init__(self) -> None:
        if self.decision_state not in AG2_DEPENDENCY_REVIEW_DECISION_STATES:
            raise ValueError(f"unsupported AG2 dependency review decision_state: {self.decision_state}")
        if self.package_name != "ag2":
            raise ValueError("AG2 dependency review package_name must be ag2")
        if self.import_name != "autogen":
            raise ValueError("AG2 dependency review import_name must be autogen")
        if self.review_scope != "optional_dependency_no_live":
            raise ValueError("AG2 dependency review scope must be optional_dependency_no_live")
        required_gates = self._normalize_string_tuple(self.required_gates, "required_gates")
        source_urls = self._normalize_string_tuple(self.source_urls, "source_urls")
        notes = self._normalize_string_tuple(self.notes, "notes", allow_empty=True)
        if required_gates != AG2_DEPENDENCY_REVIEW_REQUIRED_GATES:
            raise ValueError("AG2 dependency review required_gates must match the approved gate list")
        if source_urls != AG2_DEPENDENCY_REVIEW_SOURCE_URLS:
            raise ValueError("AG2 dependency review source_urls must match the approved official references")
        for field_name in (
            "would_install_dependency",
            "would_import_ag2",
            "would_execute_live",
            "provider_or_model_config_required",
        ):
            if getattr(self, field_name) is not False:
                raise ValueError(f"AG2 dependency review {field_name} must remain false")
        if not isinstance(self.reference_commit, str):
            raise ValueError("AG2 dependency review reference_commit must be a string")
        object.__setattr__(self, "required_gates", required_gates)
        object.__setattr__(self, "source_urls", source_urls)
        object.__setattr__(self, "notes", notes)
        object.__setattr__(self, "reference_commit", self.reference_commit.strip())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Ag2DependencyReviewRecord":
        if not isinstance(payload, dict):
            raise ValueError("AG2 dependency review payload must be an object")
        unsupported = sorted(set(payload) - AG2_DEPENDENCY_REVIEW_RECORD_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported AG2 dependency review fields: {', '.join(unsupported)}")
        return cls(**payload)

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision_state": self.decision_state,
            "package_name": self.package_name,
            "import_name": self.import_name,
            "review_scope": self.review_scope,
            "required_gates": list(self.required_gates),
            "source_urls": list(self.source_urls),
            "reference_commit": self.reference_commit,
            "notes": list(self.notes),
            "would_install_dependency": self.would_install_dependency,
            "would_import_ag2": self.would_import_ag2,
            "would_execute_live": self.would_execute_live,
            "provider_or_model_config_required": self.provider_or_model_config_required,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()

    @staticmethod
    def _normalize_string_tuple(value: Any, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"AG2 dependency review {label} must be a list")
        if not value and not allow_empty:
            raise ValueError(f"AG2 dependency review {label} must be non-empty")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"AG2 dependency review {label} must contain non-empty strings")
            normalized.append(item.strip())
        return tuple(normalized)


@dataclass(frozen=True)
class Ag2OptionalImportGate:
    decision_state: str = "blocked_pending_policy"
    package_name: str = "ag2"
    import_name: str = "autogen"
    gate_scope: str = "optional_import_gate_no_live"
    required_gates: tuple[str, ...] = AG2_DEPENDENCY_REVIEW_REQUIRED_GATES
    satisfied_gates: tuple[str, ...] = ()
    missing_gates: tuple[str, ...] | None = None
    import_allowed: bool = False
    decision_reason: str = "AG2 optional import remains blocked pending policy gates."
    would_install_dependency: bool = False
    would_import_ag2: bool = False
    would_execute_live: bool = False
    provider_or_model_config_required: bool = False

    def __post_init__(self) -> None:
        if self.decision_state not in AG2_OPTIONAL_IMPORT_GATE_DECISION_STATES:
            raise ValueError(f"unsupported AG2 optional import gate decision_state: {self.decision_state}")
        if self.package_name != "ag2":
            raise ValueError("AG2 optional import gate package_name must be ag2")
        if self.import_name != "autogen":
            raise ValueError("AG2 optional import gate import_name must be autogen")
        if self.gate_scope != "optional_import_gate_no_live":
            raise ValueError("AG2 optional import gate scope must be optional_import_gate_no_live")
        required_gates = Ag2DependencyReviewRecord._normalize_string_tuple(self.required_gates, "required_gates")
        satisfied_gates = Ag2DependencyReviewRecord._normalize_string_tuple(
            self.satisfied_gates, "satisfied_gates", allow_empty=True
        )
        if required_gates != AG2_DEPENDENCY_REVIEW_REQUIRED_GATES:
            raise ValueError("AG2 optional import gate required_gates must match the approved gate list")
        unsupported_satisfied = sorted(set(satisfied_gates) - set(required_gates))
        if unsupported_satisfied:
            raise ValueError(f"unsupported AG2 optional import satisfied_gates: {', '.join(unsupported_satisfied)}")
        expected_missing = tuple(gate for gate in required_gates if gate not in satisfied_gates)
        if self.missing_gates is None:
            missing_gates = expected_missing
        else:
            missing_gates = Ag2DependencyReviewRecord._normalize_string_tuple(
                self.missing_gates, "missing_gates", allow_empty=True
            )
        if missing_gates != expected_missing:
            raise ValueError("AG2 optional import gate missing_gates must match unsatisfied required gates")
        expected_allowed = self.decision_state == "allowed" and not expected_missing
        if self.import_allowed is not expected_allowed:
            raise ValueError("AG2 optional import gate import_allowed must match decision_state and missing_gates")
        for field_name in (
            "would_install_dependency",
            "would_import_ag2",
            "would_execute_live",
            "provider_or_model_config_required",
        ):
            if getattr(self, field_name) is not False:
                raise ValueError(f"AG2 optional import gate {field_name} must remain false")
        if not isinstance(self.decision_reason, str) or not self.decision_reason.strip():
            raise ValueError("AG2 optional import gate decision_reason must be a non-empty string")
        object.__setattr__(self, "required_gates", required_gates)
        object.__setattr__(self, "satisfied_gates", satisfied_gates)
        object.__setattr__(self, "missing_gates", missing_gates)
        object.__setattr__(self, "decision_reason", self.decision_reason.strip())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Ag2OptionalImportGate":
        if not isinstance(payload, dict):
            raise ValueError("AG2 optional import gate payload must be an object")
        unsupported = sorted(set(payload) - AG2_OPTIONAL_IMPORT_GATE_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported AG2 optional import gate fields: {', '.join(unsupported)}")
        return cls(**payload)

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision_state": self.decision_state,
            "package_name": self.package_name,
            "import_name": self.import_name,
            "gate_scope": self.gate_scope,
            "required_gates": list(self.required_gates),
            "satisfied_gates": list(self.satisfied_gates),
            "missing_gates": list(self.missing_gates),
            "import_allowed": self.import_allowed,
            "decision_reason": self.decision_reason,
            "would_install_dependency": self.would_install_dependency,
            "would_import_ag2": self.would_import_ag2,
            "would_execute_live": self.would_execute_live,
            "provider_or_model_config_required": self.provider_or_model_config_required,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()


@dataclass(frozen=True)
class Ag2RuntimeImportPolicy:
    schema_version: int = SCHEMA_VERSION
    policy_kind: str = AG2_RUNTIME_IMPORT_POLICY_KIND
    policy_scope: str = "runtime_import_policy_no_live"
    policy_state: str = "blocked_pending_policy"
    approval_required: bool = True
    approval_artifact_required: bool = True
    human_approval_required: bool = True
    runtime_import_allowed: bool = False
    decision_reason: str = "AG2 runtime import is blocked-by-default until human policy approval is recorded."
    would_install_dependency: bool = False
    would_import_ag2: bool = False
    would_execute_live: bool = False
    provider_or_model_config_required: bool = False
    ag2_import_gate: Ag2OptionalImportGate | dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("AG2 runtime import policy schema_version must match the meeting schema version")
        if self.policy_kind != AG2_RUNTIME_IMPORT_POLICY_KIND:
            raise ValueError(f"AG2 runtime import policy kind must be {AG2_RUNTIME_IMPORT_POLICY_KIND}")
        if self.policy_scope != "runtime_import_policy_no_live":
            raise ValueError("AG2 runtime import policy scope must be runtime_import_policy_no_live")
        if self.policy_state not in AG2_RUNTIME_IMPORT_POLICY_STATES:
            raise ValueError(f"unsupported AG2 runtime import policy_state: {self.policy_state}")
        for field_name in ("approval_required", "approval_artifact_required", "human_approval_required"):
            if getattr(self, field_name) is not True:
                raise ValueError(f"AG2 runtime import policy {field_name} must remain true")
        for field_name in (
            "runtime_import_allowed",
            "would_install_dependency",
            "would_import_ag2",
            "would_execute_live",
            "provider_or_model_config_required",
        ):
            if getattr(self, field_name) is not False:
                raise ValueError(f"AG2 runtime import policy {field_name} must remain false")
        if not isinstance(self.decision_reason, str) or not self.decision_reason.strip():
            raise ValueError("AG2 runtime import policy decision_reason must be a non-empty string")
        gate = self.ag2_import_gate
        if gate is None:
            normalized_gate = Ag2OptionalImportGate()
        elif isinstance(gate, Ag2OptionalImportGate):
            normalized_gate = gate
        elif isinstance(gate, dict):
            normalized_gate = Ag2OptionalImportGate.from_payload(gate)
        else:
            raise ValueError("AG2 runtime import policy ag2_import_gate must be an object")
        if normalized_gate.import_allowed:
            raise ValueError("AG2 runtime import policy cannot carry an import-allowed gate")
        object.__setattr__(self, "decision_reason", self.decision_reason.strip())
        object.__setattr__(self, "ag2_import_gate", normalized_gate)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Ag2RuntimeImportPolicy":
        if not isinstance(payload, dict):
            raise ValueError("AG2 runtime import policy payload must be an object")
        unsupported = sorted(set(payload) - AG2_RUNTIME_IMPORT_POLICY_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported AG2 runtime import policy fields: {', '.join(unsupported)}")
        return cls(
            schema_version=payload.get("schema_version", SCHEMA_VERSION),
            policy_kind=payload.get("policy_kind", AG2_RUNTIME_IMPORT_POLICY_KIND),
            policy_scope=payload.get("policy_scope", "runtime_import_policy_no_live"),
            policy_state=payload.get("policy_state", "blocked_pending_policy"),
            approval_required=payload.get("approval_required", True),
            approval_artifact_required=payload.get("approval_artifact_required", True),
            human_approval_required=payload.get("human_approval_required", True),
            runtime_import_allowed=payload.get("runtime_import_allowed", False),
            decision_reason=payload.get(
                "decision_reason",
                "AG2 runtime import is blocked-by-default until human policy approval is recorded.",
            ),
            would_install_dependency=payload.get("would_install_dependency", False),
            would_import_ag2=payload.get("would_import_ag2", False),
            would_execute_live=payload.get("would_execute_live", False),
            provider_or_model_config_required=payload.get("provider_or_model_config_required", False),
            ag2_import_gate=payload.get("ag2_import_gate"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_kind": self.policy_kind,
            "policy_scope": self.policy_scope,
            "policy_state": self.policy_state,
            "approval_required": self.approval_required,
            "approval_artifact_required": self.approval_artifact_required,
            "human_approval_required": self.human_approval_required,
            "runtime_import_allowed": self.runtime_import_allowed,
            "decision_reason": self.decision_reason,
            "would_install_dependency": self.would_install_dependency,
            "would_import_ag2": self.would_import_ag2,
            "would_execute_live": self.would_execute_live,
            "provider_or_model_config_required": self.provider_or_model_config_required,
            "ag2_import_gate": self.ag2_import_gate.to_payload(),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()


@dataclass(frozen=True)
class Ag2HostLocalAdapterPlan:
    schema_version: int = SCHEMA_VERSION
    plan_kind: str = AG2_HOST_LOCAL_ADAPTER_PLAN_KIND
    plan_scope: str = "host_local_adapter_plan_no_live"
    plan_state: str = "blocked_pending_policy"
    host_local_only: bool = True
    runtime_import_policy_required: bool = True
    runtime_import_policy_gate_satisfied: bool = False
    adapter_enabled: bool = False
    adapter_eligible: bool = False
    supports_live_submit: bool = False
    dispatches_via_control_plane: bool = False
    uses_tmux_substrate: bool = False
    uses_model_routing: bool = False
    model_routing_disabled_by_default: bool = True
    decision_reason: str = "AG2 host-local adapter plan is disabled by default and remains advisory only."
    would_install_dependency: bool = False
    would_import_ag2: bool = False
    would_execute_live: bool = False
    provider_or_model_config_required: bool = False
    runtime_import_policy: Ag2RuntimeImportPolicy | dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("AG2 host-local adapter plan schema_version must match the meeting schema version")
        if self.plan_kind != AG2_HOST_LOCAL_ADAPTER_PLAN_KIND:
            raise ValueError(f"AG2 host-local adapter plan kind must be {AG2_HOST_LOCAL_ADAPTER_PLAN_KIND}")
        if self.plan_scope != "host_local_adapter_plan_no_live":
            raise ValueError("AG2 host-local adapter plan scope must be host_local_adapter_plan_no_live")
        if self.plan_state not in AG2_HOST_LOCAL_ADAPTER_PLAN_STATES:
            raise ValueError(f"unsupported AG2 host-local adapter plan_state: {self.plan_state}")
        for field_name in ("host_local_only", "runtime_import_policy_required", "model_routing_disabled_by_default"):
            if getattr(self, field_name) is not True:
                raise ValueError(f"AG2 host-local adapter plan {field_name} must remain true")
        for field_name in (
            "runtime_import_policy_gate_satisfied",
            "adapter_enabled",
            "adapter_eligible",
            "supports_live_submit",
            "dispatches_via_control_plane",
            "uses_tmux_substrate",
            "uses_model_routing",
            "would_install_dependency",
            "would_import_ag2",
            "would_execute_live",
            "provider_or_model_config_required",
        ):
            if getattr(self, field_name) is not False:
                raise ValueError(f"AG2 host-local adapter plan {field_name} must remain false")
        if not isinstance(self.decision_reason, str) or not self.decision_reason.strip():
            raise ValueError("AG2 host-local adapter plan decision_reason must be a non-empty string")
        runtime_policy = self.runtime_import_policy
        if runtime_policy is None:
            normalized_policy = Ag2RuntimeImportPolicy()
        elif isinstance(runtime_policy, Ag2RuntimeImportPolicy):
            normalized_policy = runtime_policy
        elif isinstance(runtime_policy, dict):
            normalized_policy = Ag2RuntimeImportPolicy.from_payload(runtime_policy)
        else:
            raise ValueError("AG2 host-local adapter plan runtime_import_policy must be an object")
        if normalized_policy.runtime_import_allowed:
            raise ValueError("AG2 host-local adapter plan cannot carry an import-allowed runtime policy")
        object.__setattr__(self, "decision_reason", self.decision_reason.strip())
        object.__setattr__(self, "runtime_import_policy", normalized_policy)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Ag2HostLocalAdapterPlan":
        if not isinstance(payload, dict):
            raise ValueError("AG2 host-local adapter plan payload must be an object")
        unsupported = sorted(set(payload) - AG2_HOST_LOCAL_ADAPTER_PLAN_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported AG2 host-local adapter plan fields: {', '.join(unsupported)}")
        return cls(
            schema_version=payload.get("schema_version", SCHEMA_VERSION),
            plan_kind=payload.get("plan_kind", AG2_HOST_LOCAL_ADAPTER_PLAN_KIND),
            plan_scope=payload.get("plan_scope", "host_local_adapter_plan_no_live"),
            plan_state=payload.get("plan_state", "blocked_pending_policy"),
            host_local_only=payload.get("host_local_only", True),
            runtime_import_policy_required=payload.get("runtime_import_policy_required", True),
            runtime_import_policy_gate_satisfied=payload.get("runtime_import_policy_gate_satisfied", False),
            adapter_enabled=payload.get("adapter_enabled", False),
            adapter_eligible=payload.get("adapter_eligible", False),
            supports_live_submit=payload.get("supports_live_submit", False),
            dispatches_via_control_plane=payload.get("dispatches_via_control_plane", False),
            uses_tmux_substrate=payload.get("uses_tmux_substrate", False),
            uses_model_routing=payload.get("uses_model_routing", False),
            model_routing_disabled_by_default=payload.get("model_routing_disabled_by_default", True),
            decision_reason=payload.get(
                "decision_reason",
                "AG2 host-local adapter plan is disabled by default and remains advisory only.",
            ),
            would_install_dependency=payload.get("would_install_dependency", False),
            would_import_ag2=payload.get("would_import_ag2", False),
            would_execute_live=payload.get("would_execute_live", False),
            provider_or_model_config_required=payload.get("provider_or_model_config_required", False),
            runtime_import_policy=payload.get("runtime_import_policy"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_kind": self.plan_kind,
            "plan_scope": self.plan_scope,
            "plan_state": self.plan_state,
            "host_local_only": self.host_local_only,
            "runtime_import_policy_required": self.runtime_import_policy_required,
            "runtime_import_policy_gate_satisfied": self.runtime_import_policy_gate_satisfied,
            "adapter_enabled": self.adapter_enabled,
            "adapter_eligible": self.adapter_eligible,
            "supports_live_submit": self.supports_live_submit,
            "dispatches_via_control_plane": self.dispatches_via_control_plane,
            "uses_tmux_substrate": self.uses_tmux_substrate,
            "uses_model_routing": self.uses_model_routing,
            "model_routing_disabled_by_default": self.model_routing_disabled_by_default,
            "decision_reason": self.decision_reason,
            "would_install_dependency": self.would_install_dependency,
            "would_import_ag2": self.would_import_ag2,
            "would_execute_live": self.would_execute_live,
            "provider_or_model_config_required": self.provider_or_model_config_required,
            "runtime_import_policy": self.runtime_import_policy.to_payload(),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()


@dataclass(frozen=True)
class Ag2HumanApprovalArtifact:
    schema_version: int = SCHEMA_VERSION
    artifact_kind: str = AG2_HUMAN_APPROVAL_ARTIFACT_KIND
    approval_scope: str = "runtime_import_policy_no_live"
    approval_state: str = "absent"
    approval_id: str = ""
    approved_by: str = ""
    approved_at: str = ""
    expires_at: str = ""
    policy_snapshot_sha256: str = ""
    decision_reason: str = "AG2 human approval artifact is absent; runtime import remains blocked."
    advisory_only: bool = True
    runtime_import_allowed: bool = False
    adapter_eligibility_granted: bool = False
    would_install_dependency: bool = False
    would_import_ag2: bool = False
    would_execute_live: bool = False
    provider_or_model_config_required: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("AG2 human approval artifact schema_version must match the meeting schema version")
        if self.artifact_kind != AG2_HUMAN_APPROVAL_ARTIFACT_KIND:
            raise ValueError(f"AG2 human approval artifact kind must be {AG2_HUMAN_APPROVAL_ARTIFACT_KIND}")
        if self.approval_scope != "runtime_import_policy_no_live":
            raise ValueError("AG2 human approval artifact scope must be runtime_import_policy_no_live")
        if self.approval_state not in AG2_HUMAN_APPROVAL_ARTIFACT_STATES:
            raise ValueError(f"unsupported AG2 human approval artifact approval_state: {self.approval_state}")
        if self.advisory_only is not True:
            raise ValueError("AG2 human approval artifact advisory_only must remain true")
        for field_name in (
            "runtime_import_allowed",
            "adapter_eligibility_granted",
            "would_install_dependency",
            "would_import_ag2",
            "would_execute_live",
            "provider_or_model_config_required",
        ):
            if getattr(self, field_name) is not False:
                raise ValueError(f"AG2 human approval artifact {field_name} must remain false")
        for field_name in ("approval_id", "approved_by", "approved_at", "expires_at", "policy_snapshot_sha256"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValueError(f"AG2 human approval artifact {field_name} must be a string")
            if value.strip():
                raise ValueError(f"AG2 human approval artifact {field_name} must remain empty in Phase 20")
            object.__setattr__(self, field_name, "")
        if not isinstance(self.decision_reason, str) or not self.decision_reason.strip():
            raise ValueError("AG2 human approval artifact decision_reason must be a non-empty string")
        object.__setattr__(self, "decision_reason", self.decision_reason.strip())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Ag2HumanApprovalArtifact":
        if not isinstance(payload, dict):
            raise ValueError("AG2 human approval artifact payload must be an object")
        unsupported = sorted(set(payload) - AG2_HUMAN_APPROVAL_ARTIFACT_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported AG2 human approval artifact fields: {', '.join(unsupported)}")
        return cls(
            schema_version=payload.get("schema_version", SCHEMA_VERSION),
            artifact_kind=payload.get("artifact_kind", AG2_HUMAN_APPROVAL_ARTIFACT_KIND),
            approval_scope=payload.get("approval_scope", "runtime_import_policy_no_live"),
            approval_state=payload.get("approval_state", "absent"),
            approval_id=payload.get("approval_id", ""),
            approved_by=payload.get("approved_by", ""),
            approved_at=payload.get("approved_at", ""),
            expires_at=payload.get("expires_at", ""),
            policy_snapshot_sha256=payload.get("policy_snapshot_sha256", ""),
            decision_reason=payload.get(
                "decision_reason",
                "AG2 human approval artifact is absent; runtime import remains blocked.",
            ),
            advisory_only=payload.get("advisory_only", True),
            runtime_import_allowed=payload.get("runtime_import_allowed", False),
            adapter_eligibility_granted=payload.get("adapter_eligibility_granted", False),
            would_install_dependency=payload.get("would_install_dependency", False),
            would_import_ag2=payload.get("would_import_ag2", False),
            would_execute_live=payload.get("would_execute_live", False),
            provider_or_model_config_required=payload.get("provider_or_model_config_required", False),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_kind": self.artifact_kind,
            "approval_scope": self.approval_scope,
            "approval_state": self.approval_state,
            "approval_id": self.approval_id,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "expires_at": self.expires_at,
            "policy_snapshot_sha256": self.policy_snapshot_sha256,
            "decision_reason": self.decision_reason,
            "advisory_only": self.advisory_only,
            "runtime_import_allowed": self.runtime_import_allowed,
            "adapter_eligibility_granted": self.adapter_eligibility_granted,
            "would_install_dependency": self.would_install_dependency,
            "would_import_ag2": self.would_import_ag2,
            "would_execute_live": self.would_execute_live,
            "provider_or_model_config_required": self.provider_or_model_config_required,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()


@dataclass(frozen=True)
class Ag2HostLocalConfigContract:
    schema_version: int = SCHEMA_VERSION
    contract_kind: str = AG2_HOST_LOCAL_CONFIG_CONTRACT_KIND
    contract_scope: str = "host_local_config_no_sync"
    contract_state: str = "absent"
    host_local_only: bool = True
    advisory_only: bool = True
    config_reference: str = ""
    config_values_recorded: bool = False
    provider_config_present: bool = False
    model_config_present: bool = False
    endpoint_config_present: bool = False
    secret_config_present: bool = False
    synced_to_repo: bool = False
    model_routing_disabled_by_default: bool = True
    adapter_eligibility_granted: bool = False
    decision_reason: str = "AG2 host-local config contract is absent; no synced config values are recorded."
    would_install_dependency: bool = False
    would_import_ag2: bool = False
    would_execute_live: bool = False
    provider_or_model_config_required: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("AG2 host-local config contract schema_version must match the meeting schema version")
        if self.contract_kind != AG2_HOST_LOCAL_CONFIG_CONTRACT_KIND:
            raise ValueError(f"AG2 host-local config contract kind must be {AG2_HOST_LOCAL_CONFIG_CONTRACT_KIND}")
        if self.contract_scope != "host_local_config_no_sync":
            raise ValueError("AG2 host-local config contract scope must be host_local_config_no_sync")
        if self.contract_state not in AG2_HOST_LOCAL_CONFIG_CONTRACT_STATES:
            raise ValueError(f"unsupported AG2 host-local config contract_state: {self.contract_state}")
        for field_name in ("host_local_only", "advisory_only", "model_routing_disabled_by_default"):
            if getattr(self, field_name) is not True:
                raise ValueError(f"AG2 host-local config contract {field_name} must remain true")
        for field_name in (
            "config_values_recorded",
            "provider_config_present",
            "model_config_present",
            "endpoint_config_present",
            "secret_config_present",
            "synced_to_repo",
            "adapter_eligibility_granted",
            "would_install_dependency",
            "would_import_ag2",
            "would_execute_live",
            "provider_or_model_config_required",
        ):
            if getattr(self, field_name) is not False:
                raise ValueError(f"AG2 host-local config contract {field_name} must remain false")
        if not isinstance(self.config_reference, str):
            raise ValueError("AG2 host-local config contract config_reference must be a string")
        if self.config_reference.strip():
            raise ValueError("AG2 host-local config contract config_reference must remain empty in Phase 20")
        if not isinstance(self.decision_reason, str) or not self.decision_reason.strip():
            raise ValueError("AG2 host-local config contract decision_reason must be a non-empty string")
        object.__setattr__(self, "config_reference", "")
        object.__setattr__(self, "decision_reason", self.decision_reason.strip())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Ag2HostLocalConfigContract":
        if not isinstance(payload, dict):
            raise ValueError("AG2 host-local config contract payload must be an object")
        unsupported = sorted(set(payload) - AG2_HOST_LOCAL_CONFIG_CONTRACT_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported AG2 host-local config contract fields: {', '.join(unsupported)}")
        return cls(
            schema_version=payload.get("schema_version", SCHEMA_VERSION),
            contract_kind=payload.get("contract_kind", AG2_HOST_LOCAL_CONFIG_CONTRACT_KIND),
            contract_scope=payload.get("contract_scope", "host_local_config_no_sync"),
            contract_state=payload.get("contract_state", "absent"),
            host_local_only=payload.get("host_local_only", True),
            advisory_only=payload.get("advisory_only", True),
            config_reference=payload.get("config_reference", ""),
            config_values_recorded=payload.get("config_values_recorded", False),
            provider_config_present=payload.get("provider_config_present", False),
            model_config_present=payload.get("model_config_present", False),
            endpoint_config_present=payload.get("endpoint_config_present", False),
            secret_config_present=payload.get("secret_config_present", False),
            synced_to_repo=payload.get("synced_to_repo", False),
            model_routing_disabled_by_default=payload.get("model_routing_disabled_by_default", True),
            adapter_eligibility_granted=payload.get("adapter_eligibility_granted", False),
            decision_reason=payload.get(
                "decision_reason",
                "AG2 host-local config contract is absent; no synced config values are recorded.",
            ),
            would_install_dependency=payload.get("would_install_dependency", False),
            would_import_ag2=payload.get("would_import_ag2", False),
            would_execute_live=payload.get("would_execute_live", False),
            provider_or_model_config_required=payload.get("provider_or_model_config_required", False),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_kind": self.contract_kind,
            "contract_scope": self.contract_scope,
            "contract_state": self.contract_state,
            "host_local_only": self.host_local_only,
            "advisory_only": self.advisory_only,
            "config_reference": self.config_reference,
            "config_values_recorded": self.config_values_recorded,
            "provider_config_present": self.provider_config_present,
            "model_config_present": self.model_config_present,
            "endpoint_config_present": self.endpoint_config_present,
            "secret_config_present": self.secret_config_present,
            "synced_to_repo": self.synced_to_repo,
            "model_routing_disabled_by_default": self.model_routing_disabled_by_default,
            "adapter_eligibility_granted": self.adapter_eligibility_granted,
            "decision_reason": self.decision_reason,
            "would_install_dependency": self.would_install_dependency,
            "would_import_ag2": self.would_import_ag2,
            "would_execute_live": self.would_execute_live,
            "provider_or_model_config_required": self.provider_or_model_config_required,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()


@dataclass(frozen=True)
class Ag2RealRuntimePolicyArtifact:
    schema_version: int = SCHEMA_VERSION
    policy_kind: str = AG2_REAL_RUNTIME_POLICY_ARTIFACT_KIND
    policy_scope: str = "real_runtime_policy_design_no_live"
    policy_state: str = "blocked_pending_policy"
    decision_reason: str = "Real AG2 runtime policy is incomplete; runtime remains blocked."
    required_policy_gates: tuple[str, ...] = AG2_REAL_RUNTIME_PREFLIGHT_MISSING_GATES
    missing_gates: tuple[str, ...] = AG2_REAL_RUNTIME_PREFLIGHT_MISSING_GATES
    satisfied_gates: tuple[str, ...] = ()
    requires_human_approval: bool = True
    requires_host_local_config: bool = True
    provider_policy: dict[str, Any] = field(default_factory=_ag2_real_runtime_provider_policy)
    model_policy: dict[str, Any] = field(default_factory=_ag2_real_runtime_model_policy)
    redaction_policy: dict[str, Any] = field(default_factory=_ag2_real_runtime_redaction_policy)
    cost_policy: dict[str, Any] = field(default_factory=_ag2_real_runtime_cost_policy)
    timeout_policy: dict[str, Any] = field(default_factory=_ag2_real_runtime_timeout_policy)
    sync_boundary_policy: dict[str, Any] = field(default_factory=_ag2_real_runtime_sync_boundary_policy)
    approval_format_policy: dict[str, Any] = field(default_factory=_ag2_real_runtime_approval_format_policy)
    host_local_config_format_policy: dict[str, Any] = field(
        default_factory=_ag2_real_runtime_host_local_config_format_policy
    )
    would_install_dependency: bool = False
    would_import_ag2: bool = False
    would_execute_live: bool = False
    provider_or_model_config_required: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("AG2 real runtime policy artifact schema_version must match the meeting schema version")
        if self.policy_kind != AG2_REAL_RUNTIME_POLICY_ARTIFACT_KIND:
            raise ValueError(f"AG2 real runtime policy artifact kind must be {AG2_REAL_RUNTIME_POLICY_ARTIFACT_KIND}")
        if self.policy_scope != "real_runtime_policy_design_no_live":
            raise ValueError("AG2 real runtime policy artifact scope must be real_runtime_policy_design_no_live")
        if self.policy_state not in AG2_REAL_RUNTIME_POLICY_ARTIFACT_STATES:
            raise ValueError(f"unsupported AG2 real runtime policy_state: {self.policy_state}")
        if not isinstance(self.decision_reason, str) or not self.decision_reason.strip():
            raise ValueError("AG2 real runtime policy artifact decision_reason must be a non-empty string")
        for field_name in ("requires_human_approval", "requires_host_local_config"):
            if getattr(self, field_name) is not True:
                raise ValueError(f"AG2 real runtime policy artifact {field_name} must remain true")
        for field_name in (
            "would_install_dependency",
            "would_import_ag2",
            "would_execute_live",
            "provider_or_model_config_required",
        ):
            if getattr(self, field_name) is not False:
                raise ValueError(f"AG2 real runtime policy artifact {field_name} must remain false")
        required_policy_gates = _normalize_ag2_policy_gate_tuple(
            self.required_policy_gates,
            "required_policy_gates",
        )
        missing_gates = _normalize_ag2_policy_gate_tuple(self.missing_gates, "missing_gates")
        satisfied_gates = _normalize_ag2_policy_gate_tuple(self.satisfied_gates, "satisfied_gates", allow_empty=True)
        if required_policy_gates != AG2_REAL_RUNTIME_PREFLIGHT_MISSING_GATES:
            raise ValueError("AG2 real runtime policy artifact required_policy_gates must match preflight gates")
        if missing_gates != AG2_REAL_RUNTIME_PREFLIGHT_MISSING_GATES:
            raise ValueError("AG2 real runtime policy artifact missing_gates must match preflight gates")
        if satisfied_gates:
            raise ValueError("AG2 real runtime policy artifact satisfied_gates must remain empty")
        normalized_sections = {
            "provider_policy": _validate_ag2_real_runtime_policy_section("provider_policy", self.provider_policy),
            "model_policy": _validate_ag2_real_runtime_policy_section("model_policy", self.model_policy),
            "redaction_policy": _validate_ag2_real_runtime_policy_section("redaction_policy", self.redaction_policy),
            "cost_policy": _validate_ag2_real_runtime_policy_section("cost_policy", self.cost_policy),
            "timeout_policy": _validate_ag2_real_runtime_policy_section("timeout_policy", self.timeout_policy),
            "sync_boundary_policy": _validate_ag2_real_runtime_policy_section(
                "sync_boundary_policy",
                self.sync_boundary_policy,
            ),
            "approval_format_policy": _validate_ag2_real_runtime_policy_section(
                "approval_format_policy",
                self.approval_format_policy,
            ),
            "host_local_config_format_policy": _validate_ag2_real_runtime_policy_section(
                "host_local_config_format_policy",
                self.host_local_config_format_policy,
            ),
        }
        object.__setattr__(self, "decision_reason", self.decision_reason.strip())
        object.__setattr__(self, "required_policy_gates", required_policy_gates)
        object.__setattr__(self, "missing_gates", missing_gates)
        object.__setattr__(self, "satisfied_gates", satisfied_gates)
        for section_name, section_payload in normalized_sections.items():
            object.__setattr__(self, section_name, section_payload)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Ag2RealRuntimePolicyArtifact":
        if not isinstance(payload, dict):
            raise ValueError("AG2 real runtime policy artifact payload must be an object")
        unsupported = sorted(set(payload) - AG2_REAL_RUNTIME_POLICY_ARTIFACT_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported AG2 real runtime policy artifact fields: {', '.join(unsupported)}")
        return cls(
            schema_version=payload.get("schema_version", SCHEMA_VERSION),
            policy_kind=payload.get("policy_kind", AG2_REAL_RUNTIME_POLICY_ARTIFACT_KIND),
            policy_scope=payload.get("policy_scope", "real_runtime_policy_design_no_live"),
            policy_state=payload.get("policy_state", "blocked_pending_policy"),
            decision_reason=payload.get(
                "decision_reason",
                "Real AG2 runtime policy is incomplete; runtime remains blocked.",
            ),
            required_policy_gates=payload.get("required_policy_gates", AG2_REAL_RUNTIME_PREFLIGHT_MISSING_GATES),
            missing_gates=payload.get("missing_gates", AG2_REAL_RUNTIME_PREFLIGHT_MISSING_GATES),
            satisfied_gates=payload.get("satisfied_gates", ()),
            requires_human_approval=payload.get("requires_human_approval", True),
            requires_host_local_config=payload.get("requires_host_local_config", True),
            provider_policy=payload.get("provider_policy", _ag2_real_runtime_provider_policy()),
            model_policy=payload.get("model_policy", _ag2_real_runtime_model_policy()),
            redaction_policy=payload.get("redaction_policy", _ag2_real_runtime_redaction_policy()),
            cost_policy=payload.get("cost_policy", _ag2_real_runtime_cost_policy()),
            timeout_policy=payload.get("timeout_policy", _ag2_real_runtime_timeout_policy()),
            sync_boundary_policy=payload.get("sync_boundary_policy", _ag2_real_runtime_sync_boundary_policy()),
            approval_format_policy=payload.get("approval_format_policy", _ag2_real_runtime_approval_format_policy()),
            host_local_config_format_policy=payload.get(
                "host_local_config_format_policy",
                _ag2_real_runtime_host_local_config_format_policy(),
            ),
            would_install_dependency=payload.get("would_install_dependency", False),
            would_import_ag2=payload.get("would_import_ag2", False),
            would_execute_live=payload.get("would_execute_live", False),
            provider_or_model_config_required=payload.get("provider_or_model_config_required", False),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_kind": self.policy_kind,
            "policy_scope": self.policy_scope,
            "policy_state": self.policy_state,
            "decision_reason": self.decision_reason,
            "required_policy_gates": list(self.required_policy_gates),
            "missing_gates": list(self.missing_gates),
            "satisfied_gates": list(self.satisfied_gates),
            "requires_human_approval": self.requires_human_approval,
            "requires_host_local_config": self.requires_host_local_config,
            "provider_policy": dict(self.provider_policy),
            "model_policy": dict(self.model_policy),
            "redaction_policy": dict(self.redaction_policy),
            "cost_policy": dict(self.cost_policy),
            "timeout_policy": dict(self.timeout_policy),
            "sync_boundary_policy": dict(self.sync_boundary_policy),
            "approval_format_policy": dict(self.approval_format_policy),
            "host_local_config_format_policy": dict(self.host_local_config_format_policy),
            "would_install_dependency": self.would_install_dependency,
            "would_import_ag2": self.would_import_ag2,
            "would_execute_live": self.would_execute_live,
            "provider_or_model_config_required": self.provider_or_model_config_required,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()


@dataclass(frozen=True)
class MeetingSynthesisSummary:
    verdict: str
    risks: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    recommended_next_actions: tuple[str, ...] = ()
    method: str = MEETING_SYNTHESIS_METHOD

    def __post_init__(self) -> None:
        if self.method != MEETING_SYNTHESIS_METHOD:
            raise ValueError("meeting synthesis method must be deterministic")
        verdict = self._normalize_text(self.verdict, "verdict")
        risks = self._normalize_string_tuple(self.risks, "risks", allow_empty=True)
        open_questions = self._normalize_string_tuple(self.open_questions, "open_questions", allow_empty=True)
        recommended_next_actions = self._normalize_string_tuple(
            self.recommended_next_actions,
            "recommended_next_actions",
            allow_empty=True,
        )
        object.__setattr__(self, "verdict", verdict)
        object.__setattr__(self, "risks", risks)
        object.__setattr__(self, "open_questions", open_questions)
        object.__setattr__(self, "recommended_next_actions", recommended_next_actions)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MeetingSynthesisSummary":
        if not isinstance(payload, dict):
            raise ValueError("meeting synthesis payload must be an object")
        unsupported = sorted(set(payload) - MEETING_SYNTHESIS_SUMMARY_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported meeting synthesis fields: {', '.join(unsupported)}")
        return cls(
            method=payload.get("method", MEETING_SYNTHESIS_METHOD),
            verdict=payload.get("verdict", ""),
            risks=payload.get("risks", ()),
            open_questions=payload.get("open_questions", ()),
            recommended_next_actions=payload.get("recommended_next_actions", ()),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "verdict": self.verdict,
            "risks": list(self.risks),
            "open_questions": list(self.open_questions),
            "recommended_next_actions": list(self.recommended_next_actions),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()

    @staticmethod
    def _normalize_text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"meeting synthesis {label} must be a non-empty string")
        normalized = value.strip()
        _reject_forbidden_synthesis_text(normalized, label)
        return normalized

    @staticmethod
    def _normalize_string_tuple(value: Any, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"meeting synthesis {label} must be a list")
        if not value and not allow_empty:
            raise ValueError(f"meeting synthesis {label} must be non-empty")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"meeting synthesis {label} must contain non-empty strings")
            text = item.strip()
            _reject_forbidden_synthesis_text(text, label)
            normalized.append(text)
        return tuple(normalized)


@dataclass(frozen=True)
class ControlPlaneDryRunProposedAction:
    workers: tuple[str, ...]
    title: str
    body: str
    timeout: int = 300

    def __post_init__(self) -> None:
        workers = self._normalize_workers(self.workers)
        title = self._normalize_text(self.title, "control_plane_dry_run title")
        body = self._normalize_text(self.body, "control_plane_dry_run body")
        timeout = self._normalize_timeout(self.timeout)
        object.__setattr__(self, "workers", workers)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "timeout", timeout)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ControlPlaneDryRunProposedAction":
        if not isinstance(payload, dict):
            raise ValueError("proposed action must be an object")
        unsupported = sorted(set(payload) - CONTROL_PLANE_DRY_RUN_ACTION_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported proposed action fields: {', '.join(unsupported)}")
        if payload.get("kind") != CONTROL_PLANE_DRY_RUN_KIND:
            raise ValueError(f"unsupported proposed action kind: {payload.get('kind')}")
        return cls(
            workers=payload.get("workers"),
            title=payload.get("title"),
            body=payload.get("body"),
            timeout=payload.get("timeout", 300),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": CONTROL_PLANE_DRY_RUN_KIND,
            "workers": list(self.workers),
            "title": self.title,
            "body": self.body,
            "timeout": self.timeout,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()

    @staticmethod
    def _normalize_workers(raw_workers: Any) -> tuple[str, ...]:
        if not isinstance(raw_workers, (list, tuple)) or not raw_workers:
            raise ValueError("control_plane_dry_run workers must be a non-empty list")
        workers: list[str] = []
        for worker in raw_workers:
            if not isinstance(worker, str) or not worker.strip():
                raise ValueError("control_plane_dry_run workers must be non-empty strings")
            workers.append(worker.strip())
        return tuple(workers)

    @staticmethod
    def _normalize_text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _normalize_timeout(value: Any) -> int:
        if type(value) is not int or value < 1:
            raise ValueError("control_plane_dry_run timeout must be a positive integer")
        return value


@dataclass(frozen=True)
class MeetingParticipant:
    name: str
    role: str
    goal: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SpeakerSelectionResult:
    next_speaker: str = ""
    selection_method: str = ""
    terminate: bool = False
    reason: str = ""
    termination_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MeetingDecision:
    round_index: int
    previous_speaker: str
    next_speaker: str
    selection_method: str
    terminate: bool
    reason: str = ""
    termination_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MeetingMessage:
    round_index: int
    speaker: str
    role: str
    content: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MeetingSpec:
    task: str
    participants: list[MeetingParticipant]
    max_rounds: int
    policy: str = "round_robin"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["participants"] = [participant.to_dict() for participant in self.participants]
        return data


@dataclass(frozen=True)
class MeetingResult:
    task: str
    participants: list[MeetingParticipant]
    messages: list[MeetingMessage]
    selector_decisions: list[MeetingDecision]
    termination_reason: str
    verdict: str
    proposed_actions: list[dict[str, Any]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "task": self.task,
            "participants": [participant.to_dict() for participant in self.participants],
            "messages": [message.to_dict() for message in self.messages],
            "selector_decisions": [decision.to_dict() for decision in self.selector_decisions],
            "termination_reason": self.termination_reason,
            "verdict": self.verdict,
            "proposed_actions": list(self.proposed_actions),
            "risks": list(self.risks),
            "synthesis": build_meeting_synthesis_summary(self).to_payload(),
        }


def build_meeting_audit_summary(
    *,
    ag2_human_approval_validation: dict[str, Any] | None = None,
    ag2_host_local_config_reference_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_kind": "meeting_ag2_optional_import_gate",
        "method": MEETING_AUDIT_METHOD,
        "would_execute_live": False,
        "ag2_import_gate": Ag2OptionalImportGate().to_payload(),
        "ag2_runtime_import_policy": build_ag2_runtime_import_policy(),
        "ag2_host_local_adapter_plan": build_ag2_host_local_adapter_plan(),
        "ag2_human_approval_artifact": build_ag2_human_approval_artifact(),
        "ag2_host_local_config_contract": build_ag2_host_local_config_contract(),
        "ag2_real_runtime_policy_artifact": build_ag2_real_runtime_policy_artifact(),
        "ag2_effective_runtime_boundary": build_ag2_effective_runtime_boundary_contract(),
        "ag2_real_runtime_dependency_decision": build_ag2_real_runtime_dependency_decision(),
        "ag2_human_approval_validation": _normalize_ag2_validation_evidence(
            ag2_human_approval_validation,
            expected_kind="ag2_human_approval_payload_validation",
            default_evidence=build_ag2_human_approval_validation_evidence(),
        ),
        "ag2_host_local_config_reference_validation": _normalize_ag2_validation_evidence(
            ag2_host_local_config_reference_validation,
            expected_kind="ag2_host_local_config_reference_validation",
            default_evidence=build_ag2_host_local_config_reference_validation_evidence(),
        ),
    }


def build_ag2_meeting_layer_contract_summary() -> dict[str, Any]:
    """Return the frozen no-live AG2 meeting-layer interface contract."""
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_kind": "ag2_meeting_layer_interface_contract",
        "contract_state": "frozen_no_live",
        "result_schema_stable": True,
        "audit_is_companion_only": True,
        "artifact_files": [
            "meeting-result.json",
            "meeting-synthesis.json",
            "meeting-audit.json",
            "transcript.md",
        ],
        "result_schema_top_level_keys": [
            "schema_version",
            "task",
            "participants",
            "messages",
            "selector_decisions",
            "termination_reason",
            "verdict",
            "proposed_actions",
            "risks",
            "synthesis",
        ],
        "audit_sections": [
            "schema_version",
            "audit_kind",
            "method",
            "would_execute_live",
            "ag2_import_gate",
            "ag2_runtime_import_policy",
            "ag2_host_local_adapter_plan",
            "ag2_human_approval_artifact",
            "ag2_host_local_config_contract",
            "ag2_real_runtime_policy_artifact",
            "ag2_effective_runtime_boundary",
            "ag2_real_runtime_dependency_decision",
            "ag2_human_approval_validation",
            "ag2_host_local_config_reference_validation",
        ],
        "supported_validation_statuses": ["not_run", "valid", "rejected"],
        "allowed_selector_policies": ["role_order", "round_robin"],
        "allowed_termination_conditions": ["all_participants_spoke", "single_turn"],
        "status_commands": [
            "ag2-import-gate",
            "ag2-adapter-plan",
            "ag2-approval-status",
            "ag2-host-local-config-status",
            "ag2-runtime-preflight",
            "ag2-status",
            "ag2-release-readiness",
            "safety-check",
        ],
        "effect_flags": {
            "would_execute_live": False,
            "would_install_dependency": False,
            "would_import_ag2": False,
            "provider_or_model_config_required": False,
            "import_allowed": False,
            "runtime_import_allowed": False,
            "effective_runtime_import_allowed": False,
            "adapter_enabled": False,
            "adapter_eligible": False,
            "adapter_eligibility_granted": False,
            "effective_adapter_eligibility_granted": False,
            "supports_live_submit": False,
            "dispatches_via_control_plane": False,
            "uses_tmux_substrate": False,
            "uses_model_routing": False,
            "config_values_recorded": False,
            "provider_config_present": False,
            "model_config_present": False,
            "endpoint_config_present": False,
            "secret_config_present": False,
            "synced_to_repo": False,
            "provider_family_allowed": False,
            "provider_values_recorded": False,
            "model_routing_allowed": False,
            "v4_pro_allowed": False,
            "secret_echo_allowed": False,
            "spend_cap_recorded": False,
            "max_timeout_recorded": False,
            "synced_runtime_config_allowed": False,
        },
        "non_goals": [
            "no_ag2_autogen_runtime_imports",
            "no_provider_model_base_url_api_key_config",
            "no_v4_pro",
            "no_dynamic_plugins",
            "no_live_worker_execution",
            "no_report_or_terminal_marker_writes",
            "no_lock_registry_or_watcher_writes",
        ],
    }


def build_ag2_runtime_import_policy(gate: Ag2OptionalImportGate | None = None) -> dict[str, Any]:
    return Ag2RuntimeImportPolicy(ag2_import_gate=gate).to_payload()


def build_ag2_host_local_adapter_plan(
    runtime_import_policy: Ag2RuntimeImportPolicy | None = None,
) -> dict[str, Any]:
    return Ag2HostLocalAdapterPlan(runtime_import_policy=runtime_import_policy).to_payload()


def build_ag2_human_approval_artifact() -> dict[str, Any]:
    return Ag2HumanApprovalArtifact().to_payload()


def build_ag2_host_local_config_contract() -> dict[str, Any]:
    return Ag2HostLocalConfigContract().to_payload()


def build_ag2_real_runtime_policy_artifact() -> dict[str, Any]:
    return Ag2RealRuntimePolicyArtifact().to_payload()


def build_ag2_effective_runtime_boundary_contract() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "boundary_kind": AG2_EFFECTIVE_RUNTIME_BOUNDARY_KIND,
        "boundary_state": "blocked_design_only",
        "ready_for_real_runtime": False,
        "effective_runtime_allowed": False,
        "requires_non_advisory_approval_semantics": True,
        "requires_host_local_config_storage_policy": True,
        "requires_non_v4_pro_model_policy": True,
        "requires_redaction_policy": True,
        "requires_cost_timeout_policy": True,
        "requires_sync_boundary_policy": True,
        "required_non_advisory_gates": [
            {
                "gate_name": gate_name,
                "preflight_missing_gate": preflight_missing_gate,
                "current_state": "missing",
                "satisfied": False,
                "must_be_effective_before_adapter": True,
            }
            for gate_name, preflight_missing_gate in AG2_EFFECTIVE_RUNTIME_BOUNDARY_REQUIRED_GATES
        ],
        "adapter_implementation_allowed": False,
        "live_worker_dispatch_allowed": False,
        "new_cli_command_allowed": False,
        "would_install_dependency": False,
        "would_import_ag2": False,
        "would_execute_live": False,
        "provider_or_model_config_required": False,
        "notes": [
            "boundary contract is design-only and advisory",
            "real AG2 runtime remains blocked until every required gate has non-advisory semantics",
            "no new CLI command or adapter implementation is authorized by this contract",
        ],
    }


def build_ag2_human_approval_validation_evidence() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "validation_kind": "ag2_human_approval_payload_validation",
        "validation_status": "not_run",
        "valid": False,
        "normalized_payload": None,
        "errors": [],
        "warnings": ["no approval payload validation evidence recorded for this meeting audit"],
        "effective_runtime_import_allowed": False,
        "effective_adapter_eligibility_granted": False,
        "would_install_dependency": False,
        "would_import_ag2": False,
        "would_execute_live": False,
        "provider_or_model_config_required": False,
    }


def build_ag2_host_local_config_reference_validation_evidence() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "validation_kind": "ag2_host_local_config_reference_validation",
        "validation_status": "not_run",
        "valid": False,
        "path_classification": "not_run",
        "config_contract": build_ag2_host_local_config_contract(),
        "errors": [],
        "warnings": ["no host-local config reference validation evidence recorded for this meeting audit"],
        "config_values_recorded": False,
        "synced_to_repo": False,
        "adapter_eligibility_granted": False,
        "would_install_dependency": False,
        "would_import_ag2": False,
        "would_execute_live": False,
        "provider_or_model_config_required": False,
    }


def _normalize_ag2_validation_evidence(
    evidence: dict[str, Any] | None,
    *,
    expected_kind: str,
    default_evidence: dict[str, Any],
) -> dict[str, Any]:
    if evidence is None:
        return default_evidence
    if not isinstance(evidence, dict):
        raise ValueError("AG2 validation evidence must be an object")
    if evidence.get("validation_kind") != expected_kind:
        raise ValueError(f"AG2 validation evidence kind must be {expected_kind}")
    if not isinstance(evidence.get("valid"), bool):
        raise ValueError("AG2 validation evidence valid must be a boolean")
    normalized = dict(evidence)
    normalized["validation_status"] = "valid" if normalized["valid"] else "rejected"
    for field_name in AG2_VALIDATION_FALSE_FIELDS:
        if field_name in normalized:
            normalized[field_name] = False
    return normalized


def build_ag2_real_runtime_preflight(
    *,
    ag2_human_approval_validation: dict[str, Any] | None = None,
    ag2_host_local_config_reference_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gate = Ag2OptionalImportGate()
    runtime_import_policy = build_ag2_runtime_import_policy(gate)
    adapter_plan = build_ag2_host_local_adapter_plan()
    approval_validation = _normalize_ag2_validation_evidence(
        ag2_human_approval_validation,
        expected_kind="ag2_human_approval_payload_validation",
        default_evidence=build_ag2_human_approval_validation_evidence(),
    )
    host_local_config_reference_validation = _normalize_ag2_validation_evidence(
        ag2_host_local_config_reference_validation,
        expected_kind="ag2_host_local_config_reference_validation",
        default_evidence=build_ag2_host_local_config_reference_validation_evidence(),
    )
    runtime_policy_artifact = build_ag2_real_runtime_policy_artifact()
    effective_runtime_boundary = build_ag2_effective_runtime_boundary_contract()
    return {
        "schema_version": SCHEMA_VERSION,
        "preflight_kind": AG2_REAL_RUNTIME_PREFLIGHT_KIND,
        "preflight_state": "blocked",
        "ready_for_real_runtime": False,
        "missing_gates": list(AG2_REAL_RUNTIME_PREFLIGHT_MISSING_GATES),
        "would_install_dependency": False,
        "would_import_ag2": False,
        "would_execute_live": False,
        "provider_or_model_config_required": False,
        "runtime_import_policy": runtime_import_policy,
        "adapter_plan": adapter_plan,
        "runtime_policy_artifact": runtime_policy_artifact,
        "effective_runtime_boundary": effective_runtime_boundary,
        "dependency_decision": build_ag2_real_runtime_dependency_decision(),
        "approval_validation": approval_validation,
        "host_local_config_reference_validation": host_local_config_reference_validation,
        "notes": [
            "preflight is fail-closed and advisory only",
            "valid validation evidence does not enable runtime import or adapter eligibility",
            "real provider smoke requires explicit host-local config and human approval policy",
        ],
    }


def build_ag2_release_readiness() -> dict[str, Any]:
    """Return the current no-live release gate decision for the AG2 meeting layer."""
    return {
        "schema_version": SCHEMA_VERSION,
        "readiness_kind": "ag2_meeting_layer_release_readiness",
        "release_state": "ready_mock_no_live",
        "go_no_go": "go_mock_no_live",
        "release_scope": "project_local_mock_ag2_meeting_layer",
        "would_install_dependency": False,
        "would_import_ag2": False,
        "would_execute_live": False,
        "provider_or_model_config_required": False,
        "supported_modes": list(AG2_RELEASE_READINESS_SUPPORTED_MODES),
        "unsupported_modes": list(AG2_RELEASE_READINESS_UNSUPPORTED_MODES),
        "required_checks": list(AG2_RELEASE_READINESS_REQUIRED_CHECKS),
        "packaging_decision": "keep_project_local_for_now",
        "next_phase": "phase_33_packaging_decision",
        "contract_summary": build_ag2_meeting_layer_contract_summary(),
        "real_runtime_preflight": build_ag2_real_runtime_preflight(),
        "effective_runtime_boundary": build_ag2_effective_runtime_boundary_contract(),
        "dependency_decision": build_ag2_real_runtime_dependency_decision(),
        "real_runtime_release_gate": build_ag2_real_runtime_release_gate(),
        "notes": [
            "mock AG2-style meetings are usable through the project-local wrapper",
            "real AG2 runtime remains blocked by Phase 28B gates",
            "release readiness does not grant provider, model, import, or live-worker authorization",
        ],
    }


def validate_ag2_human_approval_payload(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    normalized_payload: dict[str, Any] | None = None
    try:
        normalized_payload = Ag2HumanApprovalArtifact.from_payload(payload).to_payload()
    except ValueError as exc:
        errors.append(str(exc))
    return {
        "schema_version": SCHEMA_VERSION,
        "validation_kind": "ag2_human_approval_payload_validation",
        "valid": not errors,
        "normalized_payload": normalized_payload,
        "errors": errors,
        "warnings": (
            []
            if errors
            else [
                "syntactic approval payload validation is advisory only",
                "valid approval payload does not enable runtime import or adapter eligibility",
            ]
        ),
        "effective_runtime_import_allowed": False,
        "effective_adapter_eligibility_granted": False,
        "would_install_dependency": False,
        "would_import_ag2": False,
        "would_execute_live": False,
        "provider_or_model_config_required": False,
    }


def validate_ag2_effective_approval_payload(payload: dict[str, Any], *, now_iso: str | None = None) -> dict[str, Any]:
    errors: list[str] = []
    normalized_payload: dict[str, Any] | None = None
    if not isinstance(payload, dict):
        errors.append("AG2 effective approval payload must be an object")
    else:
        unsupported = sorted(set(payload) - AG2_EFFECTIVE_APPROVAL_FIELDS)
        if unsupported:
            errors.append(f"unsupported AG2 effective approval fields: {', '.join(unsupported)}")
        if payload.get("schema_version") != SCHEMA_VERSION:
            errors.append("AG2 effective approval schema_version must match the meeting schema version")
        if payload.get("artifact_kind") != AG2_EFFECTIVE_APPROVAL_KIND:
            errors.append(f"AG2 effective approval artifact_kind must be {AG2_EFFECTIVE_APPROVAL_KIND}")
        if payload.get("approval_state") != "approved":
            errors.append("AG2 effective approval approval_state must be approved")
        for field_name in ("approval_id", "approved_by", "approved_at", "expires_at", "decision_reason"):
            if not isinstance(payload.get(field_name), str) or not payload[field_name].strip():
                errors.append(f"AG2 effective approval {field_name} must be a non-empty string")
        snapshot = payload.get("policy_snapshot_sha256")
        if not isinstance(snapshot, str) or len(snapshot) != 64 or any(ch not in "0123456789abcdef" for ch in snapshot):
            errors.append("AG2 effective approval policy_snapshot_sha256 must be a lowercase sha256 hex digest")
        if isinstance(payload.get("expires_at"), str) and payload["expires_at"].strip():
            try:
                expires_at = _parse_iso_datetime(payload["expires_at"])
                now = _parse_iso_datetime(now_iso) if now_iso else datetime.now(timezone.utc)
            except ValueError as exc:
                errors.append(str(exc))
            else:
                if expires_at <= now:
                    errors.append("AG2 effective approval has expired")
    valid = not errors
    if valid:
        normalized_payload = {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": AG2_EFFECTIVE_APPROVAL_KIND,
            "approval_state": "approved",
            "approval_id": payload["approval_id"].strip(),
            "approved_by": payload["approved_by"].strip(),
            "approved_at": payload["approved_at"].strip(),
            "expires_at": payload["expires_at"].strip(),
            "policy_snapshot_sha256": payload["policy_snapshot_sha256"].strip(),
            "decision_reason": payload["decision_reason"].strip(),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "validation_kind": "ag2_effective_approval_payload_validation",
        "valid": valid,
        "approval_effective": valid,
        "normalized_payload": normalized_payload,
        "errors": errors,
        "warnings": [] if errors else ["approval is effective only for explicitly gated AG2 runtime adapter calls"],
    }


def load_ag2_host_local_runtime_config(
    config_reference_path: os.PathLike[str] | str,
    *,
    repo_root: Path | str,
    runtime_root: Path | str,
    business_roots: tuple[Path | str, ...] = (),
) -> Ag2HostLocalRuntimeConfig:
    reference_validation = validate_ag2_host_local_config_reference(
        config_reference_path,
        repo_root=repo_root,
        runtime_root=runtime_root,
        business_roots=business_roots,
    )
    if not reference_validation["valid"]:
        raise ValueError("; ".join(reference_validation["errors"]))
    path = Path(os.fspath(config_reference_path)).expanduser().resolve(strict=False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"AG2 host-local runtime config could not be read: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("AG2 host-local runtime config must contain a JSON object")
    unsupported = sorted(set(payload) - AG2_RUNTIME_CONFIG_FIELDS)
    if unsupported:
        raise ValueError(f"unsupported AG2 host-local runtime config fields: {', '.join(unsupported)}")
    provider = _required_runtime_config_text(payload, "provider")
    model = _required_runtime_config_text(payload, "model")
    base_url = _required_runtime_config_text(payload, "base_url")
    api_key_env = _required_runtime_config_text(payload, "api_key_env")
    if _is_v4_pro_model(model):
        raise ValueError("AG2 host-local runtime config must not use v4-pro")
    if not (base_url.startswith("https://") or base_url.startswith("http://127.0.0.1") or base_url.startswith("http://localhost")):
        raise ValueError("AG2 host-local runtime config base_url must be https or localhost http")
    if not api_key_env.replace("_", "").isalnum() or not api_key_env[0].isalpha():
        raise ValueError("AG2 host-local runtime config api_key_env must be an environment variable name")
    timeout_seconds = payload.get("timeout_seconds", 30)
    if type(timeout_seconds) is not int or timeout_seconds < 1 or timeout_seconds > 300:
        raise ValueError("AG2 host-local runtime config timeout_seconds must be an integer between 1 and 300")
    max_cost_usd = payload.get("max_cost_usd", 0.05)
    if type(max_cost_usd) not in {int, float} or isinstance(max_cost_usd, bool) or max_cost_usd <= 0 or max_cost_usd > 5:
        raise ValueError("AG2 host-local runtime config max_cost_usd must be a positive number no greater than 5")
    empty_response_retries = payload.get("empty_response_retries", 1)
    if type(empty_response_retries) is not int or empty_response_retries < 0 or empty_response_retries > 3:
        raise ValueError("AG2 host-local runtime config empty_response_retries must be an integer between 0 and 3")
    return Ag2HostLocalRuntimeConfig(
        config_reference=path,
        provider=provider,
        model=model,
        base_url=base_url.rstrip("/"),
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
        max_cost_usd=float(max_cost_usd),
        empty_response_retries=empty_response_retries,
    )


def validate_ag2_host_local_config_reference(
    config_reference_path: os.PathLike[str] | str,
    *,
    repo_root: Path | str,
    runtime_root: Path | str,
    business_roots: tuple[Path | str, ...] = (),
) -> dict[str, Any]:
    errors: list[str] = []
    raw_path = os.fspath(config_reference_path)
    if not isinstance(raw_path, str) or not raw_path.strip():
        errors.append("AG2 host-local config reference path must be a non-empty absolute path")
        candidate = Path("")
    else:
        candidate = Path(raw_path.strip())
        if not candidate.is_absolute():
            errors.append("AG2 host-local config reference path must be absolute")
    if not errors:
        resolved = _resolve_path_for_classification(candidate)
        roots = [
            ("synced repository root", _resolve_path_for_classification(Path(repo_root))),
            ("runtime root", _resolve_path_for_classification(Path(runtime_root))),
            *[
                ("business repository root", _resolve_path_for_classification(Path(root)))
                for root in business_roots
            ],
        ]
        for label, root in roots:
            if _path_is_relative_to(resolved, root):
                errors.append(f"AG2 host-local config reference path must not be under {label}")
                break
        forbidden_parts = {"multi-agent_questions", "multi-agent_question_artifacts"}
        if any(part in forbidden_parts for part in resolved.parts):
            errors.append("AG2 host-local config reference path must not point at synced meeting notes or artifacts")
        marker_names = {"report.md", ".done", ".fail", ".blocked"}
        if any(part in marker_names for part in resolved.parts):
            errors.append("AG2 host-local config reference path must not point at worker reports or terminal markers")
    valid = not errors
    return {
        "schema_version": SCHEMA_VERSION,
        "validation_kind": "ag2_host_local_config_reference_validation",
        "valid": valid,
        "path_classification": "host_local" if valid else "rejected",
        "config_contract": build_ag2_host_local_config_contract(),
        "errors": errors,
        "warnings": (
            []
            if errors
            else [
                "host-local config reference validation is content-blind",
                "valid reference does not record provider/model/endpoint/secret values",
            ]
        ),
        "config_values_recorded": False,
        "synced_to_repo": False,
        "adapter_eligibility_granted": False,
        "would_install_dependency": False,
        "would_import_ag2": False,
        "would_execute_live": False,
        "provider_or_model_config_required": False,
    }


def _required_runtime_config_text(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"AG2 host-local runtime config {field_name} must be a non-empty string")
    return value.strip()


def _normalize_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of strings")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must contain non-empty strings")
        normalized.append(item.strip())
    return tuple(normalized)


def _is_v4_pro_model(model: str) -> bool:
    return "v4-pro" in model.strip().lower().replace("_", "-")


def _parse_iso_datetime(value: str | None) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("AG2 effective approval timestamp must be a non-empty ISO datetime")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resolve_path_for_classification(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def build_meeting_synthesis_summary(result: MeetingResult) -> MeetingSynthesisSummary:
    spoken = {message.speaker for message in result.messages}
    missing = [participant.name for participant in result.participants if participant.name not in spoken]

    risks = _dedupe_strings(result.risks)
    open_questions: list[str] = []
    recommended_next_actions: list[str] = []

    if result.termination_reason == "max_rounds":
        risks.append("meeting stopped at max_rounds before explicit termination")
        if missing:
            missing_text = ", ".join(missing)
            risks.append(f"participant coverage incomplete: {missing_text}")
            open_questions.append(f"What input is still needed from {missing_text}?")
            recommended_next_actions.append(
                "Run another deterministic meeting round with enough max_rounds for missing participants."
            )
        else:
            open_questions.append("Is the current transcript sufficient to approve a follow-up action?")
            recommended_next_actions.append("Review whether to add a local termination condition before execution.")

    if result.proposed_actions:
        count = len(result.proposed_actions)
        recommended_next_actions.append(
            f"Review {count} proposed action artifact(s) with bridge-dry-run before any live execution."
        )
    elif result.verdict == "terminated" and not risks:
        recommended_next_actions.append("Review transcript and approve or reject the deterministic meeting recommendation.")
    elif not recommended_next_actions:
        recommended_next_actions.append("Review transcript before approving any follow-up action.")

    return MeetingSynthesisSummary(
        verdict=result.verdict,
        risks=tuple(_dedupe_strings(risks)),
        open_questions=tuple(_dedupe_strings(open_questions)),
        recommended_next_actions=tuple(_dedupe_strings(recommended_next_actions)),
    )


def _dedupe_strings(values: list[str] | tuple[str, ...]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped


def _reject_forbidden_synthesis_text(value: str, label: str) -> None:
    hit = next((token for token in MEETING_SYNTHESIS_FORBIDDEN_TEXT if token in value), "")
    if hit:
        raise ValueError(f"forbidden meeting synthesis control token in {label}: {hit}")
