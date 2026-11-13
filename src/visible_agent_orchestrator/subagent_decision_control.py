from __future__ import annotations

from pathlib import Path
from typing import Any

from visible_agent_orchestrator.lane_contracts import inspect_lane_contract_bundle


SCHEMA_VERSION = 1
ALLOWED_EVIDENCE_BUNDLE_SIZES = {"small_fixed", "broad", "unknown"}
ALLOWED_TOPOLOGY_KINDS = {"unknown", "3-worker", "4-worker", "4-worker-parallel", "5-worker", "6-worker"}
REQUIRED_OR_OPTIONAL_VALUES = {"required", "optional"}
WARN_CONTRACT_OVERLAP_POLICY = {
    "policy_kind": "internal_replan_signal",
    "user_visible_by_default": False,
    "leader_action": "try_direct_fallback_or_rewrite_contract",
    "gate_action": "block_auto_execution_and_allow_internal_replan",
    "allowed_followups": [
        "direct_local_tools",
        "replacement_contract",
        "optional_lane_removal_via_new_contract",
    ],
    "forbidden_followups": [
        "launch_warned_plan",
        "auto_waive",
        "auto_accept",
        "reinterpret_as_skip_optional_lane",
    ],
    "escalation_conditions": [
        "replan_budget_exhausted",
        "required_lane_unrepairable",
        "no_nonoverlap_path_found",
    ],
}


def build_opencode_subagent_decision_control_plan(request: dict[str, Any]) -> dict[str, Any]:
    lane_contracts_path = str(request.get("lane_contracts_path") or request.get("lane_contracts") or "").strip()
    workers = [str(item).strip() for item in list(request.get("workers") or []) if str(item).strip()]
    if not workers:
        worker_value = request.get("worker")
        if isinstance(worker_value, list):
            workers = [str(item).strip() for item in worker_value if str(item).strip()]
        elif str(worker_value or "").strip():
            workers = [str(worker_value).strip()]
    strict_closeout_contract = bool(request.get("strict_closeout_contract"))
    direct_tools_available = bool(request.get("direct_tools_available"))
    unresolved_go_no_go = bool(request.get("unresolved_go_no_go"))
    evidence_bundle_size = str(request.get("evidence_bundle_size") or "unknown").strip().lower()
    topology_kind = str(request.get("topology_kind") or "unknown").strip().lower()
    blocked_reasons: list[str] = []

    if not lane_contracts_path:
        blocked_reasons.append("lane_contracts_path is required")
    if not workers:
        blocked_reasons.append("workers are required")
    if evidence_bundle_size not in ALLOWED_EVIDENCE_BUNDLE_SIZES:
        blocked_reasons.append(f"invalid_evidence_bundle_size:{evidence_bundle_size}")
    if topology_kind not in ALLOWED_TOPOLOGY_KINDS:
        blocked_reasons.append(f"invalid_topology_kind:{topology_kind}")

    bundle: dict[str, Any] = {"assignments": [], "planner_coverage": {}, "contract_warnings": [], "blocked_reasons": []}
    if not blocked_reasons:
        bundle = inspect_lane_contract_bundle(lane_contracts_path, workers=workers)
        blocked_reasons.extend([str(item) for item in list(bundle.get("blocked_reasons") or []) if str(item)])

    contract_warnings = [item for item in list(bundle.get("contract_warnings") or []) if isinstance(item, dict)]
    lanes: list[dict[str, Any]] = []
    direct_tools_first = strict_closeout_contract and direct_tools_available and evidence_bundle_size == "small_fixed"
    launchable_lane_count = 0
    blocked_required_lane_count = 0
    for assignment in [] if blocked_reasons else bundle.get("assignments", []):
        lane = _lane_decision(
            assignment,
            planner_coverage=bundle.get("planner_coverage") if isinstance(bundle.get("planner_coverage"), dict) else {},
            direct_tools_first=direct_tools_first,
            unresolved_go_no_go=unresolved_go_no_go,
            topology_kind=topology_kind,
        )
        if lane["spawn_recommendation"] in {"allow_optional_lane", "allow_required_lane"}:
            launchable_lane_count += 1
        if lane["spawn_recommendation"] == "block_required_lane":
            blocked_required_lane_count += 1
        lanes.append(lane)

    if blocked_reasons and _is_overlap_only_contract_warning(blocked_reasons, contract_warnings):
        primary_recommendation = "warn_contract_overlap"
        decision_phase = "contract_validation"
        decision_reason_code = "warn_contract_overlap"
    elif blocked_reasons:
        primary_recommendation = "block_invalid_contract"
        decision_phase = "contract_validation"
        decision_reason_code = "invalid_lane_contract"
    elif direct_tools_first:
        primary_recommendation = "direct_tools_first"
        decision_phase = "route_decision"
        decision_reason_code = "strict_closeout_small_fixed_direct_tools_ready"
    elif blocked_required_lane_count:
        primary_recommendation = "block_required_lane"
        decision_phase = "spawn_admission"
        decision_reason_code = "required_lane_cannot_be_silently_skipped"
    elif launchable_lane_count:
        primary_recommendation = "allow_spawnable_lanes"
        decision_phase = "spawn_admission"
        decision_reason_code = "spawnable_lanes_available"
    else:
        primary_recommendation = "skip_optional_lane"
        decision_phase = "spawn_admission"
        decision_reason_code = "no_optional_lane_admitted"
    consumer_policy = _consumer_policy(
        primary_recommendation=primary_recommendation,
        decision_phase=decision_phase,
    )
    replan_plan = _replan_plan(
        primary_recommendation=primary_recommendation,
        consumer_policy=consumer_policy,
        assignments=[item for item in list(bundle.get("assignments") or []) if isinstance(item, dict)],
        planner_coverage=bundle.get("planner_coverage") if isinstance(bundle.get("planner_coverage"), dict) else {},
        direct_tools_available=direct_tools_available,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "opencode_subagent_decision_control_plan",
        "advisory_only": True,
        "dry_run_only": True,
        "actuation_enabled": False,
        "would_execute": False,
        "would_write_runtime_state": False,
        "ok": not blocked_reasons,
        "lane_contracts_path": str(Path(lane_contracts_path).expanduser().resolve()) if lane_contracts_path else "",
        "workers": workers,
        "strict_closeout_contract": strict_closeout_contract,
        "direct_tools_available": direct_tools_available,
        "evidence_bundle_size": evidence_bundle_size,
        "unresolved_go_no_go": unresolved_go_no_go,
        "topology_kind": topology_kind,
        "primary_recommendation": primary_recommendation,
        "decision_phase": decision_phase,
        "decision_reason_code": decision_reason_code,
        "decision_inputs": {
            "strict_closeout_contract": strict_closeout_contract,
            "direct_tools_available": direct_tools_available,
            "evidence_bundle_size": evidence_bundle_size,
            "unresolved_go_no_go": unresolved_go_no_go,
            "topology_kind": topology_kind,
            "blocked_required_lane_count": blocked_required_lane_count,
        },
        "launchable_lane_count": launchable_lane_count,
        "lanes": lanes,
        "blocked_reasons": blocked_reasons,
        "contract_warnings": contract_warnings,
        "consumer_policy": consumer_policy,
        "replan_plan": replan_plan,
    }


