"""Pure, deterministic decisions for long-running collaboration loops."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


SCHEMA_VERSION = 1
KNOWN_PRIMITIVES = frozenset(
    {"pipeline", "fanout", "fork_join", "advisory_feedback"}
)
INCIDENT_KINDS = frozenset(
    {
        "suspected_stall",
        "connection_lost",
        "provider_request_budget_exhausted",
        "received_without_started",
        "delivery_uncertain",
        "binding_drift",
        "terminal_truth_invalid",
        "budget_exhausted",
    }
)
INCIDENT_POLICIES = {
    "suspected_stall": {
        "owner": "main",
        "notification_cooldown_cycles": 2,
        "recovery_ceiling": "read_only_reconciliation",
    },
    "connection_lost": {
        "owner": "main",
        "notification_cooldown_cycles": 2,
        "recovery_ceiling": "read_only_reconciliation",
    },
    "provider_request_budget_exhausted": {
        "owner": "main",
        "notification_cooldown_cycles": 2,
        "recovery_ceiling": "artifact_only",
    },
    "received_without_started": {
        "owner": "main",
        "notification_cooldown_cycles": 2,
        "recovery_ceiling": "read_only_reconciliation",
    },
    "delivery_uncertain": {
        "owner": "main",
        "notification_cooldown_cycles": 2,
        "recovery_ceiling": "receipt_lookup_only",
    },
    "binding_drift": {
        "owner": "main",
        "notification_cooldown_cycles": 2,
        "recovery_ceiling": "artifact_only",
    },
    "terminal_truth_invalid": {
        "owner": "main",
        "notification_cooldown_cycles": 2,
        "recovery_ceiling": "terminal_settling_only",
    },
    "budget_exhausted": {
        "owner": "main",
        "notification_cooldown_cycles": 2,
        "recovery_ceiling": "artifact_only",
    },
}
MAIN_COMMAND_ACTIONS = frozenset(
    {
        "acknowledge",
        "continue_observing",
        "request_status",
        "request_replan",
        "stop",
    }
)
MAIN_ADOPTION_RESULTS = frozenset(
    {"accepted", "accepted_with_changes", "rejected"}
)
FINDING_DISPOSITIONS = frozenset({"accepted", "rejected", "deferred"})
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class LoopEngineeringError(ValueError):
    """Raised when a loop-engineering artifact violates its fixed contract."""


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    return _digest(snapshot)


def reduce_watchdog_cycle(
    *,
    profile: dict[str, Any],
    snapshot: dict[str, Any],
    previous: dict[str, Any] | None,
    stale_cycle_threshold: int,
) -> dict[str, Any]:
    """Classify one evidence snapshot without worker or control-plane effects."""
    primitives = _profile_primitives(profile)
    if type(stale_cycle_threshold) is not int or stale_cycle_threshold < 2:
        raise LoopEngineeringError("stale_cycle_threshold must be at least 2")
    run_id = _required_id(snapshot, "run_id")
    workflow_revision = _required_id(snapshot, "workflow_revision")
    evidence_digest = snapshot_digest(snapshot)
    progress_digest = _progress_digest(snapshot)
    health = _mapping(snapshot.get("health"))
    state = _health_state(health)
    previous_progress = str(_mapping(previous).get("progress_digest") or "")
    previous_stale_cycles = _nonnegative_int(
        _mapping(previous).get("stale_cycles")
    )
    if state:
        stale_cycles = 0
    elif _all_lanes_terminal(snapshot):
        state = "terminal"
        stale_cycles = 0
    elif progress_digest == previous_progress:
        stale_cycles = previous_stale_cycles + 1
        if stale_cycles >= stale_cycle_threshold:
            state = "suspected_stall"
        else:
            state = "healthy"
    else:
        stale_cycles = 1
        state = "healthy"

    structural_join = (
        evaluate_structural_join(profile=profile, snapshot=snapshot)
        if "fork_join" in primitives and profile.get("structural_join") is not None
        else None
    )
    incident = (
        _incident(
            run_id=run_id,
            workflow_revision=workflow_revision,
            kind=state,
            evidence_digest=evidence_digest,
            primitives=primitives,
            snapshot=snapshot,
        )
        if state in INCIDENT_KINDS
        else None
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "workflow_revision": workflow_revision,
        "profile_primitives": primitives,
        "state": state,
        "stale_cycles": stale_cycles,
        "progress_digest": progress_digest,
        "evidence_snapshot_digest": evidence_digest,
        "structural_join": structural_join,
        "incident": incident,
        "worker_effects_executed": 0,
    }


def evaluate_structural_join(
    *,
    profile: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Check declared fork-join structure without judging implementation meaning."""
    primitives = _profile_primitives(profile)
    if "fork_join" not in primitives:
        raise LoopEngineeringError(
            "structural join requires the fork_join profile primitive"
        )
    contract = _mapping(profile.get("structural_join"))
    required_lanes = _required_id_list(
        contract.get("required_lanes"),
        "structural_join required_lanes",
    )
    required_artifacts = _required_id_list(
        contract.get("required_artifacts"),
        "structural_join required_artifacts",
    )
    require_same_base_revision = contract.get("require_same_base_revision")
    if type(require_same_base_revision) is not bool:
        raise LoopEngineeringError(
            "structural_join require_same_base_revision must be boolean"
        )

    lanes = _mapping(snapshot.get("lanes"))
    blockers: list[str] = []
    base_revisions: list[str] = []
    for lane_id in required_lanes:
        lane = _mapping(lanes.get(lane_id))
        if str(lane.get("terminal_state") or "") != "succeeded":
            blockers.append(f"lane_not_succeeded:{lane_id}")
            continue
        artifacts = _mapping(lane.get("artifacts"))
        for artifact_name in required_artifacts:
            if not str(artifacts.get(artifact_name) or ""):
                blockers.append(f"artifact_missing:{lane_id}:{artifact_name}")
        if require_same_base_revision:
            base_revision = str(lane.get("base_revision") or "")
            if not base_revision:
                blockers.append(f"base_revision_missing:{lane_id}")
            else:
                base_revisions.append(base_revision)
    if require_same_base_revision and len(set(base_revisions)) > 1:
        blockers.append("base_revision_mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_primitives": primitives,
        "state": "join_contract_satisfied" if not blockers else "join_blocked",
        "required_lanes": required_lanes,
        "required_artifacts": required_artifacts,
        "blockers": blockers,
        "semantic_compatibility_evaluated": False,
        "worker_effects_executed": 0,
    }


