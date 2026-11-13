from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from visible_agent_orchestrator.risk_review import RiskReviewError, normalize_risk_review


VALID_SELECTED_ACTIONS = {
    "accept",
    "rerun_lane",
    "rerun_planner",
    "block",
    "human_review_required",
}

TERMINAL_MARKERS = {".done", ".fail", ".blocked"}


class LeaderDecisionError(Exception):
    """Raised when a leader decision audit sidecar is malformed or unsafe."""


def validate_leader_decision(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate an advisory leader decision sidecar.

    The sidecar is intentionally audit-only. Validation must never imply worker
    completion or override report/terminal-marker truth.
    """

    if not isinstance(payload, dict):
        raise LeaderDecisionError("leader_decision_must_be_object")
    if payload.get("schema_version") != 1:
        raise LeaderDecisionError("unsupported_schema_version")
    if payload.get("artifact_kind") != "leader_decision":
        raise LeaderDecisionError("invalid_artifact_kind")
    if payload.get("advisory_only") is not True:
        raise LeaderDecisionError("leader_decision_must_be_advisory_only")

    selected_action = _required_str(payload, "selected_action")
    if selected_action not in VALID_SELECTED_ACTIONS:
        raise LeaderDecisionError(
            "unknown_selected_action:" + selected_action + "; valid_selected_actions:" + ",".join(sorted(VALID_SELECTED_ACTIONS))
        )

    for field in ("decision_id", "created_at", "run_id", "task_id", "reason"):
        _required_str(payload, field)

    gate_inputs = payload.get("gate_inputs")
    if not isinstance(gate_inputs, dict):
        raise LeaderDecisionError("missing_gate_inputs")
    if str(gate_inputs.get("protocol") or "") != "evaluated":
        raise LeaderDecisionError("protocol_gate_must_be_evaluated")

    lanes = payload.get("required_lanes")
    if not isinstance(lanes, list):
        raise LeaderDecisionError("required_lanes_must_be_list")
    for index, lane in enumerate(lanes):
        _validate_lane(lane, index=index)

    for field in ("unresolved_structural_conflicts", "known_gaps"):
        if not isinstance(payload.get(field), list):
            raise LeaderDecisionError(f"{field}_must_be_list")

    return payload


def build_protocol_leader_decision(
    *,
    run_id: str,
    task_id: str,
    required_lanes: list[dict[str, Any]],
    planner_coverage: dict[str, Any] | None = None,
    semantic_summary: dict[str, Any] | None = None,
    semantic_retry_plan: dict[str, Any] | None = None,
    risk_review: dict[str, Any] | None = None,
    arbiter_decision: dict[str, Any] | None = None,
    control_plane_thread_id: str = "",
    created_at: str = "",
) -> dict[str, Any]:
    """Build a Phase 0A advisory decision from protocol-only lane evidence."""

    normalized_lanes = [_normalize_lane(item) for item in required_lanes]
    selected_action, reason, known_gaps = _select_protocol_action(normalized_lanes)
    coverage_status, coverage_action, coverage_reason, coverage_gaps, coverage_payload = _planner_coverage_gate(
        planner_coverage
    )
    semantic_status, semantic_action, semantic_reason, semantic_gaps, semantic_payload = _semantic_gate(
        semantic_summary,
        semantic_retry_plan,
    )
    risk_status, risk_action, risk_reason, risk_gaps, risk_payload = _risk_gate(risk_review)
    arbiter_status, arbiter_action, arbiter_reason, arbiter_gaps, arbiter_payload = _arbiter_gate(arbiter_decision)
    if coverage_gaps:
        known_gaps.extend(coverage_gaps)
    if semantic_gaps:
        known_gaps.extend(semantic_gaps)
    if risk_gaps:
        known_gaps.extend(risk_gaps)
    if arbiter_gaps:
        known_gaps.extend(arbiter_gaps)
    if selected_action == "accept" and coverage_action:
        selected_action = coverage_action
        reason = coverage_reason
    if selected_action == "accept" and semantic_action:
        selected_action = semantic_action
        reason = semantic_reason
    if selected_action == "accept" and risk_action:
        selected_action = risk_action
        reason = risk_reason
    if selected_action == "accept" and arbiter_action:
        selected_action = arbiter_action
        reason = arbiter_reason
    normalized_run = _normalize_run_id(run_id)
    decision = {
        "schema_version": 1,
        "artifact_kind": "leader_decision",
        "advisory_only": True,
        "decision_id": _decision_id(normalized_run, task_id),
        "created_at": created_at or _utc_now(),
        "run_id": normalized_run,
        "task_id": _required_raw_str(task_id, "task_id"),
        "control_plane_thread_id": str(control_plane_thread_id or ""),
        "gate_inputs": {
            "protocol": "evaluated",
            "semantic": semantic_status,
            "risk": risk_status,
            "arbiter": arbiter_status,
            "planner_coverage": coverage_status,
        },
        "required_lanes": normalized_lanes,
        "unresolved_structural_conflicts": [],
        "known_gaps": known_gaps,
        "selected_action": selected_action,
        "reason": reason,
    }
    if coverage_payload:
        decision["planner_coverage"] = coverage_payload
    if semantic_payload:
        decision["semantic_review"] = semantic_payload
    if risk_payload:
        decision["risk_review"] = risk_payload
    if arbiter_payload:
        decision["arbiter_decision"] = arbiter_payload
    return validate_leader_decision(decision)


def _normalize_lane(lane: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(lane, dict):
        raise LeaderDecisionError("lane_must_be_object")
    markers = [_normalize_marker(item) for item in _list(lane.get("terminal_markers"))]
    report_present = _bool(lane.get("report_present"))
    report_non_empty = _bool(lane.get("report_non_empty"))
    submission_failed = _bool(lane.get("submission_failed"))
    protocol_status = _protocol_status(
        report_present,
        report_non_empty,
        markers,
        submission_failed=submission_failed,
    )
    return {
        "lane_id": _required_raw_str(lane.get("lane_id"), "lane_id"),
        "worker_id": _required_raw_str(lane.get("worker_id"), "worker_id"),
        "report_present": report_present,
        "report_non_empty": report_non_empty,
        "terminal_markers": markers,
        "terminal_marker_count": len(markers),
        "submission_failed": submission_failed,
        "late_terminal_marker_observed": _bool(lane.get("late_terminal_marker_observed")),
        "terminal_marker_observed_at": str(lane.get("terminal_marker_observed_at") or ""),
        "protocol_status": protocol_status,
    }


def _protocol_status(
    report_present: bool,
    report_non_empty: bool,
    markers: list[str],
    *,
    submission_failed: bool = False,
) -> str:
    if submission_failed:
        return "submit_failed"
    if len(markers) > 1:
        return "conflicting_terminal_markers"
    if any(marker not in TERMINAL_MARKERS for marker in markers):
        return "invalid_terminal_marker"
    if not markers:
        return "missing_terminal_marker"
    if not report_present:
        return "missing_report"
    if not report_non_empty:
        return "empty_report"
    marker = markers[0]
    if marker == ".done":
        return "complete"
    if marker == ".fail":
        return "failed"
    return "blocked"


def _select_protocol_action(lanes: list[dict[str, Any]]) -> tuple[str, str, list[str]]:
    if not lanes:
        return "block", "No required lanes were provided.", ["no required lanes provided"]

    known_gaps: list[str] = []
    has_blocked = False
    has_blocking_failure = False
    for lane in lanes:
        status = str(lane.get("protocol_status") or "")
        lane_id = str(lane.get("lane_id") or "unknown")
        if status == "complete":
            continue
        if status == "blocked":
            has_blocked = True
            known_gaps.append(f"{lane_id}: terminal marker is .blocked")
            continue
        has_blocking_failure = True
        known_gaps.append(f"{lane_id}: {status.replace('_', ' ')}")

    if has_blocking_failure:
        return "block", "At least one required lane is missing valid protocol completion evidence.", known_gaps
    if has_blocked:
        return "human_review_required", "At least one required lane reported .blocked.", known_gaps
    return "accept", "All required lanes have a non-empty report and exactly one .done marker.", []


def _planner_coverage_gate(
    planner_coverage: dict[str, Any] | None,
) -> tuple[str, str, str, list[str], dict[str, Any]]:
    if not planner_coverage:
        return "not_configured", "", "", [], {}
    if not isinstance(planner_coverage, dict):
        return "invalid", "human_review_required", "Planner coverage is malformed.", ["planner coverage is malformed"], {}

    status = str(planner_coverage.get("status") or "unknown").strip() or "unknown"
    uncovered = _string_list(planner_coverage.get("uncovered_risk_paths"))
    missing_lanes = _string_list(planner_coverage.get("missing_required_lanes"))
    gaps = [f"planner coverage uncovered risk path: {path}" for path in uncovered]
    gaps.extend(f"planner coverage missing required lane: {lane_id}" for lane_id in missing_lanes)
    payload = {
        "status": status,
        "uncovered_risk_paths": uncovered,
        "missing_required_lanes": missing_lanes,
    }

    if status == "needs_planner_revision":
        return (
            status,
            "rerun_planner",
            "Planner coverage found uncovered risk paths or missing required lanes.",
            gaps or ["planner coverage requires revision"],
            payload,
        )
    if status == "low_confidence":
        return (
            status,
            "human_review_required",
            "Planner coverage confidence is too low for automatic accept.",
            gaps or ["planner coverage confidence is low"],
            payload,
        )
    return status, "", "", gaps, payload


def _semantic_gate(
    semantic_summary: dict[str, Any] | None,
    semantic_retry_plan: dict[str, Any] | None,
) -> tuple[str, str, str, list[str], dict[str, Any]]:
    if not semantic_summary:
        return "not_configured", "", "", [], {}
    if not isinstance(semantic_summary, dict):
        return "invalid", "human_review_required", "Semantic review summary is malformed.", [
            "semantic review summary is malformed"
        ], {}
    retry_plan = semantic_retry_plan if isinstance(semantic_retry_plan, dict) else {}
    failed = _int(semantic_summary.get("failed"))
    blocked = _int(semantic_summary.get("blocked"))
    if failed <= 0 and blocked <= 0:
        return "passed", "", "", [], {"summary": dict(semantic_summary), "retry_plan": dict(retry_plan)}

    status = "failed" if failed > 0 else "blocked"
    gaps: list[str] = []
    if failed > 0:
        gaps.append(f"semantic review failed count: {failed}")
    if blocked > 0:
        gaps.append(f"semantic review blocked count: {blocked}")
    payload = {"summary": dict(semantic_summary), "retry_plan": dict(retry_plan)}
    if str(retry_plan.get("retry_scope") or "") == "lane_only":
        return (
            status,
            "rerun_lane",
            "Semantic review found lane-only issues that should be rerun.",
            gaps,
            payload,
        )
    if blocked > 0:
        return (
            status,
            "human_review_required",
            "Semantic review reported blockers without a lane-only retry plan.",
            gaps,
            payload,
        )
    return (
        status,
        "block",
        "Semantic review failed without a lane-only retry plan.",
        gaps,
        payload,
    )


def _risk_gate(risk_review: dict[str, Any] | None) -> tuple[str, str, str, list[str], dict[str, Any]]:
    if not risk_review:
        return "not_configured", "", "", [], {}
    try:
        normalized = normalize_risk_review(risk_review)
    except RiskReviewError as exc:
        return "invalid", "human_review_required", "Risk review artifact is malformed.", [str(exc)], {}

    status = str(normalized.get("status") or "unknown")
    blocking = _string_list(normalized.get("blocking_findings"))
    low_confidence = _string_list(normalized.get("low_confidence_findings"))
    gaps = [f"risk review critical finding: {finding_id}" for finding_id in blocking]
    gaps.extend(f"risk review low-confidence critical finding: {finding_id}" for finding_id in low_confidence)
    if blocking:
        return (
            status,
            "block",
            "Risk review reported high-confidence critical findings.",
            gaps,
            normalized,
        )
    if low_confidence:
        return (
            status,
            "human_review_required",
            "Risk review reported low-confidence critical findings.",
            gaps,
            normalized,
        )
    return status, "", "", gaps, normalized


def _arbiter_gate(arbiter_decision: dict[str, Any] | None) -> tuple[str, str, str, list[str], dict[str, Any]]:
    if not arbiter_decision:
        return "not_configured", "", "", [], {}
    if not isinstance(arbiter_decision, dict):
        return "invalid", "human_review_required", "Arbiter decision is malformed.", [
            "arbiter decision is malformed"
        ], {}
    status = str(arbiter_decision.get("decision") or "unknown").strip() or "unknown"
    action = str(arbiter_decision.get("recommended_leader_action") or "none").strip()
    payload = dict(arbiter_decision)
    if not bool(arbiter_decision.get("triggered")) or action == "none":
        return status, "", "", [], payload
    if action not in VALID_SELECTED_ACTIONS:
        return status, "human_review_required", "Arbiter recommended an unknown action.", [
            f"arbiter unknown recommended action: {action}"
        ], payload
    conflict_id = str(arbiter_decision.get("conflict_id") or "unknown")
    gaps = [f"arbiter conflict requires resolution: {conflict_id}"]
    return status, action, str(arbiter_decision.get("reason") or "Arbiter requires follow-up."), gaps, payload


def _validate_lane(lane: Any, *, index: int) -> None:
    if not isinstance(lane, dict):
        raise LeaderDecisionError(f"required_lanes[{index}]_must_be_object")
    for field in ("lane_id", "worker_id", "protocol_status"):
        _required_str(lane, field)
    if not isinstance(lane.get("report_present"), bool):
        raise LeaderDecisionError(f"required_lanes[{index}].report_present_must_be_boolean")
    if not isinstance(lane.get("report_non_empty"), bool):
        raise LeaderDecisionError(f"required_lanes[{index}].report_non_empty_must_be_boolean")
    markers = lane.get("terminal_markers")
    if not isinstance(markers, list):
        raise LeaderDecisionError(f"required_lanes[{index}].terminal_markers_must_be_list")
    if any(not isinstance(marker, str) for marker in markers):
        raise LeaderDecisionError(f"required_lanes[{index}].terminal_markers_must_be_strings")
    if not isinstance(lane.get("terminal_marker_count"), int):
        raise LeaderDecisionError(f"required_lanes[{index}].terminal_marker_count_must_be_integer")
    if lane.get("terminal_marker_count") != len(markers):
        raise LeaderDecisionError(f"required_lanes[{index}].terminal_marker_count_mismatch")


def _normalize_run_id(run_id: str) -> str:
    raw = _required_raw_str(run_id, "run_id")
    if raw.startswith(("visible-runs/", "langgraph-control-plane/")):
        return raw
    return "visible-runs/" + raw


def _decision_id(run_id: str, task_id: str) -> str:
    safe_run = run_id.replace("visible-runs/", "").replace("/", "-")
    safe_task = _required_raw_str(task_id, "task_id").replace("/", "-")
    return f"leader-decision-{safe_run}-{safe_task}"


def _required_str(payload: dict[str, Any], field: str) -> str:
    return _required_raw_str(payload.get(field), field)


def _required_raw_str(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise LeaderDecisionError(f"missing_{field}")
    return normalized


def _normalize_marker(value: Any) -> str:
    marker = _required_raw_str(value, "terminal_marker")
    if marker.startswith("."):
        return marker
    return "." + marker


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LeaderDecisionError("terminal_markers_must_be_list")
    return value


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
