"""Advisory Gate decision projection for Coder/Reviewer role evidence."""

from __future__ import annotations

from typing import Any

from .leader_decision import build_protocol_leader_decision


ALLOWED_GATE_DECISIONS = {"approve", "block", "rerun_coder", "human_review_required"}


class GateDecisionError(Exception):
    pass


def build_gate_decision(
    *,
    run_id: str,
    task_id: str,
    role_lanes: list[dict[str, Any]],
    planner_coverage: dict[str, Any] | None = None,
    semantic_summary: dict[str, Any] | None = None,
    semantic_retry_plan: dict[str, Any] | None = None,
    risk_review: dict[str, Any] | None = None,
    arbiter_decision: dict[str, Any] | None = None,
    created_at: str = "",
) -> dict[str, Any]:
    leader = build_protocol_leader_decision(
        run_id=run_id,
        task_id=task_id,
        required_lanes=role_lanes,
        planner_coverage=planner_coverage,
        semantic_summary=semantic_summary,
        semantic_retry_plan=semantic_retry_plan,
        risk_review=risk_review,
        arbiter_decision=arbiter_decision,
        created_at=created_at,
    )
    selected_action = str(leader.get("selected_action", ""))
    decision = {
        "accept": "approve",
        "block": "block",
        "rerun_lane": "rerun_coder",
        "rerun_planner": "rerun_coder",
        "human_review_required": "human_review_required",
    }.get(selected_action, "human_review_required")
    payload = {
        "schema_version": 1,
        "artifact_kind": "gate_decision",
        "advisory_only": True,
        "decision_id": _gate_decision_id(str(leader.get("run_id", "")), task_id),
        "created_at": str(leader.get("created_at", "")),
        "run_id": str(leader.get("run_id", "")),
        "task_id": str(leader.get("task_id", "")),
        "evaluated_lanes": _evaluated_lanes(leader.get("required_lanes"), role_lanes),
        "gate_inputs_summary": dict(leader.get("gate_inputs", {})),
        "decision": decision,
        "decision_source": _decision_source(leader),
        "leader_selected_action": selected_action,
        "reason": str(leader.get("reason", "")),
        "known_gaps": list(leader.get("known_gaps", [])),
        "forbidden_actions": [
            "do not dispatch workers",
            "do not release locks",
            "do not write terminal markers",
            "do not authorize business writes",
        ],
    }
    return validate_gate_decision(payload)


def validate_gate_decision(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise GateDecisionError("gate_decision_must_be_object")
    if int(payload.get("schema_version", 0) or 0) != 1:
        raise GateDecisionError("schema_version_must_be_1")
    if payload.get("artifact_kind") != "gate_decision":
        raise GateDecisionError("artifact_kind_must_be_gate_decision")
    if payload.get("advisory_only") is not True:
        raise GateDecisionError("advisory_only_must_be_true")
    decision = str(payload.get("decision", ""))
    if decision not in ALLOWED_GATE_DECISIONS:
        raise GateDecisionError("invalid_gate_decision")
    if not str(payload.get("decision_id", "")).strip():
        raise GateDecisionError("decision_id_is_required")
    if not str(payload.get("run_id", "")).strip():
        raise GateDecisionError("run_id_is_required")
    if not str(payload.get("task_id", "")).strip():
        raise GateDecisionError("task_id_is_required")
    if not isinstance(payload.get("evaluated_lanes"), list):
        raise GateDecisionError("evaluated_lanes_must_be_array")
    if not isinstance(payload.get("gate_inputs_summary"), dict):
        raise GateDecisionError("gate_inputs_summary_must_be_object")
    return payload


def _evaluated_lanes(leader_lanes: Any, role_lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    roles_by_lane = {str(item.get("lane_id", "")): str(item.get("role", "")) for item in role_lanes if isinstance(item, dict)}
    result: list[dict[str, Any]] = []
    if not isinstance(leader_lanes, list):
        return result
    for lane in leader_lanes:
        if not isinstance(lane, dict):
            continue
        item = dict(lane)
        role = roles_by_lane.get(str(item.get("lane_id", "")), "")
        if role:
            item["role"] = role
        result.append(item)
    return result


def _decision_source(leader: dict[str, Any]) -> str:
    if leader.get("selected_action") == "accept":
        return "protocol"
    gate_inputs = leader.get("gate_inputs", {})
    if isinstance(gate_inputs, dict):
        for key in ("semantic", "risk", "arbiter", "planner_coverage"):
            value = str(gate_inputs.get(key, ""))
            if value and value not in {"not_configured", "passed", "evaluated"}:
                return key
    return "protocol"


def _gate_decision_id(run_id: str, task_id: str) -> str:
    normalized = run_id.replace("/", "-")
    return f"gate-decision-{normalized}-{task_id}"