def validate_main_command(
    *,
    command: dict[str, Any],
    incident: dict[str, Any],
) -> dict[str, Any]:
    """Validate a Main-owned command against immutable incident evidence."""
    if not isinstance(command, dict):
        raise LoopEngineeringError("main command must be an object")
    command_id = _required_id(command, "command_id")
    incident_id = _required_id(command, "incident_id")
    action = str(command.get("action") or "")
    if action not in MAIN_COMMAND_ACTIONS:
        raise LoopEngineeringError("main command action is not allowed")
    if incident_id != _required_id(incident, "incident_id"):
        raise LoopEngineeringError("main command incident does not match")
    if str(incident.get("kind") or "") not in INCIDENT_KINDS:
        raise LoopEngineeringError("incident kind is not allowed")
    evidence_digest = str(command.get("evidence_snapshot_digest") or "")
    _required_digest(evidence_digest, "main command evidence digest")
    incident_digest = _required_digest(
        str(incident.get("evidence_snapshot_digest") or ""),
        "incident evidence digest",
    )
    if evidence_digest != incident_digest:
        raise LoopEngineeringError("main command has stale evidence")
    return {
        "schema_version": SCHEMA_VERSION,
        "command_id": command_id,
        "incident_id": incident_id,
        "evidence_snapshot_digest": evidence_digest,
        "action": action,
    }