def _lane_decision(
    assignment: dict[str, Any],
    *,
    planner_coverage: dict[str, Any],
    direct_tools_first: bool,
    unresolved_go_no_go: bool,
    topology_kind: str,
) -> dict[str, Any]:
    worker_id = str(assignment.get("worker_id") or "")
    lane_id = str(assignment.get("lane_id") or "")
    evidence_domain = str(assignment.get("evidence_domain") or "")
    role = str(assignment.get("role") or "")
    may_spawn = bool((assignment.get("worker_authority") or {}).get("may_spawn_native_subagents"))
    subdomains = assignment.get("subdomains") if isinstance(assignment.get("subdomains"), list) else []
    required_or_optional, required_or_optional_source = _resolve_required_or_optional(
        assignment,
        planner_coverage=planner_coverage,
    )

    lane_result = {
        "worker_id": worker_id,
        "lane_id": lane_id,
        "evidence_domain": evidence_domain,
        "role": role,
        "required_or_optional": required_or_optional,
        "required_or_optional_source": required_or_optional_source,
        "topology_kind": topology_kind,
        "may_spawn_native_subagents": may_spawn,
        "downstream_consumer": str(assignment.get("downstream_consumer") or ""),
        "expected_artifact": str(assignment.get("expected_artifact") or ""),
        "spawn_recommendation": "no_spawn",
        "reason": "worker_not_spawnable",
        "decision_phase": "spawn_admission",
        "decision_reason_code": "worker_not_spawnable",
        "decision_inputs": {
            "direct_tools_first": direct_tools_first,
            "unresolved_go_no_go": unresolved_go_no_go,
            "may_spawn_native_subagents": may_spawn,
            "role": role,
            "required_or_optional": required_or_optional,
            "topology_kind": topology_kind,
        },
        "subdomains": [],
    }
    if not may_spawn:
        return lane_result

    if direct_tools_first:
        if required_or_optional == "required":
            lane_result["spawn_recommendation"] = "block_required_lane"
            lane_result["reason"] = "direct_tools_first"
            lane_result["decision_phase"] = "spawn_admission"
            lane_result["decision_reason_code"] = "required_lane_cannot_be_silently_skipped"
        else:
            lane_result["spawn_recommendation"] = "skip_optional_lane"
            lane_result["reason"] = "direct_tools_first"
            lane_result["decision_phase"] = "route_decision"
            lane_result["decision_reason_code"] = "direct_tools_first"
    elif any(str(item.get("agent") or "") == "oracle" for item in subdomains) and not unresolved_go_no_go:
        if required_or_optional == "required":
            lane_result["spawn_recommendation"] = "block_required_lane"
            lane_result["reason"] = "no_real_go_no_go_ambiguity"
            lane_result["decision_phase"] = "spawn_admission"
            lane_result["decision_reason_code"] = "required_lane_needs_go_no_go_ambiguity"
        else:
            lane_result["spawn_recommendation"] = "skip_optional_lane"
            lane_result["reason"] = "no_real_go_no_go_ambiguity"
            lane_result["decision_phase"] = "spawn_admission"
            lane_result["decision_reason_code"] = "no_real_go_no_go_ambiguity"
    else:
        lane_result["spawn_recommendation"] = (
            "allow_required_lane" if required_or_optional == "required" else "allow_optional_lane"
        )
        lane_result["reason"] = "expected_unique_value_declared"
        lane_result["decision_phase"] = "spawn_admission"
        lane_result["decision_reason_code"] = "expected_unique_value_declared"

    rendered_subdomains: list[dict[str, Any]] = []
    for subdomain in subdomains:
        agent = str(subdomain.get("agent") or "")
        recommendation = "launch_optional_lane"
        reason = "expected_unique_value_declared"
        decision_phase = "spawn_admission"
        decision_reason_code = "expected_unique_value_declared"
        if direct_tools_first:
            recommendation = "block_required_lane" if required_or_optional == "required" else "skip_optional_lane"
            reason = "direct_tools_first"
            decision_phase = "spawn_admission" if required_or_optional == "required" else "route_decision"
            decision_reason_code = (
                "required_lane_cannot_be_silently_skipped" if required_or_optional == "required" else "direct_tools_first"
            )
        elif agent == "oracle" and not unresolved_go_no_go:
            recommendation = "block_required_lane" if required_or_optional == "required" else "skip_optional_lane"
            reason = "no_real_go_no_go_ambiguity"
            decision_phase = "spawn_admission"
            decision_reason_code = (
                "required_lane_needs_go_no_go_ambiguity" if required_or_optional == "required" else "no_real_go_no_go_ambiguity"
            )
        rendered_subdomains.append(
            {
                "subdomain_id": str(subdomain.get("subdomain_id") or ""),
                "agent": agent,
                "allowed_sources": [str(item) for item in list(subdomain.get("allowed_sources") or [])],
                "expected_unique_value": str(subdomain.get("expected_unique_value") or ""),
                "recommendation": recommendation,
                "reason": reason,
                "decision_phase": decision_phase,
                "decision_reason_code": decision_reason_code,
                "decision_inputs": {
                    "direct_tools_first": direct_tools_first,
                    "unresolved_go_no_go": unresolved_go_no_go,
                    "agent": agent,
                    "required_or_optional": required_or_optional,
                },
            }
        )
    lane_result["subdomains"] = rendered_subdomains
    return lane_result


