"""Machine-readable source-side contracts for missing OpenCode runtime surfaces.

These contracts do not call OpenCode or mutate runtime state. They define the
callable interfaces that Phase A/B need so runtime owners can implement them in
their own source tree while repository-local tooling can consume a stable shape.
"""

from __future__ import annotations


SCHEMA_VERSION = 1


def _no_live_flags() -> dict[str, bool]:
    return {
        "advisory_only": True,
        "dry_run_only": True,
        "actuation_enabled": False,
        "would_execute": False,
        "would_write_runtime_state": False,
    }


def build_background_output_contract() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "opencode_background_output_contract",
        **_no_live_flags(),
        "callable_surface": "background_output",
        "http_route": "/experimental/session/{sessionID}/background/{taskID}",
        "sdk_method": "backgroundOutput",
        "method": "GET_OR_EQUIVALENT_RUNTIME_CALL",
        "request": {
            "required_fields": ["task_id"],
            "optional_fields": ["block", "timeout_ms"],
        },
        "response": {
            "required_fields": [
                "completion_seen",
                "retrieval_ready",
                "state",
                "retry_after_ms",
                "canonical_retrieval_handle",
                "fallback_session_id",
                "message",
            ],
            "allowed_states": [
                "retrieval_ready",
                "completed_but_indexing",
                "expired_with_session_fallback",
                "unknown_task_id",
                "retrieval_not_ready_or_unknown",
            ],
        },
        "parent_rules": [
            "retry_at_most_once_before_freeze",
            "never_poll_after_terminal_closeout",
            "do_not_treat_task_not_found_as_proof_of_no_work",
        ],
    }


def build_optional_lane_actuation_contract() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "opencode_optional_lane_actuation_contract",
        **_no_live_flags(),
        "callable_surface": "control_background_lane",
        "http_route": "/experimental/session/{sessionID}/background/{taskID}/control",
        "sdk_method": "backgroundControl",
        "method": "POST_OR_EQUIVALENT_RUNTIME_CALL",
        "request": {
            "required_fields": [
                "action",
                "canonical_retrieval_handle",
                "reason",
                "parent_task_id",
            ],
            "allowed_actions": [
                "cancel_late_optional_lane",
                "ignore_late_optional_lane",
            ],
        },
        "response": {
            "required_fields": [
                "action_requested",
                "action_applied",
                "state",
                "canonical_retrieval_handle",
                "required_or_optional",
                "actuation_gate",
                "message",
            ],
            "allowed_states": [
                "cancelled_in_runtime",
                "already_completed_but_unconsumed",
                "already_expired",
                "required_lane_rejected",
                "gate_disabled_dry_run",
                "unknown_lane_handle",
            ],
        },
        "safety_invariants": [
            "never_cancel_required_lane",
            "never_mutate_report_or_terminal_markers",
            "never_release_locks_or_change_completion_truth",
        ],
    }