def build_main_command_result(
    *,
    command: dict[str, Any],
    incident: dict[str, Any],
) -> dict[str, Any]:
    """Build an artifact-only result after command validation."""
    normalized = validate_main_command(command=command, incident=incident)
    action = normalized["action"]
    next_state = {
        "acknowledge": "acknowledged",
        "continue_observing": "continue_observing",
        "request_status": "awaiting_status",
        "request_replan": "awaiting_replan",
        "stop": "stopped",
    }[action]
    packet: dict[str, Any] | None = None
    if action in {"request_status", "request_replan"}:
        packet = {
            "schema_version": SCHEMA_VERSION,
            "kind": "status_request" if action == "request_status" else "replan_request",
            "incident_id": normalized["incident_id"],
            "evidence_snapshot_digest": normalized["evidence_snapshot_digest"],
            "requires_main_decision": True,
            "would_contact_worker": False,
        }
    return {
        **normalized,
        "status": "accepted",
        "next_state": next_state,
        "leader_packet": packet,
        "worker_effects_executed": 0,
    }


def reduce_thinker_feedback(
    *,
    review_bundle: dict[str, Any],
    current_snapshot_digest: str,
    workflow_revision: str,
    previous: dict[str, Any] | None,
    max_rounds: int,
) -> dict[str, Any]:
    """Route advisory Thinker output without granting workflow authority."""
    if type(max_rounds) is not int or max_rounds < 1:
        raise LoopEngineeringError("max_rounds must be positive")
    recommendation = str(review_bundle.get("recommendation") or "")
    reviewed_digest = str(review_bundle.get("reviewed_snapshot_digest") or "")
    reviewed_revision = str(review_bundle.get("workflow_revision") or "")
    _required_digest(current_snapshot_digest, "current snapshot digest")
    _required_digest(reviewed_digest, "reviewed snapshot digest")
    if recommendation not in {
        "recommend_accept",
        "recommend_revise",
        "recommend_block",
    }:
        return _thinker_result("needs_human", "invalid_advisory", 0, "")
    if reviewed_digest != current_snapshot_digest or reviewed_revision != workflow_revision:
        return _thinker_result("stale_advisory", "reviewed_snapshot_stale", 0, reviewed_digest)
    old = _mapping(previous)
    same_snapshot = str(old.get("reviewed_snapshot_digest") or "") == reviewed_digest
    round_number = _nonnegative_int(old.get("round_number")) + (
        0 if same_snapshot else 1
    )
    if same_snapshot:
        return _thinker_result(
            "awaiting_main_adoption",
            "unchanged_snapshot_no_repeat",
            round_number,
            reviewed_digest,
        )
    if round_number > max_rounds:
        return _thinker_result(
            "needs_human",
            "review_round_limit_reached",
            round_number,
            reviewed_digest,
        )
    state = {
        "recommend_accept": "awaiting_main_adoption",
        "recommend_revise": "awaiting_main_revision",
        "recommend_block": "needs_human",
    }[recommendation]
    return _thinker_result(state, recommendation, round_number, reviewed_digest)