def _resolve_required_or_optional(
    assignment: dict[str, Any],
    *,
    planner_coverage: dict[str, Any],
) -> tuple[str, str]:
    explicit = str(assignment.get("required_or_optional") or "").strip().lower()
    if explicit in REQUIRED_OR_OPTIONAL_VALUES:
        return explicit, "lane_contract"
    lane_id = str(assignment.get("lane_id") or "").strip()
    required_lanes = planner_coverage.get("required_lanes") if isinstance(planner_coverage.get("required_lanes"), list) else []
    optional_lanes = planner_coverage.get("optional_lanes") if isinstance(planner_coverage.get("optional_lanes"), list) else []
    if lane_id and lane_id in [str(item) for item in required_lanes]:
        return "required", "planner_coverage"
    if lane_id and lane_id in [str(item) for item in optional_lanes]:
        return "optional", "planner_coverage"
    return "optional", "default_optional"


def _is_overlap_only_contract_warning(blocked_reasons: list[str], contract_warnings: list[dict[str, Any]]) -> bool:
    warning_messages = [
        str(item.get("warning_message") or "").strip()
        for item in contract_warnings
        if isinstance(item, dict) and str(item.get("warning_code") or "").strip() == "warn_contract_overlap"
    ]
    warning_messages = [item for item in warning_messages if item]
    if not blocked_reasons or not warning_messages:
        return False
    return blocked_reasons == warning_messages


