"""Pure deterministic reducer and arbiter for one supervisor cycle."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from visible_agent_orchestrator.collaboration_profile import profile_edge
from visible_agent_orchestrator.supervision_policy import (
    authorization_grant_allows,
    supervision_policy_digest,
)


def evidence_snapshot_digest(snapshot: dict[str, Any]) -> str:
    return _digest(snapshot)


def logical_action_id(
    *,
    run_id: str,
    workflow_revision: str,
    edge_id: str,
    prerequisite_attempt_ids: list[str],
    target_task_contract_digest: str,
) -> str:
    return _digest(
        {
            "run_id": run_id,
            "workflow_revision": workflow_revision,
            "edge_id": edge_id,
            "prerequisite_attempt_ids": sorted(prerequisite_attempt_ids),
            "target_task_contract_digest": target_task_contract_digest,
        }
    )


def supervision_decision_id(
    *,
    logical_id: str,
    policy_digest: str,
    snapshot_digest: str,
    lease_epoch: int,
) -> str:
    return _digest(
        {
            "logical_action_id": logical_id,
            "policy_digest": policy_digest,
            "evidence_snapshot_digest": snapshot_digest,
            "supervisor_lease_epoch": int(lease_epoch),
        }
    )


def evaluate_supervision(
    *,
    profile: dict[str, Any],
    manifest: dict[str, Any],
    policy: dict[str, Any],
    grant: dict[str, Any] | None,
    snapshot: dict[str, Any],
    lease_epoch: int,
) -> dict[str, Any]:
    """Reduce immutable evidence into one disposition without side effects."""
    snapshot_digest = evidence_snapshot_digest(snapshot)
    health = _dict(snapshot.get("health"))
    lane_states = _lane_protocol_states(manifest, snapshot)
    edge_states = _edge_states(profile, manifest, policy, snapshot, lane_states)

    safety = _safety_disposition(health)
    if safety:
        return _decision(
            disposition="escalate",
            reason=safety,
            edge_id="",
            edge_states=edge_states,
            lane_states=lane_states,
            snapshot_digest=snapshot_digest,
            policy=policy,
            lease_epoch=lease_epoch,
        )

    if any(state == "invalid" for state in lane_states.values()):
        return _decision(
            disposition="escalate",
            reason="terminal_truth_invalid",
            edge_id="",
            edge_states=edge_states,
            lane_states=lane_states,
            snapshot_digest=snapshot_digest,
            policy=policy,
            lease_epoch=lease_epoch,
        )

    if (
        str(snapshot.get("run_id") or "") != str(manifest.get("run_id") or "")
        or str(snapshot.get("workflow_revision") or "")
        != str(manifest.get("workflow_revision") or "")
    ):
        return _decision(
            disposition="stop",
            reason="workflow_revision_stale",
            edge_id="",
            edge_states=_supersede_all(edge_states),
            lane_states=lane_states,
            snapshot_digest=snapshot_digest,
            policy=policy,
            lease_epoch=lease_epoch,
        )

    if any(state == "superseded" for state in edge_states.values()):
        return _decision(
            disposition="stop",
            reason="workflow_revision_stale",
            edge_id="",
            edge_states=edge_states,
            lane_states=lane_states,
            snapshot_digest=snapshot_digest,
            policy=policy,
            lease_epoch=lease_epoch,
        )

    if any(state == "blocked" for state in edge_states.values()):
        return _decision(
            disposition="escalate",
            reason="replan_required",
            edge_id="",
            edge_states=edge_states,
            lane_states=lane_states,
            snapshot_digest=snapshot_digest,
            policy=policy,
            lease_epoch=lease_epoch,
        )

    grant_state = str((grant or {}).get("state") or "")
    if grant_state in {"paused", "revoked", "draining"}:
        return _decision(
            disposition="observe",
            reason=f"authorization_{grant_state}",
            edge_id="",
            edge_states=edge_states,
            lane_states=lane_states,
            snapshot_digest=snapshot_digest,
            policy=policy,
            lease_epoch=lease_epoch,
        )

    if str(manifest.get("adoption", {}).get("status") or "") != "workflow_revision_ready":
        status = str(manifest.get("adoption", {}).get("status") or "")
        reason = (
            status
            if status in {"awaiting_main_adoption", "awaiting_main_revision"}
            else "awaiting_main_adoption"
        )
        return _decision(
            disposition="observe",
            reason=reason,
            edge_id="",
            edge_states=edge_states,
            lane_states=lane_states,
            snapshot_digest=snapshot_digest,
            policy=policy,
            lease_epoch=lease_epoch,
        )

    eligible_edges = [
        edge_id
        for edge_id, state in edge_states.items()
        if state == "eligible"
    ]
    if not eligible_edges:
        if _all_terminal(lane_states):
            return _decision(
                disposition="stop",
                reason="complete",
                edge_id="",
                edge_states=edge_states,
                lane_states=lane_states,
                snapshot_digest=snapshot_digest,
                policy=policy,
                lease_epoch=lease_epoch,
            )
        return _decision(
            disposition="observe",
            reason="continue_observing",
            edge_id="",
            edge_states=edge_states,
            lane_states=lane_states,
            snapshot_digest=snapshot_digest,
            policy=policy,
            lease_epoch=lease_epoch,
        )

    edge_id = eligible_edges[0]
    edge = profile_edge(profile, edge_id)
    logical_id = _logical_id_for_edge(manifest, edge)
    decision_id = supervision_decision_id(
        logical_id=logical_id,
        policy_digest=supervision_policy_digest(policy),
        snapshot_digest=snapshot_digest,
        lease_epoch=lease_epoch,
    )
    mode = str(policy.get("mode") or "")
    requires_worker_binding = edge["effect_class"] in {
        "read_only_worker",
        "write_isolated",
        "shared_write",
    }
    effect_is_automatable = edge["effect_class"] in {
        "artifact_only",
        "local_no_live",
        "read_only_worker",
        "write_isolated",
    }
    can_act = (
        effect_is_automatable
        and
        mode == "preauthorized_followups"
        and bool(policy.get("execution_authorization", {}).get("enabled"))
        and grant is not None
        and authorization_grant_allows(grant, edge_id)
        and (not requires_worker_binding or _target_binding_ready(snapshot, edge))
    )
    if can_act:
        return {
            **_decision(
                disposition="act",
                reason="handoff_eligible",
                edge_id=edge_id,
                edge_states=edge_states,
                lane_states=lane_states,
                snapshot_digest=snapshot_digest,
                policy=policy,
                lease_epoch=lease_epoch,
            ),
            "logical_action_id": logical_id,
            "decision_id": decision_id,
            "effect_class": edge["effect_class"],
            "source_nodes": list(edge["source_nodes"]),
            "target_node": edge["target_node"],
            "would_act": True,
        }
    if not effect_is_automatable and mode == "preauthorized_followups":
        disposition = "observe"
        reason = "effect_requires_human"
    else:
        disposition = "propose" if mode == "shadow" else "observe"
        reason = "shadow_handoff_eligible" if mode == "shadow" else "handoff_not_authorized"
    return {
        **_decision(
            disposition=disposition,
            reason=reason,
            edge_id=edge_id,
            edge_states=edge_states,
            lane_states=lane_states,
            snapshot_digest=snapshot_digest,
            policy=policy,
            lease_epoch=lease_epoch,
        ),
        "logical_action_id": logical_id,
        "decision_id": decision_id,
        "effect_class": edge["effect_class"],
        "source_nodes": list(edge["source_nodes"]),
        "target_node": edge["target_node"],
        "would_act": False,
    }


def _lane_protocol_states(
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, str]:
    snapshot_lanes = _dict(snapshot.get("lanes"))
    states: dict[str, str] = {}
    for lane in manifest.get("lanes", []):
        if not isinstance(lane, dict):
            continue
        node_id = str(lane.get("node_id") or "")
        observed = _dict(snapshot_lanes.get(node_id))
        if not observed:
            states[node_id] = "pending"
            continue
        if str(observed.get("attempt_id") or "") != str(lane.get("attempt_id") or ""):
            states[node_id] = "superseded"
            continue
        terminal = str(observed.get("terminal_state") or "")
        if terminal == "succeeded":
            if (
                not str(observed.get("report_digest") or "")
                or not str(observed.get("terminal_evidence_digest") or "")
                or str(observed.get("task_contract_digest") or "")
                != str(lane.get("task_contract_digest") or "")
            ):
                states[node_id] = "invalid"
            else:
                states[node_id] = "succeeded"
        elif terminal in {"failed", "fail"}:
            states[node_id] = "failed"
        elif terminal == "blocked":
            states[node_id] = "blocked"
        elif terminal == "running":
            states[node_id] = "running"
        elif terminal:
            states[node_id] = "invalid"
        else:
            states[node_id] = "running"
    return states


def _edge_states(
    profile: dict[str, Any],
    manifest: dict[str, Any],
    policy: dict[str, Any],
    snapshot: dict[str, Any],
    lane_states: dict[str, str],
) -> dict[str, str]:
    states: dict[str, str] = {}
    for edge_id in policy.get("candidate_edges", []):
        edge = profile_edge(profile, edge_id)
        join_state = _join_state(edge, lane_states)
        if join_state != "eligible":
            states[edge_id] = join_state
            continue
        if not _proof_satisfied(edge, manifest, snapshot, lane_states):
            states[edge_id] = "waiting"
            continue
        states[edge_id] = "eligible"
    return states


def _join_state(edge: dict[str, Any], lane_states: dict[str, str]) -> str:
    sources = [str(item) for item in edge.get("source_nodes", [])]
    optional = {
        str(item) for item in edge.get("optional_source_nodes", [])
    }
    required = [node_id for node_id in sources if node_id not in optional]
    states = {node_id: lane_states.get(node_id, "pending") for node_id in sources}
    if edge.get("join") == "quorum":
        if any(state == "superseded" for state in states.values()):
            return "superseded"
        successes = sum(state == "succeeded" for state in states.values())
        possible_successes = successes + sum(
            state in {"pending", "running"} for state in states.values()
        )
        required_successes = int(edge.get("required_successes") or 0)
        if successes >= required_successes:
            return "eligible"
        return "blocked" if possible_successes < required_successes else "waiting"

    required_states = [states[node_id] for node_id in required]
    if "superseded" in required_states:
        return "superseded"
    if any(state in {"failed", "blocked", "invalid"} for state in required_states):
        return "blocked"
    return (
        "eligible"
        if all(state == "succeeded" for state in required_states)
        else "waiting"
    )


def _proof_satisfied(
    edge: dict[str, Any],
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    lane_states: dict[str, str],
) -> bool:
    source_nodes = {str(item) for item in edge.get("source_nodes", [])}
    for proof in edge.get("proof_requirements", []):
        if not isinstance(proof, dict):
            return False
        kind = str(proof.get("kind") or "")
        node_id = str(proof.get("node_id") or "")
        if kind == "terminal_success":
            if node_id in source_nodes:
                continue
            if lane_states.get(node_id) != "succeeded":
                return False
        elif kind == "artifact_present":
            artifacts = _dict(snapshot.get("artifacts"))
            if not bool(artifacts.get(node_id)):
                return False
        elif kind == "advisory_ready":
            advisory = _dict(snapshot.get("advisory"))
            if str(advisory.get("state") or "") != "ready":
                return False
        elif kind == "main_adoption_present":
            if str(manifest.get("adoption", {}).get("status") or "") != "workflow_revision_ready":
                return False
        elif kind == "gate_passed":
            gates = _dict(snapshot.get("gates"))
            if not bool(gates.get(node_id)):
                return False
        else:
            return False
    return True


def _safety_disposition(health: dict[str, Any]) -> str:
    if bool(health.get("terminal_conflict")) or bool(health.get("evidence_invalid")):
        return "terminal_truth_invalid"
    if bool(health.get("binding_drift")):
        return "binding_drift"
    if bool(health.get("delivery_uncertain")):
        return "delivery_uncertain"
    if bool(health.get("budget_exhausted")):
        return "budget_exhausted"
    if bool(health.get("internal_error")):
        return "internal_error"
    return ""


def _target_binding_ready(snapshot: dict[str, Any], edge: dict[str, Any]) -> bool:
    target_bindings = _dict(snapshot.get("target_bindings"))
    binding = _dict(target_bindings.get(str(edge.get("target_node") or "")))
    return bool(
        binding
        and str(binding.get("worker_id") or "")
        and str(binding.get("instance_id") or "")
        and int(binding.get("binding_generation") or 0) > 0
        and int(binding.get("fencing_generation") or 0) > 0
        and str(binding.get("lock_state") or "") == "unlocked"
        and bool(binding.get("fresh_idle"))
    )


def _logical_id_for_edge(manifest: dict[str, Any], edge: dict[str, Any]) -> str:
    prerequisite_attempt_ids = [
        str(lane.get("attempt_id") or "")
        for lane in manifest.get("lanes", [])
        if isinstance(lane, dict)
        and str(lane.get("node_id") or "") in set(edge.get("source_nodes") or [])
    ]
    target = _dict(_dict(manifest.get("targets")).get(str(edge.get("target_node") or "")))
    target_contract = {
        "target_node": edge.get("target_node"),
        "task_title": target.get("task_title"),
        "task_body": target.get("task_body"),
        "effect_class": edge.get("effect_class"),
    }
    if edge.get("effect_class") == "write_isolated":
        target_contract["isolated_write"] = target.get("isolated_write")
    target_contract_digest = _digest(target_contract)
    return logical_action_id(
        run_id=str(manifest.get("run_id") or ""),
        workflow_revision=str(manifest.get("workflow_revision") or ""),
        edge_id=str(edge.get("edge_id") or ""),
        prerequisite_attempt_ids=prerequisite_attempt_ids,
        target_task_contract_digest=target_contract_digest,
    )


def _decision(
    *,
    disposition: str,
    reason: str,
    edge_id: str,
    edge_states: dict[str, str],
    lane_states: dict[str, str],
    snapshot_digest: str,
    policy: dict[str, Any],
    lease_epoch: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "disposition": disposition,
        "reason": reason,
        "selected_edge_id": edge_id,
        "edge_states": edge_states,
        "lane_states": lane_states,
        "run_health": "normal" if disposition not in {"escalate", "stop"} else reason,
        "evidence_snapshot_digest": snapshot_digest,
        "policy_digest": supervision_policy_digest(policy),
        "supervisor_lease_epoch": int(lease_epoch),
        "logical_action_id": "",
        "decision_id": "",
        "effect_class": "",
        "source_nodes": [],
        "target_node": "",
        "would_act": False,
    }


def _supersede_all(edge_states: dict[str, str]) -> dict[str, str]:
    return {edge_id: "superseded" for edge_id in edge_states}


def _all_terminal(lane_states: dict[str, str]) -> bool:
    return bool(lane_states) and all(
        state in {"succeeded", "failed", "blocked", "invalid", "superseded"}
        for state in lane_states.values()
    )


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