def build_main_adoption_request(
    *,
    feedback_id: str,
    feedback: dict[str, Any],
    review_bundle: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the advisory-only request that only Main may adopt or reject."""
    if not SAFE_ID.fullmatch(feedback_id):
        raise LoopEngineeringError("feedback_id is invalid")
    state = str(feedback.get("state") or "")
    reason = str(feedback.get("reason") or "")
    if state not in {"awaiting_main_adoption", "awaiting_main_revision"}:
        return None
    if reason == "unchanged_snapshot_no_repeat":
        return None
    recommendation = str(review_bundle.get("recommendation") or "")
    if recommendation not in {"recommend_accept", "recommend_revise"}:
        return None
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": (
            "main_adoption_request"
            if state == "awaiting_main_adoption"
            else "main_revision_request"
        ),
        "feedback_id": feedback_id,
        "workflow_revision": str(feedback.get("workflow_revision") or review_bundle.get("workflow_revision") or ""),
        "reviewed_snapshot_digest": str(
            feedback.get("reviewed_snapshot_digest")
            or review_bundle.get("reviewed_snapshot_digest")
            or ""
        ),
        "recommendation": recommendation,
        "requires_main_decision": True,
        "authorizes_edges": [],
        "worker_effects_executed": 0,
    }


def build_advisory_review(
    *,
    feedback_id: str,
    feedback: dict[str, Any],
    review_bundle: dict[str, Any],
) -> dict[str, Any]:
    """Build the parent-facing advisory artifact without exposing inner workers."""
    if not SAFE_ID.fullmatch(feedback_id):
        raise LoopEngineeringError("feedback_id is invalid")
    recommendation = str(review_bundle.get("recommendation") or "")
    if recommendation not in {
        "recommend_accept",
        "recommend_revise",
        "recommend_block",
    }:
        raise LoopEngineeringError("advisory recommendation is invalid")
    workflow_revision = _required_id(review_bundle, "workflow_revision")
    reviewed_snapshot_digest = _required_digest(
        str(review_bundle.get("reviewed_snapshot_digest") or ""),
        "advisory reviewed snapshot digest",
    )
    if feedback.get("authorizes_edges") != []:
        raise LoopEngineeringError("advisory feedback must not authorize edges")
    if feedback.get("worker_effects_executed") != 0:
        raise LoopEngineeringError("advisory feedback must have zero worker effects")
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "advisory_review",
        "authority": "advisory_only",
        "feedback_id": feedback_id,
        "workflow_revision": workflow_revision,
        "reviewed_snapshot_digest": reviewed_snapshot_digest,
        "recommendation": recommendation,
        "feedback_state": str(feedback.get("state") or ""),
        "authorizes_edges": [],
        "worker_effects_executed": 0,
    }


def validate_main_adoption_decision(
    *,
    decision: dict[str, Any],
    adoption_request: dict[str, Any],
) -> dict[str, Any]:
    """Validate a Main-only adoption decision against its advisory request."""
    if not isinstance(decision, dict):
        raise LoopEngineeringError("main adoption decision must be an object")
    if adoption_request.get("schema_version") != SCHEMA_VERSION:
        raise LoopEngineeringError("adoption request schema version is invalid")
    request_kind = str(adoption_request.get("kind") or "")
    if request_kind not in {
        "main_adoption_request",
        "main_revision_request",
    }:
        raise LoopEngineeringError("adoption request kind is invalid")
    if adoption_request.get("requires_main_decision") is not True:
        raise LoopEngineeringError("adoption request is not Main-owned")
    if adoption_request.get("authorizes_edges") != []:
        raise LoopEngineeringError("adoption request must not authorize edges")
    if adoption_request.get("worker_effects_executed") != 0:
        raise LoopEngineeringError("adoption request must have zero worker effects")
    feedback_id = _required_id(decision, "feedback_id")
    if feedback_id != _required_id(adoption_request, "feedback_id"):
        raise LoopEngineeringError("main adoption feedback does not match")
    source_workflow_revision = _required_id(
        adoption_request,
        "workflow_revision",
    )
    reviewed_snapshot_digest = _required_digest(
        str(adoption_request.get("reviewed_snapshot_digest") or ""),
        "adoption request reviewed snapshot digest",
    )
    recommendation = str(adoption_request.get("recommendation") or "")
    if recommendation not in {"recommend_accept", "recommend_revise"}:
        raise LoopEngineeringError("adoption request recommendation is invalid")
    if {
        "recommend_accept": "main_adoption_request",
        "recommend_revise": "main_revision_request",
    }[recommendation] != request_kind:
        raise LoopEngineeringError(
            "adoption request recommendation does not match kind"
        )

    decision_id = _required_id(decision, "decision_id")
    result = str(decision.get("result") or "")
    if result not in MAIN_ADOPTION_RESULTS:
        raise LoopEngineeringError("main adoption result is invalid")
    candidate_plan_digest = _required_digest(
        str(decision.get("candidate_plan_digest") or ""),
        "candidate plan digest",
    )
    finding_dispositions = _normalize_finding_dispositions(
        decision.get("finding_dispositions")
    )

    frozen_workflow: dict[str, str] | None = None
    if result != "rejected":
        next_workflow_revision = _required_id(
            decision,
            "next_workflow_revision",
        )
        if next_workflow_revision == source_workflow_revision:
            raise LoopEngineeringError("next workflow revision must change")
        next_workflow_digest = _required_digest(
            str(decision.get("next_workflow_digest") or ""),
            "next workflow digest",
        )
        frozen_workflow = {
            "status": "workflow_revision_ready",
            "adoption_decision_id": decision_id,
            "workflow_revision": next_workflow_revision,
            "workflow_digest": next_workflow_digest,
        }
    elif decision.get("next_workflow_revision") or decision.get("next_workflow_digest"):
        raise LoopEngineeringError("rejected adoption must not freeze a workflow")

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "main_adoption_decision",
        "decision_id": decision_id,
        "feedback_id": feedback_id,
        "owner": "main",
        "source_workflow_revision": source_workflow_revision,
        "reviewed_snapshot_digest": reviewed_snapshot_digest,
        "recommendation": recommendation,
        "result": result,
        "candidate_plan_digest": candidate_plan_digest,
        "finding_dispositions": finding_dispositions,
        "frozen_workflow": frozen_workflow,
        "requires_main_decision": False,
        "authorizes_edges": [],
        "worker_effects_executed": 0,
    }


def evaluate_live_pilot_gate(
    *,
    profile: dict[str, Any],
    policy: dict[str, Any],
    grant: dict[str, Any],
    snapshot: dict[str, Any],
    edge_id: str,
    active_incidents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Report a no-write pilot candidate; it never creates a dispatch intent."""
    primitives = _profile_primitives(profile)
    blockers: list[str] = []
    edge = _profile_edge(profile, edge_id)
    effect_class = str(edge.get("effect_class") or "")
    target_node = str(edge.get("target_node") or "")
    if not edge:
        blockers.append("edge_not_declared")
    elif effect_class != "read_only_worker":
        blockers.append(f"unsafe_effect_class:{effect_class or 'missing'}")
    if not SAFE_ID.fullmatch(target_node):
        blockers.append("edge_target_missing")
    if str(policy.get("mode") or "") != "preauthorized_followups":
        blockers.append("policy_mode_not_preauthorized_followups")
    execution_authorization = _mapping(policy.get("execution_authorization"))
    if not bool(execution_authorization.get("enabled")):
        blockers.append("policy_execution_authorization_disabled")
    policy_allowed_edges = execution_authorization.get("allowed_edge_ids")
    if (
        not isinstance(policy_allowed_edges, list)
        or edge_id not in policy_allowed_edges
    ):
        blockers.append("edge_not_permitted_by_policy")
    if str(grant.get("state") or "") != "active":
        blockers.append("authorization_not_active")
    allowed_edges = grant.get("allowed_edge_ids")
    if not isinstance(allowed_edges, list) or edge_id not in allowed_edges:
        blockers.append("edge_not_authorized")
    snapshot_revision = str(snapshot.get("workflow_revision") or "")
    if str(policy.get("workflow_revision") or "") != snapshot_revision:
        blockers.append("policy_workflow_revision_mismatch")
    if str(grant.get("workflow_revision") or "") != snapshot_revision:
        blockers.append("authorization_workflow_revision_mismatch")
    limits = _mapping(policy.get("actuation_limits"))
    if _nonnegative_int(limits.get("max_auto_hops")) != 1:
        blockers.append("max_auto_hops_exceeds_pilot_limit")
    max_dispatches = _nonnegative_int(limits.get("max_worker_dispatches"))
    if max_dispatches != 1:
        blockers.append("max_worker_dispatches_exceeds_pilot_limit")
    budget = _mapping(snapshot.get("budget"))
    dispatches_used = budget.get("worker_dispatches_used")
    if type(dispatches_used) is not int or dispatches_used < 0:
        blockers.append("worker_dispatch_budget_missing")
    elif dispatches_used >= max_dispatches:
        blockers.append("worker_dispatch_budget_exhausted")
    if target_node:
        binding = _mapping(_mapping(snapshot.get("target_bindings")).get(target_node))
        binding_status = str(binding.get("binding_status") or "")
        if binding_status == "binding_drift":
            blockers.append(f"binding_drift:{target_node}")
        elif binding_status != "current_binding_ok":
            blockers.append(f"target_binding_not_current:{target_node}")
        if str(binding.get("binding_confidence") or "") != "strong":
            blockers.append(f"target_binding_not_strong:{target_node}")
    if _health_state(_mapping(snapshot.get("health"))):
        blockers.append("snapshot_health_not_clear")
    if active_incidents:
        blockers.append("active_incident_present")
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_primitives": primitives,
        "edge_id": edge_id,
        "effect_class": effect_class,
        "target_node": target_node,
        "eligible": not blockers,
        "blockers": blockers,
        "would_dispatch": False,
        "worker_effects_executed": 0,
    }