def _consumer_policy(*, primary_recommendation: str, decision_phase: str) -> dict[str, Any]:
    if primary_recommendation == "warn_contract_overlap" and decision_phase == "contract_validation":
        return dict(WARN_CONTRACT_OVERLAP_POLICY)
    return {}


def _replan_plan(
    *,
    primary_recommendation: str,
    consumer_policy: dict[str, Any],
    assignments: list[dict[str, Any]],
    planner_coverage: dict[str, Any],
    direct_tools_available: bool,
) -> dict[str, Any]:
    if primary_recommendation != "warn_contract_overlap":
        return {}
    candidate_actions: list[str] = []
    if direct_tools_available:
        candidate_actions.append("direct_local_tools")
    if _all_assignments_optional(assignments, planner_coverage=planner_coverage):
        candidate_actions.append("optional_lane_removal_via_new_contract")
    candidate_actions.append("replacement_contract")
    candidate_actions = _dedupe_strings(candidate_actions)
    recommended_action = candidate_actions[0] if candidate_actions else ""
    replan_state = _replan_state_for_action(recommended_action)
    return {
        "plan_kind": "internal_replan_followup",
        "replan_state": replan_state,
        "recommended_action": recommended_action,
        "candidate_actions": candidate_actions,
        "allowed_to_execute_now": False,
        "user_prompt_required": False,
        "state_machine": _replan_state_machine(replan_state),
        "escalation_conditions": [
            str(item) for item in list(consumer_policy.get("escalation_conditions") or []) if str(item)
        ],
    }


def _all_assignments_optional(assignments: list[dict[str, Any]], *, planner_coverage: dict[str, Any]) -> bool:
    if not assignments:
        return False
    for assignment in assignments:
        required_or_optional, _ = _resolve_required_or_optional(assignment, planner_coverage=planner_coverage)
        if required_or_optional != "optional":
            return False
    return True


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _replan_state_for_action(recommended_action: str) -> str:
    action = str(recommended_action or "").strip()
    if action == "direct_local_tools":
        return "direct_fallback_ready"
    if action == "optional_lane_removal_via_new_contract":
        return "optional_lane_drop_ready"
    if action == "replacement_contract":
        return "replacement_contract_required"
    return "escalation_required"


def _replan_state_machine(replan_state: str) -> dict[str, Any]:
    state = str(replan_state or "").strip()
    if state == "direct_fallback_ready":
        return {
            "state_kind": "internal_replan_state_machine",
            "current_state": state,
            "allowed_actions": [
                "direct_local_tools",
                "replacement_contract",
                "escalate",
            ],
            "success_outcome": "revalidate_with_new_decision_control",
            "failure_state": "replacement_contract_required",
        }
    if state == "optional_lane_drop_ready":
        return {
            "state_kind": "internal_replan_state_machine",
            "current_state": state,
            "allowed_actions": [
                "optional_lane_removal_via_new_contract",
                "replacement_contract",
                "escalate",
            ],
            "success_outcome": "revalidate_with_new_decision_control",
            "failure_state": "replacement_contract_required",
        }
    if state == "replacement_contract_required":
        return {
            "state_kind": "internal_replan_state_machine",
            "current_state": state,
            "allowed_actions": [
                "replacement_contract",
                "escalate",
            ],
            "success_outcome": "revalidate_with_new_decision_control",
            "failure_state": "escalation_required",
        }
    return {
        "state_kind": "internal_replan_state_machine",
        "current_state": "escalation_required",
        "allowed_actions": ["escalate"],
        "success_outcome": "human_review_required",
        "failure_state": "escalation_required",
    }