def _incident(
    *,
    run_id: str,
    workflow_revision: str,
    kind: str,
    evidence_digest: str,
    primitives: list[str],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    affected_nodes = _affected_nodes(snapshot)
    policy = INCIDENT_POLICIES[kind]
    incident_id = _digest(
        {
            "run_id": run_id,
            "workflow_revision": workflow_revision,
            "kind": kind,
            "evidence_snapshot_digest": evidence_digest,
            "affected_nodes": affected_nodes,
        }
    ).removeprefix("sha256:")
    notification_fingerprint = _digest(
        {
            "run_id": run_id,
            "workflow_revision": workflow_revision,
            "kind": kind,
            "affected_nodes": affected_nodes,
        }
    ).removeprefix("sha256:")
    return {
        "schema_version": SCHEMA_VERSION,
        "incident_id": incident_id,
        "run_id": run_id,
        "workflow_revision": workflow_revision,
        "kind": kind,
        "severity": "error" if kind != "suspected_stall" else "warning",
        "affected_nodes": affected_nodes,
        "profile_primitives": primitives,
        "evidence_snapshot_digest": evidence_digest,
        "recommended_action": "main_recovery_decision",
        "owner": policy["owner"],
        "notification_fingerprint": notification_fingerprint,
        "notification_cooldown_cycles": policy[
            "notification_cooldown_cycles"
        ],
        "recovery_ceiling": policy["recovery_ceiling"],
        "requires_main_decision": True,
        "worker_effects_executed": 0,
    }


def _health_state(health: dict[str, Any]) -> str:
    provider_error = _mapping(health.get("provider_error"))
    if (
        provider_error.get("detected") is True
        and str(provider_error.get("kind") or "")
        == "proxy_request_budget_exhausted"
    ):
        return "provider_request_budget_exhausted"
    for field, state in (
        ("terminal_conflict", "terminal_truth_invalid"),
        ("evidence_invalid", "terminal_truth_invalid"),
        ("connection_lost", "connection_lost"),
        ("binding_drift", "binding_drift"),
        ("received_without_started", "received_without_started"),
        ("delivery_uncertain", "delivery_uncertain"),
        ("budget_exhausted", "budget_exhausted"),
    ):
        if bool(health.get(field)):
            return state
    return ""


def _all_lanes_terminal(snapshot: dict[str, Any]) -> bool:
    lanes = _mapping(snapshot.get("lanes"))
    if not lanes:
        return False
    terminal_states = {"succeeded", "failed", "blocked", "invalid"}
    return all(
        str(_mapping(lane).get("terminal_state") or "") in terminal_states
        for lane in lanes.values()
    )


def _affected_nodes(snapshot: dict[str, Any]) -> list[str]:
    lanes = _mapping(snapshot.get("lanes"))
    running = sorted(
        node_id
        for node_id, lane in lanes.items()
        if str(_mapping(lane).get("terminal_state") or "") in {"", "running"}
    )
    return running or ["run"]


def _progress_digest(snapshot: dict[str, Any]) -> str:
    lanes = _mapping(snapshot.get("lanes"))
    compact_lanes = {
        node_id: {
            key: _mapping(lane).get(key)
            for key in (
                "attempt_id",
                "terminal_state",
                "progress_token",
                "report_digest",
                "terminal_evidence_digest",
                "task_contract_digest",
            )
        }
        for node_id, lane in sorted(lanes.items())
    }
    return _digest(
        {
            "lanes": compact_lanes,
            "artifacts": _mapping(snapshot.get("artifacts")),
            "advisory": _mapping(snapshot.get("advisory")),
        }
    )


def _profile_primitives(profile: dict[str, Any]) -> list[str]:
    primitives = profile.get("primitives")
    if not isinstance(primitives, list) or not primitives:
        raise LoopEngineeringError("profile primitives are required")
    normalized = [str(item) for item in primitives]
    if any(item not in KNOWN_PRIMITIVES for item in normalized):
        raise LoopEngineeringError("profile contains an unsupported primitive")
    return sorted(set(normalized))


def _thinker_result(
    state: str,
    reason: str,
    round_number: int,
    reviewed_snapshot_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "reason": reason,
        "round_number": round_number,
        "reviewed_snapshot_digest": reviewed_snapshot_digest,
        "authorizes_edges": [],
        "worker_effects_executed": 0,
    }


def _required_id(value: dict[str, Any], field: str) -> str:
    result = str(value.get(field) or "")
    if not SAFE_ID.fullmatch(result):
        raise LoopEngineeringError(f"{field} is invalid")
    return result


def _required_id_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise LoopEngineeringError(f"{field} must be a non-empty list")
    result = [str(item or "") for item in value]
    if len(set(result)) != len(result) or any(
        not SAFE_ID.fullmatch(item) for item in result
    ):
        raise LoopEngineeringError(f"{field} is invalid")
    return result


def _normalize_finding_dispositions(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise LoopEngineeringError("finding_dispositions must be a list")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise LoopEngineeringError("finding disposition must be an object")
        finding_id = _required_id(raw, "finding_id")
        if finding_id in seen:
            raise LoopEngineeringError("finding_dispositions must be unique")
        disposition = str(raw.get("decision") or "")
        if disposition not in FINDING_DISPOSITIONS:
            raise LoopEngineeringError("finding disposition decision is invalid")
        rationale = " ".join(str(raw.get("rationale") or "").split())
        if not rationale:
            raise LoopEngineeringError("finding disposition rationale is required")
        normalized.append(
            {
                "finding_id": finding_id,
                "decision": disposition,
                "rationale": rationale,
            }
        )
        seen.add(finding_id)
    return normalized


def _required_digest(value: str, field: str) -> str:
    if not SHA256_DIGEST.fullmatch(value):
        raise LoopEngineeringError(f"{field} is invalid")
    return value


def _profile_edge(profile: dict[str, Any], edge_id: str) -> dict[str, Any]:
    edges = profile.get("edges")
    if not isinstance(edges, list):
        return {}
    for edge in edges:
        if isinstance(edge, dict) and str(edge.get("edge_id") or "") == edge_id:
            return edge
    return {}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nonnegative_int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
