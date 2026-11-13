"""LangGraph-based control plane over the existing visible-dispatch CLI.

This module intentionally treats LangGraph as an upper-layer state machine. It
never talks to tmux panes or OpenCode prompt endpoints directly. All live work
flows through the existing `omx_visible_dispatch` control surface.
"""

from __future__ import annotations

import json
import operator
import re
import subprocess
import uuid
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from visible_agent_orchestrator.control_plane_contract import (
    CONTRACT_VERSION,
    CommandRunner,
    VisibleDispatchContract,
    default_command_runner,
    utc_now,
)
from visible_agent_orchestrator.inner_artifact_contract import (
    DEFAULT_ARTIFACT_ROOT,
    INNER_MODE_CHOICES,
    normalize_inner_artifacts,
    render_body_with_inner_artifact_contract,
    validate_inner_artifacts,
)
from visible_agent_orchestrator.lane_contracts import analyze_lane_report_adherence, derive_lane_assignments, render_lane_body
from visible_agent_orchestrator.leader_decision import build_protocol_leader_decision
from visible_agent_orchestrator.operation_authorization import (
    OperationAuthorizationError,
    derive_operation_contract,
    external_harness_authorization_ledger,
    validate_operation_capability,
)
from visible_agent_orchestrator.prompt_budget import build_prompt_budget, prompt_budget_error
from visible_agent_orchestrator.run_metrics import (
    build_run_control_advice,
    build_run_metrics,
    evaluate_execution_budget,
)


class ControlPlaneState(dict):
    """Runtime state carrier for static typing readability."""


class GraphState(TypedDict, total=False):
    root: str
    thread_id: str
    backend: str
    contract_version: int
    workers: list[str]
    title: str
    body: str
    inner_mode: str
    artifact_root: str
    lane_contracts: str
    lane_assignments: list[dict[str, str]]
    lane_assignment_source: str
    task_contracts: dict[str, dict[str, Any]]
    task_contract_workers: list[str]
    planner_coverage: dict[str, Any]
    lane_adherence_gate: str
    lane_adherence_summary: dict[str, int]
    session_actions: dict[str, str]
    session_action_reason: str
    rotation_reason: str
    rehydration_packet_type: str
    semantic_summary: dict[str, int]
    semantic_retry_plan: dict[str, Any]
    provider_error_summary: dict[str, Any]
    risk_review: dict[str, Any]
    arbiter_decision: dict[str, Any]
    leader_decision: dict[str, Any]
    next_operator_action: str
    business_repo_path: str
    business_no_write_before: dict[str, Any]
    business_no_write_proof: dict[str, Any]
    allow_real: bool
    wait_timeout: int
    dry_run: bool
    model: str
    max_prompt_bytes: int
    nominal_context_tokens: int
    effective_context_tokens: int
    execution_budget: dict[str, Any]
    run_metrics: dict[str, Any]
    execution_budget_status: dict[str, Any]
    run_control_advice: dict[str, Any]
    require_confirmation: bool
    approved: bool
    rejected: bool
    preflight: dict[str, Any]
    submissions: Annotated[list[dict[str, Any]], operator.add]
    results: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]
    summary: dict[str, int]
    final_state: str
    finished_at: str


def _artifact_dir(root: Path, thread_id: str) -> Path:
    return root / ".omx" / "framework" / "team" / "langgraph-control-plane" / thread_id


def checkpoint_db_path(root: Path | str) -> Path:
    return Path(root).resolve() / ".omx" / "framework" / "team" / "langgraph-control-plane" / "control-plane.sqlite"


def make_json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(item) for item in value]
    if hasattr(value, "value") and hasattr(value, "id"):
        return {
            "type": value.__class__.__name__,
            "id": str(getattr(value, "id", "")),
            "value": make_json_safe(getattr(value, "value", None)),
        }
    return str(value)


def _completion_ratio(state: dict[str, Any]) -> str:
    summary = state.get("summary", {}) if isinstance(state.get("summary"), dict) else {}
    workers = state.get("workers", []) if isinstance(state.get("workers"), list) else []
    total = len(workers) or sum(int(value) for value in summary.values() if isinstance(value, int))
    completed = int(summary.get("completed", 0) or 0)
    if total <= 0:
        return "-"
    return f"{completed}/{total} completed"


def _parse_boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _validate_routed_task_contract(
    root: Path,
    task_contract: dict[str, Any],
    worker_id: str,
) -> dict[str, Any]:
    capability = task_contract.get("operation_capability")
    if not isinstance(capability, dict):
        raise OperationAuthorizationError(
            f"task_contract for {worker_id} requires operation_capability"
        )
    scope = capability.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") != "worker" or scope.get("id") != worker_id:
        raise OperationAuthorizationError(
            f"task_contract capability scope does not match worker {worker_id}"
        )
    base_contract = dict(task_contract)
    base_contract.pop("operation_capability")
    try:
        operation_contract = derive_operation_contract(
            "worker_dispatch",
            base_contract,
        )
        validate_operation_capability(
            capability,
            operation_kind="worker_dispatch",
            target_scope={"kind": "worker", "id": worker_id},
            required_mutation_gates=list(
                operation_contract["required_mutation_gates"]
            ),
            task_id=str(capability.get("task_id") or ""),
            attempt_id=str(capability.get("attempt_id") or ""),
        )
        ledger = external_harness_authorization_ledger(
            root,
            str(capability.get("task_id") or ""),
            str(capability.get("attempt_id") or ""),
        )
        active = ledger.active_authorization(
            str(capability.get("authorization_id") or "")
        )
        if active != capability:
            raise OperationAuthorizationError(
                "operation capability is not active in its ledger"
            )
    except OperationAuthorizationError as exc:
        raise OperationAuthorizationError(
            f"task_contract for {worker_id} is invalid: {exc}"
        ) from exc
    return task_contract


def _load_routed_task_contracts(
    path_value: str,
    workers: list[str],
    root: Path,
) -> dict[str, dict[str, Any]]:
    if not str(path_value or "").strip():
        return {}
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    assignments = payload.get("assignments") if isinstance(payload, dict) else None
    if not isinstance(assignments, list):
        return {}
    routed = [item for item in assignments if isinstance(item, dict) and "task_contract" in item]
    if not routed:
        return {}
    selected = set(workers)
    contracts: dict[str, dict[str, Any]] = {}
    for item in routed:
        worker_id = str(item.get("worker_id") or "").strip()
        if worker_id not in selected:
            raise OperationAuthorizationError(
                "task_contract workers must exactly match selected workers"
            )
        if worker_id in contracts:
            raise OperationAuthorizationError(
                f"duplicate task_contract input for worker {worker_id}"
            )
        task_contract = item.get("task_contract")
        if not isinstance(task_contract, dict):
            raise OperationAuthorizationError(
                f"task_contract for {worker_id} must be an object"
            )
        contracts[worker_id] = dict(task_contract)
    missing = sorted(selected - set(contracts))
    if missing:
        raise OperationAuthorizationError(
            f"missing task_contract for selected worker {missing[0]}"
        )
    for worker_id, task_contract in contracts.items():
        _validate_routed_task_contract(root, task_contract, worker_id)
    return contracts


def _task_contract_from_lane_assignment(
    assignment: dict[str, Any],
    prepared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    allowed_changed_files = [
        str(item).strip()
        for item in assignment.get("allowed_changed_files", [])
        if str(item).strip()
    ]
    target_repo = str(assignment.get("target_repo_root") or "").strip()
    lane_contract: dict[str, Any] = {}
    if allowed_changed_files and target_repo:
        lane_contract = {
            "required_report_headings": ["Files", "Commit"],
            "allowed_changed_files": allowed_changed_files,
            "contract_target_repo": target_repo,
        }
    if prepared is None:
        return lane_contract
    for key, value in lane_contract.items():
        if prepared.get(key) != value:
            raise OperationAuthorizationError(
                f"prepared task_contract conflicts with lane field {key}"
            )
    return prepared


def _semantic_review_from_execution(execution: dict[str, Any]) -> dict[str, Any] | None:
    actions = execution.get("actions", []) if isinstance(execution.get("actions"), list) else []
    collect_action = next(
        (
            item
            for item in actions
            if isinstance(item, dict) and item.get("contract_action") == "collect"
        ),
        {},
    )
    parsed = collect_action.get("parsed", {}) if isinstance(collect_action, dict) else {}
    if not isinstance(parsed, dict):
        return None
    contract_status = str(parsed.get("report_contract_status") or "").strip().lower()
    if contract_status == "failed":
        return {
            "status": "failed",
            "reason": str(parsed.get("report_contract_reason") or "report_contract_failed"),
            "retry_scope": str(parsed.get("report_contract_retry_scope") or "lane_only"),
        }
    status = str(parsed.get("semantic_status") or "").strip().lower()
    if not status:
        return None
    review: dict[str, Any] = {"status": status}
    role = str(parsed.get("worker_role") or parsed.get("role") or "").strip()
    reason = str(parsed.get("semantic_reason") or "").strip()
    retry_scope = str(parsed.get("retry_scope") or "").strip()
    rerun_summarizer = _parse_boolish(parsed.get("rerun_summarizer"))
    if role:
        review["role"] = role
    if reason:
        review["reason"] = reason
    if retry_scope:
        review["retry_scope"] = retry_scope
    if rerun_summarizer is not None:
        review["rerun_summarizer"] = rerun_summarizer
    return review


def _summarize_semantic_reviews(results: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, Any]]:
    summary: dict[str, int] = {}
    retry_workers: list[str] = []
    retry_scope = ""
    rerun_summarizer = False
    for item in results:
        review = item.get("semantic_review", {}) if isinstance(item.get("semantic_review"), dict) else {}
        status = str(review.get("status") or "").strip().lower()
        if status not in {"failed", "blocked"}:
            continue
        summary[status] = summary.get(status, 0) + 1
        if str(review.get("retry_scope") or "") == "lane_only":
            retry_scope = "lane_only"
            worker_id = str(item.get("worker_id") or "").strip()
            if worker_id:
                retry_workers.append(worker_id)
        if bool(review.get("rerun_summarizer")):
            rerun_summarizer = True
    retry_plan: dict[str, Any] = {}
    if retry_scope:
        retry_plan["retry_scope"] = retry_scope
    if retry_workers:
        retry_plan["workers"] = sorted(dict.fromkeys(retry_workers))
    if rerun_summarizer:
        retry_plan["rerun_summarizer"] = True
    return summary, retry_plan


def _summarize_provider_errors(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, dict[str, Any]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        provider_error = item.get("provider_error", {})
        if not isinstance(provider_error, dict) or not provider_error.get("detected"):
            continue
        kind = str(provider_error.get("kind") or "").strip()
        if not kind:
            continue
        bucket = summary.setdefault(
            kind,
            {"retryable": bool(provider_error.get("retryable")), "workers": [], "count": 0},
        )
        bucket["count"] = int(bucket.get("count") or 0) + 1
        if bool(provider_error.get("retryable")):
            bucket["retryable"] = True
        worker_id = str(item.get("worker_id") or "").strip()
        if worker_id and worker_id not in bucket["workers"]:
            bucket["workers"].append(worker_id)
    return summary


def _all_results_completed(summary: dict[str, int]) -> bool:
    return bool(summary) and all(key == "completed" for key in summary)


def _retryable_provider_error_notes(results: list[dict[str, Any]]) -> set[str]:
    notes: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        provider_error = item.get("provider_error", {})
        if not isinstance(provider_error, dict):
            continue
        if not provider_error.get("detected") or not provider_error.get("retryable"):
            continue
        worker_id = str(item.get("worker_id") or "").strip()
        kind = str(provider_error.get("kind") or "").strip()
        if worker_id and kind:
            notes.add(f"{worker_id} provider_retryable_error={kind}")
    return notes


def _only_retryable_provider_errors(errors: list[str], results: list[dict[str, Any]]) -> bool:
    if not errors:
        return False
    provider_notes = _retryable_provider_error_notes(results)
    return bool(provider_notes) and all(str(error) in provider_notes for error in errors)


def _leader_decision_lanes_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lanes: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        worker_id = str(item.get("worker_id") or "").strip()
        if not worker_id:
            continue
        lane_assignment = item.get("lane_assignment", {}) if isinstance(item.get("lane_assignment"), dict) else {}
        lane_id = str(lane_assignment.get("lane_id") or worker_id).strip()
        state = str(item.get("state") or item.get("collect_status") or "").strip()
        report_excerpt = str(item.get("report_excerpt") or "").strip()
        supplied_markers = [
            str(marker).strip()
            for marker in item.get("terminal_markers", [])
            if str(marker).strip()
        ] if isinstance(item.get("terminal_markers"), list) else []
        markers = supplied_markers or _terminal_markers_for_state(state)
        report_present = (
            bool(item.get("report_present"))
            if "report_present" in item
            else bool(report_excerpt)
        )
        report_non_empty = (
            bool(item.get("report_non_empty"))
            if "report_non_empty" in item
            else bool(report_excerpt)
        )
        lanes.append(
            {
                "lane_id": lane_id,
                "worker_id": worker_id,
                "report_present": report_present,
                "report_non_empty": report_non_empty,
                "terminal_markers": markers,
                "submission_failed": bool(item.get("submission_failed")) or state == "submit_failed",
                "late_terminal_marker_observed": bool(item.get("late_terminal_marker_observed")),
                "terminal_marker_observed_at": str(item.get("terminal_marker_observed_at") or ""),
            }
        )
    return lanes


def _leader_decision_lanes_from_unexecuted_plan(state: dict[str, Any]) -> list[dict[str, Any]]:
    planner_coverage = state.get("planner_coverage", {}) if isinstance(state.get("planner_coverage"), dict) else {}
    required_lane_ids = [
        str(item).strip()
        for item in planner_coverage.get("required_lanes", [])
        if str(item).strip()
    ] if isinstance(planner_coverage.get("required_lanes"), list) else []
    if not required_lane_ids:
        return []
    assignments = state.get("lane_assignments", []) if isinstance(state.get("lane_assignments"), list) else []
    assignment_by_lane = {
        str(item.get("lane_id") or ""): item
        for item in assignments
        if isinstance(item, dict) and str(item.get("lane_id") or "")
    }
    lanes: list[dict[str, Any]] = []
    for lane_id in required_lane_ids:
        assignment = assignment_by_lane.get(lane_id, {})
        worker_id = str(assignment.get("worker_id") or lane_id).strip()
        lanes.append(
            {
                "lane_id": lane_id,
                "worker_id": worker_id,
                "report_present": False,
                "report_non_empty": False,
                "terminal_markers": [],
            }
        )
    return lanes


def _preflight_failed_before_submission(state: dict[str, Any]) -> bool:
    errors = state.get("errors", []) if isinstance(state.get("errors"), list) else []
    submissions = state.get("submissions", []) if isinstance(state.get("submissions"), list) else []
    results = state.get("results", []) if isinstance(state.get("results"), list) else []
    return bool(errors) and not submissions and not results


def _project_preflight_failure_into_leader_decision(
    decision: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    errors = [
        str(item).strip()
        for item in (state.get("errors", []) if isinstance(state.get("errors"), list) else [])
        if str(item).strip()
    ]
    if not errors:
        return decision
    updated = dict(decision)
    updated["selected_action"] = "block"
    updated["reason"] = "Worker preflight failed before lane execution."
    existing_gaps = [
        str(item)
        for item in updated.get("known_gaps", [])
        if str(item) and str(item) != "no required lanes provided"
    ]
    updated["known_gaps"] = list(dict.fromkeys(existing_gaps + errors))
    updated["preflight_failure"] = {
        "stage": "before_worker_submission",
        "errors": errors,
    }
    return updated


def _terminal_markers_for_state(state: str) -> list[str]:
    normalized = str(state or "").strip().lower()
    if normalized == "completed":
        return [".done"]
    if normalized == "failed":
        return [".fail"]
    if normalized == "blocked":
        return [".blocked"]
    if normalized == "invalid_conflicting_terminal_markers":
        return [".done", ".fail"]
    if normalized == "invalid_terminal_no_report":
        return [".done"]
    return []


def _operator_verdict(state: dict[str, Any]) -> str:
    final_state = str(state.get("final_state") or "")
    errors = state.get("errors", []) if isinstance(state.get("errors"), list) else []
    warnings = state.get("warnings", []) if isinstance(state.get("warnings"), list) else []
    if final_state == "completed" and not errors and not warnings:
        return "clean_success"
    if final_state == "completed" and warnings:
        return "success_with_followup"
    if final_state == "completed_with_retryable_provider_error":
        return "success_with_provider_retryable_followup"
    if final_state == "blocked":
        return "blocked_attention_needed"
    if final_state == "partial":
        return "partial_attention_needed"
    if final_state == "failed":
        return "failed"
    if final_state == "dry_run":
        return "dry_run"
    return final_state or "unknown"


def _next_operator_action(state: dict[str, Any]) -> str:
    final_state = str(state.get("final_state") or "")
    semantic_summary = state.get("semantic_summary", {}) if isinstance(state.get("semantic_summary"), dict) else {}
    retry_plan = state.get("semantic_retry_plan", {}) if isinstance(state.get("semantic_retry_plan"), dict) else {}
    retry_scope = str(retry_plan.get("retry_scope") or "")
    rerun_summarizer = bool(retry_plan.get("rerun_summarizer"))
    if int(semantic_summary.get("failed", 0) or 0) > 0 and retry_scope == "lane_only":
        return "rerun_failed_lane_then_rerun_summarizer" if rerun_summarizer else "rerun_failed_lane"
    if int(semantic_summary.get("blocked", 0) or 0) > 0 and retry_scope == "lane_only":
        return "rerun_blocked_lane_then_rerun_summarizer" if rerun_summarizer else "rerun_blocked_lane"
    if str(state.get("lane_adherence_gate") or "") == "failed":
        return "review_lane_adherence"
    errors = state.get("errors", []) if isinstance(state.get("errors"), list) else []
    warnings = state.get("warnings", []) if isinstance(state.get("warnings"), list) else []
    if final_state == "completed_with_retryable_provider_error":
        return "inspect_provider_retryable"
    if errors:
        return "inspect_errors"
    if warnings:
        return "review_warnings"
    if final_state == "blocked":
        return "inspect_blockers"
    if final_state in {"completed", "dry_run", "rejected"}:
        return "none"
    return "inspect_state"


def write_artifacts(root: Path, thread_id: str, state: dict[str, Any]) -> dict[str, str]:
    out_dir = _artifact_dir(root, thread_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "state.json"
    summary_path = out_dir / "summary.md"
    leader_decision_path = out_dir / "leader_decision.json"
    run_metrics_path = out_dir / "run_metrics.json"
    safe_state = make_json_safe(state)
    state_path.write_text(json.dumps(safe_state, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    run_metrics = safe_state.get("run_metrics")
    if isinstance(run_metrics, dict):
        run_metrics_path.write_text(
            json.dumps(
                {
                    "run_metrics": run_metrics,
                    "execution_budget": safe_state.get("execution_budget", {}),
                    "execution_budget_status": safe_state.get("execution_budget_status", {}),
                    "run_control_advice": safe_state.get("run_control_advice", {}),
                },
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    linked_visible_runs = sorted(
        {
            str(item.get("run_id") or "")
            for item in safe_state.get("results", [])
            if isinstance(item, dict) and str(item.get("run_id") or "")
        }
    )
    closeout_notes = sorted(
        {
            str(note).strip()
            for item in safe_state.get("results", [])
            if isinstance(item, dict)
            for note in (item.get("closeout_notes", []) if isinstance(item.get("closeout_notes"), list) else [])
            if str(note).strip()
        }
    )
    summary_lines = [
        f"# LangGraph Control Plane Run `{thread_id}`",
        "",
        f"- generated_at: {utc_now()}",
        f"- final_state: {safe_state.get('final_state') or '-'}",
        f"- operator_verdict: {_operator_verdict(safe_state)}",
        f"- completion_ratio: {_completion_ratio(safe_state)}",
        f"- next_operator_action: {_next_operator_action(safe_state)}",
        f"- contract_version: {safe_state.get('contract_version') or CONTRACT_VERSION}",
        f"- root: {safe_state.get('root') or '-'}",
        f"- backend: {safe_state.get('backend') or '-'}",
        f"- workers: {', '.join(safe_state.get('workers', [])) or '-'}",
        f"- inner_mode: {safe_state.get('inner_mode') or '-'}",
        f"- artifact_root: {safe_state.get('artifact_root') or '-'}",
        f"- lane_assignment_source: {safe_state.get('lane_assignment_source') or '-'}",
        f"- lane_contracts: {safe_state.get('lane_contracts') or '-'}",
        f"- planner_coverage_status: {((safe_state.get('planner_coverage') or {}).get('status') if isinstance(safe_state.get('planner_coverage'), dict) else '-') or '-'}",
        f"- lane_adherence_gate: {safe_state.get('lane_adherence_gate') or '-'}",
        f"- lane_adherence_summary: {json.dumps(safe_state.get('lane_adherence_summary', {}), ensure_ascii=False)}",
        f"- semantic_summary: {json.dumps(safe_state.get('semantic_summary', {}), ensure_ascii=False)}",
        f"- semantic_retry_plan: {json.dumps(safe_state.get('semantic_retry_plan', {}), ensure_ascii=False)}",
        f"- provider_error_summary: {json.dumps(safe_state.get('provider_error_summary', {}), ensure_ascii=False)}",
        f"- leader_decision_selected_action: {((safe_state.get('leader_decision') or {}).get('selected_action') if isinstance(safe_state.get('leader_decision'), dict) else '-') or '-'}",
        f"- execution_budget_status: {((safe_state.get('execution_budget_status') or {}).get('status') if isinstance(safe_state.get('execution_budget_status'), dict) else '-') or '-'}",
        f"- run_metrics_missing: {', '.join(safe_state.get('run_metrics', {}).get('missing_metrics', [])) if isinstance(safe_state.get('run_metrics'), dict) else '-'}",
        f"- visible_run_ids: {', '.join(linked_visible_runs) or '-'}",
        f"- summary: {json.dumps(safe_state.get('summary', {}), ensure_ascii=False)}",
    ]
    leader_decision = safe_state.get("leader_decision", {}) if isinstance(safe_state.get("leader_decision"), dict) else {}
    if leader_decision:
        summary_lines.extend(
            [
                "",
                "## Leader Decision",
                f"- selected_action: {leader_decision.get('selected_action') or '-'}",
                f"- advisory_only: {str(bool(leader_decision.get('advisory_only'))).lower()}",
                f"- decision_id: {leader_decision.get('decision_id') or '-'}",
                f"- reason: {leader_decision.get('reason') or '-'}",
            ]
        )
    results = safe_state.get("results", []) if isinstance(safe_state.get("results"), list) else []
    if results:
        summary_lines.extend(["", "## Worker Results"])
        for item in results:
            if not isinstance(item, dict):
                continue
            summary_lines.append(
                "- "
                f"{item.get('worker_id') or '-'} "
                f"state={item.get('state') or '-'} "
                f"run_id={item.get('run_id') or '-'} "
                f"artifact_state={((item.get('artifact_validation') or {}).get('state') if isinstance(item.get('artifact_validation'), dict) else '-') or '-'} "
                f"lane_adherence={((item.get('lane_adherence') or {}).get('status') if isinstance(item.get('lane_adherence'), dict) else '-') or '-'} "
                f"semantic_state={((item.get('semantic_review') or {}).get('status') if isinstance(item.get('semantic_review'), dict) else '-') or '-'}"
            )
            excerpt = str(item.get("report_excerpt") or "").strip()
            if excerpt:
                first = excerpt.splitlines()[0].strip()
                if first:
                    summary_lines.append(f"  excerpt: {first[:220]}")
            semantic_review = item.get("semantic_review", {}) if isinstance(item.get("semantic_review"), dict) else {}
            if semantic_review:
                reason = str(semantic_review.get("reason") or "").strip()
                retry_scope = str(semantic_review.get("retry_scope") or "").strip()
                rerun_summarizer = bool(semantic_review.get("rerun_summarizer"))
                detail = [
                    f"status={semantic_review.get('status') or '-'}",
                    f"reason={reason or '-'}",
                    f"retry_scope={retry_scope or '-'}",
                    f"rerun_summarizer={str(rerun_summarizer).lower()}",
                ]
                summary_lines.append(f"  semantic_review: {' '.join(detail)}")
    proof = safe_state.get("business_no_write_proof", {})
    if isinstance(proof, dict) and proof:
        before = proof.get("before") or {}
        after = proof.get("after") or {}
        proof_ok = bool(proof.get("ok"))
        summary_lines.extend(
            [
                "",
                "## Business No-Write Proof",
                f"- target_repo: {proof.get('target_repo') or '-'}",
                f"- proof_ok: {str(proof_ok).lower()}",
                f"- status_short_unchanged: {str(bool(proof.get('status_short_unchanged'))).lower()}",
                f"- cached_name_only_unchanged: {str(bool(proof.get('cached_name_only_unchanged'))).lower()}",
                f"- worktree_name_only_unchanged: {str(bool(proof.get('worktree_name_only_unchanged'))).lower()}",
                f"- before_status_short_count: {len((before or {}).get('status_short', []))}",
                f"- before_cached_name_only_count: {len((before or {}).get('cached_name_only', []))}",
                f"- before_worktree_name_only_count: {len((before or {}).get('worktree_name_only', []))}",
            ]
        )
        if proof_ok:
            summary_lines.append("- details_in_state_json: true")
        else:
            summary_lines.extend(
                [
                    f"- before_status_short: {json.dumps((before or {}).get('status_short', []), ensure_ascii=False)}",
                    f"- after_status_short: {json.dumps((after or {}).get('status_short', []), ensure_ascii=False)}",
                    f"- before_cached_name_only: {json.dumps((before or {}).get('cached_name_only', []), ensure_ascii=False)}",
                    f"- after_cached_name_only: {json.dumps((after or {}).get('cached_name_only', []), ensure_ascii=False)}",
                    f"- before_worktree_name_only: {json.dumps((before or {}).get('worktree_name_only', []), ensure_ascii=False)}",
                    f"- after_worktree_name_only: {json.dumps((after or {}).get('worktree_name_only', []), ensure_ascii=False)}",
                ]
            )
    if safe_state.get("errors"):
        summary_lines.extend(["", "## Errors"])
        summary_lines.extend([f"- {item}" for item in safe_state.get("errors", [])])
    if safe_state.get("warnings"):
        summary_lines.extend(["", "## Warnings"])
        summary_lines.extend([f"- {item}" for item in safe_state.get("warnings", [])])
    if closeout_notes:
        summary_lines.extend(["", "## Closeout Notes"])
        summary_lines.extend([f"- {item}" for item in closeout_notes])
    summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    artifacts = {"state_json": str(state_path), "summary_md": str(summary_path)}
    if isinstance(run_metrics, dict):
        artifacts["run_metrics_json"] = str(run_metrics_path)
    if leader_decision:
        leader_decision_path.write_text(
            json.dumps(leader_decision, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        artifacts["leader_decision_json"] = str(leader_decision_path)
    return artifacts


def snapshot_to_dict(snapshot: Any) -> dict[str, Any]:
    if snapshot is None:
        return {}
    return make_json_safe(
        {
            "values": getattr(snapshot, "values", {}),
            "next": list(getattr(snapshot, "next", ()) or ()),
            "config": getattr(snapshot, "config", {}),
            "metadata": getattr(snapshot, "metadata", {}),
            "created_at": getattr(snapshot, "created_at", ""),
            "parent_config": getattr(snapshot, "parent_config", {}),
            "interrupts": list(getattr(snapshot, "interrupts", ()) or ()),
        }
    )


def render_worker_template(template: str, worker_id: str) -> str:
    return str(template or "").replace("{worker_id}", worker_id)


def _summarize_lane_adherence(results: list[dict[str, Any]]) -> tuple[dict[str, int], str]:
    summary: dict[str, int] = {}
    gate = "pass"
    for item in results:
        if not isinstance(item, dict):
            continue
        adherence = item.get("lane_adherence", {}) if isinstance(item.get("lane_adherence"), dict) else {}
        status = str(adherence.get("status") or "")
        if not status:
            continue
        summary[status] = summary.get(status, 0) + 1
        if status == "cross_lane_bleed_suspected":
            gate = "failed"
        elif status == "handoff_only_spill" and gate != "failed":
            gate = "warning"
    return summary, gate


def _load_submission_report_text(root: Path, submission: dict[str, Any], collect_action: dict[str, Any]) -> str:
    created_task = str(submission.get("created_task") or "").strip()
    if created_task:
        task_dir = Path(created_task)
        if not task_dir.is_absolute():
            task_dir = (root / created_task).resolve()
        report_path = task_dir / "report.md"
        try:
            return report_path.read_text(encoding="utf-8")
        except Exception:
            pass
    return str(collect_action.get("stdout") or "")


ABSOLUTE_PATH_PATTERN = re.compile(r"(/[^\s\"'`]+)")
READONLY_PROOF_HINTS = (
    "read-only",
    "readonly",
    "只读",
    "no file edits",
    "no business repo writes",
    "no git index changes",
    "不改文件",
)


def _readonly_no_write_context(title: str, body: str) -> bool:
    haystack = f"{title}\n{body}".lower()
    return any(hint in haystack for hint in READONLY_PROOF_HINTS)


def _resolve_git_repo(candidate: str) -> Path | None:
    raw = str(candidate or "").strip().rstrip(".,:;)]}`\"'")
    if not raw:
        return None
    path = Path(raw).expanduser().resolve()
    current = path if path.is_dir() else path.parent
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _detect_business_repo_path(state: dict[str, Any]) -> str:
    explicit = _resolve_git_repo(str(state.get("business_repo_path") or ""))
    if explicit is not None:
        return str(explicit)
    title = str(state.get("title") or "")
    body = str(state.get("body") or "")
    if not _readonly_no_write_context(title, body):
        return ""
    combined = f"{title}\n{body}"
    for match in ABSOLUTE_PATH_PATTERN.finditer(combined):
        resolved = _resolve_git_repo(match.group(1))
        if resolved is not None:
            return str(resolved)
    return ""


def _run_git_lines(repo: Path, *args: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "lines": []}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip() or f"git {' '.join(args)} failed", "lines": []}
    return {"ok": True, "error": "", "lines": [line.rstrip("\n") for line in proc.stdout.splitlines()]}


def _business_no_write_snapshot(repo_path: str) -> dict[str, Any]:
    repo = Path(repo_path).resolve()
    status = _run_git_lines(repo, "status", "--short")
    cached = _run_git_lines(repo, "diff", "--cached", "--name-only")
    worktree = _run_git_lines(repo, "diff", "--name-only")
    errors = [item.get("error", "") for item in (status, cached, worktree) if not item.get("ok")]
    return {
        "repo": str(repo),
        "ok": not errors,
        "errors": [item for item in errors if item],
        "status_short": status.get("lines", []),
        "cached_name_only": cached.get("lines", []),
        "worktree_name_only": worktree.get("lines", []),
    }


def _capture_business_no_write_before(state: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    repo_path = _detect_business_repo_path(state)
    if not repo_path:
        return {}, []
    before = _business_no_write_snapshot(repo_path)
    update = {"business_repo_path": repo_path, "business_no_write_before": before}
    if before.get("ok"):
        return update, []
    return update, [f"business no-write pre-snapshot unavailable: {', '.join(before.get('errors', []))}"]


def _finalize_business_no_write(state: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    repo_path = str(state.get("business_repo_path") or "")
    before = state.get("business_no_write_before", {})
    if not repo_path or not isinstance(before, dict) or not before:
        return {}, [], []
    after = _business_no_write_snapshot(repo_path)
    status_unchanged = list(before.get("status_short", [])) == list(after.get("status_short", []))
    cached_unchanged = list(before.get("cached_name_only", [])) == list(after.get("cached_name_only", []))
    worktree_unchanged = list(before.get("worktree_name_only", [])) == list(after.get("worktree_name_only", []))
    proof = {
        "target_repo": repo_path,
        "ok": bool(before.get("ok")) and bool(after.get("ok")) and status_unchanged and cached_unchanged and worktree_unchanged,
        "status_short_unchanged": status_unchanged,
        "cached_name_only_unchanged": cached_unchanged,
        "worktree_name_only_unchanged": worktree_unchanged,
        "before": before,
        "after": after,
    }
    warnings: list[str] = []
    errors: list[str] = []
    if not before.get("ok") or not after.get("ok"):
        warnings.append("business no-write proof incomplete")
    else:
        if not status_unchanged:
            errors.append("business repo status changed during read-only run")
        if not cached_unchanged:
            errors.append("business repo cached index changed during read-only run")
        if not worktree_unchanged:
            errors.append("business repo worktree diff changed during read-only run")
    return proof, errors, warnings


def build_langgraph_control_plane(runner: CommandRunner = default_command_runner, *, checkpointer: Any | None = None):
    def contract_for(state: dict[str, Any]) -> VisibleDispatchContract:
        return VisibleDispatchContract(
            root=Path(state["root"]).resolve(),
            backend=str(state.get("backend") or "tmux-reference"),
            runner=runner,
            max_prompt_bytes=state.get("max_prompt_bytes"),
            model=str(state.get("model") or ""),
            nominal_context_tokens=state.get("nominal_context_tokens"),
            effective_context_tokens=state.get("effective_context_tokens"),
        )

    def preflight(state: dict[str, Any]) -> dict[str, Any]:
        workers = [str(item) for item in state.get("workers", [])]
        contract = contract_for(state)
        preflight_result = contract.preflight(workers)
        business_update, business_warnings = _capture_business_no_write_before(state)
        return {
            "contract_version": int(state.get("contract_version") or CONTRACT_VERSION),
            "preflight": preflight_result,
            **business_update,
            "errors": list(dict.fromkeys(preflight_result.get("errors", []))),
            "warnings": list(
                dict.fromkeys(
                    list(state.get("warnings", []))
                    + list(preflight_result.get("warnings", []))
                    + list(business_warnings)
                )
            ),
        }

    def approval_gate(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("errors"):
            return {}
        if not bool(state.get("require_confirmation")):
            return {"approved": True}
        if bool(state.get("approved")):
            return {"approved": True}
        decision = interrupt(
            {
                "phase": "approval_gate",
                "workers": state.get("workers", []),
                "backend": state.get("backend"),
                "contract_version": state.get("contract_version") or CONTRACT_VERSION,
                "preflight_summary": [
                    {
                        "worker_id": item.get("worker_id") or "global",
                        "action": item.get("contract_action"),
                        "returncode": item.get("returncode"),
                        "ok": item.get("ok"),
                    }
                    for item in state.get("preflight", {}).get("checks", [])
                ],
            }
        )
        approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
        return {"approved": approved, "rejected": not approved}

    def fanout_submit(state: dict[str, Any]):
        if state.get("errors"):
            return "finalize"
        if bool(state.get("require_confirmation")) and not bool(state.get("approved")):
            return "finalize"
        workers = [str(item) for item in state.get("workers", [])]
        if not workers:
            return "finalize"
        sends = []
        title_template = str(state.get("title") or "")
        body_template = str(state.get("body") or "")
        inner_mode = str(state.get("inner_mode") or "")
        artifact_root = str(state.get("artifact_root") or DEFAULT_ARTIFACT_ROOT)
        explicit_assignments = state.get("lane_assignments", []) if isinstance(state.get("lane_assignments"), list) else []
        resolved_assignments = explicit_assignments or derive_lane_assignments(
            workers,
            title=title_template,
            body=body_template,
            inner_mode=inner_mode,
        )
        lane_assignments = {
            str(item.get("worker_id") or ""): item
            for item in resolved_assignments
        }
        task_contracts = (
            state.get("task_contracts", {})
            if isinstance(state.get("task_contracts"), dict)
            else {}
        )
        for worker_id in workers:
            rendered_body = render_worker_template(body_template, worker_id)
            if inner_mode:
                rendered_body = render_body_with_inner_artifact_contract(rendered_body, inner_mode, artifact_root)
            elif worker_id in lane_assignments:
                rendered_body = render_lane_body(rendered_body, lane_assignments[worker_id])
            sends.append(
                Send(
                    "submit_worker",
                    {
                        "root": state["root"],
                        "backend": state.get("backend", "tmux-reference"),
                        "worker_id": worker_id,
                        "title": render_worker_template(title_template, worker_id),
                        "body": rendered_body,
                        "lane_assignment": lane_assignments.get(worker_id, {}),
                        "task_contract": task_contracts.get(worker_id),
                        "inner_mode": inner_mode,
                        "artifact_root": artifact_root,
                        "allow_real": bool(state.get("allow_real")),
                        "wait_timeout": int(state.get("wait_timeout", 300) or 300),
                        "dry_run": bool(state.get("dry_run")),
                        "contract_version": int(state.get("contract_version") or CONTRACT_VERSION),
                        "model": state.get("model"),
                        "max_prompt_bytes": state.get("max_prompt_bytes"),
                        "nominal_context_tokens": state.get("nominal_context_tokens"),
                        "effective_context_tokens": state.get("effective_context_tokens"),
                        "session_action": (
                            state.get("session_actions", {}).get(worker_id, "")
                            if isinstance(state.get("session_actions"), dict)
                            else ""
                        ),
                        "session_action_reason": state.get(
                            "session_action_reason", ""
                        ),
                        "rotation_reason": state.get("rotation_reason", ""),
                        "rehydration_packet_type": state.get(
                            "rehydration_packet_type", ""
                        ),
                    },
                )
            )
        return sends

    def submit_worker(state: dict[str, Any]) -> dict[str, Any]:
        worker_id = str(state["worker_id"])
        title = str(state.get("title") or "")
        body = str(state.get("body") or "")
        lane_assignment = state.get("lane_assignment", {}) if isinstance(state.get("lane_assignment"), dict) else {}
        prepared_task_contract = (
            state.get("task_contract")
            if isinstance(state.get("task_contract"), dict)
            else None
        )
        task_contract = _task_contract_from_lane_assignment(
            lane_assignment,
            prepared_task_contract,
        )
        if task_contract.get("operation_capability") is not None:
            _validate_routed_task_contract(
                Path(state["root"]).resolve(),
                task_contract,
                worker_id,
            )
        budget = build_prompt_budget(
            title=title,
            body=body,
            model=str(state.get("model") or ""),
            worker_id=worker_id,
            lane_id=str(lane_assignment.get("lane_id") or ""),
            max_prompt_bytes=state.get("max_prompt_bytes"),
            nominal_context_tokens=state.get("nominal_context_tokens"),
            effective_context_tokens=state.get("effective_context_tokens"),
        )
        if budget["status"] != "ok":
            entry = {
                "contract_version": int(state.get("contract_version") or CONTRACT_VERSION),
                "contract_action": "submit",
                "worker_id": worker_id,
                "dry_run": bool(state.get("dry_run")),
                "backend": str(state.get("backend") or "tmux-reference"),
                "title": title,
                "body": body,
                "lane_assignment": lane_assignment,
                "returncode": 1,
                "ok": False,
                "state": "prompt_budget_rejected",
                "warnings": [],
                "errors": [prompt_budget_error(worker_id, budget)],
                "run_id": "",
                "task_id": "",
                "created_task": "",
                "prompt_budget": budget,
                "closeout_notes": ["prompt_budget_rejected_before_submit"],
            }
            return {"submissions": [entry], "errors": list(entry["errors"]), "warnings": []}
        if bool(state.get("dry_run")):
            return {
                "submissions": [
                    {
                        "contract_version": int(state.get("contract_version") or CONTRACT_VERSION),
                        "contract_action": "submit",
                        "worker_id": worker_id,
                        "dry_run": True,
                        "backend": str(state.get("backend") or "tmux-reference"),
                        "title": title,
                        "body": body,
                        "lane_assignment": lane_assignment,
                        "task_contract": task_contract or None,
                        "session_action": str(
                            state.get("session_action") or ""
                        ),
                        "session_action_reason": str(
                            state.get("session_action_reason") or ""
                        ),
                        "returncode": 0,
                        "warnings": [],
                        "errors": [],
                    }
                ]
            }
        contract = contract_for(state)
        entry = contract.submit(
            worker_id,
            title,
            body,
            allow_real=bool(state.get("allow_real")),
            task_contract=task_contract or None,
            session_action=str(state.get("session_action") or ""),
            session_action_reason=str(
                state.get("session_action_reason") or ""
            ),
            rotation_reason=str(state.get("rotation_reason") or ""),
            rehydration_packet_type=str(
                state.get("rehydration_packet_type") or ""
            ),
        )
        if lane_assignment:
            entry["lane_assignment"] = lane_assignment
        return {"submissions": [entry], "errors": list(entry.get("errors", [])), "warnings": list(entry.get("warnings", []))}

    def wait_and_collect(state: dict[str, Any]) -> dict[str, Any]:
        wait_timeout = int(state.get("wait_timeout", 300) or 300)
        contract = contract_for(state)
        results: list[dict[str, Any]] = []
        errors = list(state.get("errors", []))
        warnings = list(state.get("warnings", []))
        for submission in state.get("submissions", []):
            execution = contract.execute_submission(submission, wait_timeout=wait_timeout)
            inner_mode = str(submission.get("inner_mode") or state.get("inner_mode") or "")
            artifact_root = str(submission.get("artifact_root") or state.get("artifact_root") or DEFAULT_ARTIFACT_ROOT)
            lane_assignment = submission.get("lane_assignment", {}) if isinstance(submission.get("lane_assignment"), dict) else {}
            if inner_mode and execution.get("state") == "completed":
                normalization = normalize_inner_artifacts(contract.root, str(execution.get("run_id") or ""), str(execution.get("task_id") or ""), inner_mode, artifact_root)
                validation = validate_inner_artifacts(contract.root, str(execution.get("run_id") or ""), str(execution.get("task_id") or ""), inner_mode, artifact_root)
                execution["artifact_validation"] = validation
                if normalization.get("warning"):
                    execution["warnings"] = list(dict.fromkeys(list(execution.get("warnings", [])) + [str(normalization["warning"])]))
                if not validation.get("ok"):
                    execution["state"] = str(validation.get("state") or "artifact_missing")
                    execution["errors"] = list(dict.fromkeys(list(execution.get("errors", [])) + list(validation.get("errors", []))))
                if validation.get("warnings"):
                    execution["warnings"] = list(dict.fromkeys(list(execution.get("warnings", [])) + list(validation.get("warnings", []))))
            if lane_assignment:
                collect_action = next(
                    (item for item in execution.get("actions", []) if isinstance(item, dict) and item.get("contract_action") == "collect"),
                    {},
                )
                lane_adherence = analyze_lane_report_adherence(
                    _load_submission_report_text(contract.root, submission, collect_action if isinstance(collect_action, dict) else {}),
                    lane_assignment,
                )
                execution["lane_adherence"] = lane_adherence
                if not lane_adherence.get("ok", True):
                    matched = ", ".join(str(item) for item in lane_adherence.get("matched_terms", []))
                    execution["warnings"] = list(
                        dict.fromkeys(list(execution.get("warnings", [])) + [f"{execution.get('worker_id')} lane_adherence_cross_lane_bleed"])
                    )
                    execution["closeout_notes"] = list(
                        dict.fromkeys(
                            list(execution.get("closeout_notes", []))
                            + [f"{execution.get('worker_id')} lane_adherence_cross_lane_bleed matched={matched or '-'}"]
                        )
                    )
            semantic_review = _semantic_review_from_execution(execution)
            if semantic_review:
                execution["semantic_review"] = semantic_review
            results.append(execution)
            errors.extend(execution.get("errors", []))
            warnings.extend(execution.get("warnings", []))
        return {
            "results": results,
            "errors": list(dict.fromkeys(errors)),
            "warnings": list(dict.fromkeys(warnings)),
        }

    def finalize(state: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, int] = {}
        for item in state.get("results", []):
            key = str(item.get("state") or "unknown")
            summary[key] = summary.get(key, 0) + 1
        semantic_summary, semantic_retry_plan = _summarize_semantic_reviews(
            state.get("results", []) if isinstance(state.get("results"), list) else []
        )
        provider_error_summary = _summarize_provider_errors(
            state.get("results", []) if isinstance(state.get("results"), list) else []
        )
        lane_adherence_summary, lane_adherence_gate = _summarize_lane_adherence(
            state.get("results", []) if isinstance(state.get("results"), list) else []
        )
        run_metrics = build_run_metrics(
            state.get("results", []) if isinstance(state.get("results"), list) else [],
            lane_assignments=state.get("lane_assignments", []) if isinstance(state.get("lane_assignments"), list) else [],
        )
        execution_budget_status = evaluate_execution_budget(
            run_metrics,
            state.get("execution_budget", {}) if isinstance(state.get("execution_budget"), dict) else {},
        )
        run_control_advice = build_run_control_advice(
            run_metrics,
            execution_budget_status,
        )
        proof, proof_errors, proof_warnings = _finalize_business_no_write(state)
        result_lanes = _leader_decision_lanes_from_results(
            state.get("results", []) if isinstance(state.get("results"), list) else []
        )
        preflight_failed_before_submission = _preflight_failed_before_submission(state)
        if not result_lanes and preflight_failed_before_submission:
            result_lanes = _leader_decision_lanes_from_unexecuted_plan(state)
        leader_decision = build_protocol_leader_decision(
            run_id=f"langgraph-control-plane/{state.get('thread_id') or 'unknown'}",
            task_id="finalize",
            required_lanes=result_lanes,
            planner_coverage=state.get("planner_coverage") if isinstance(state.get("planner_coverage"), dict) else None,
            semantic_summary=semantic_summary,
            semantic_retry_plan=semantic_retry_plan,
            risk_review=state.get("risk_review") if isinstance(state.get("risk_review"), dict) else None,
            arbiter_decision=state.get("arbiter_decision") if isinstance(state.get("arbiter_decision"), dict) else None,
            control_plane_thread_id=str(state.get("thread_id") or ""),
        )
        if preflight_failed_before_submission:
            leader_decision = _project_preflight_failure_into_leader_decision(leader_decision, state)
        gate_warnings: list[str] = []
        if lane_adherence_gate == "failed":
            gate_warnings.append("lane_adherence_gate_failed")
        combined_errors = list(dict.fromkeys(list(state.get("errors", [])) + proof_errors))
        if combined_errors:
            if _all_results_completed(summary) and _only_retryable_provider_errors(
                combined_errors,
                state.get("results", []) if isinstance(state.get("results"), list) else [],
            ):
                final_state = "completed_with_retryable_provider_error"
            else:
                final_state = "failed"
        elif int(semantic_summary.get("failed", 0) or 0) > 0:
            final_state = "failed"
        elif int(summary.get("failed", 0) or 0) > 0:
            final_state = "failed"
        elif int(semantic_summary.get("blocked", 0) or 0) > 0:
            final_state = "blocked"
        elif int(summary.get("blocked", 0) or 0) > 0:
            final_state = "blocked"
        elif lane_adherence_gate == "failed":
            final_state = "partial"
        elif bool(state.get("rejected")):
            final_state = "rejected"
        elif summary and all(key == "completed" for key in summary):
            final_state = "completed"
        elif summary and all(key == "dry_run" for key in summary):
            final_state = "dry_run"
        else:
            final_state = "partial"
        metric_warnings = list(run_metrics.get("warnings", [])) if isinstance(run_metrics.get("warnings"), list) else []
        combined_warnings = list(
            dict.fromkeys(list(state.get("warnings", [])) + proof_warnings + gate_warnings + metric_warnings)
        )
        final_warnings = list(dict.fromkeys(proof_warnings + gate_warnings + metric_warnings))
        next_operator_action = _next_operator_action(
            {
                **state,
                "final_state": final_state,
                "semantic_summary": semantic_summary,
                "semantic_retry_plan": semantic_retry_plan,
                "lane_adherence_gate": lane_adherence_gate,
                "errors": combined_errors,
                "warnings": combined_warnings,
            }
        )
        return {
            "summary": summary,
            "final_state": final_state,
            "next_operator_action": next_operator_action,
            "finished_at": utc_now(),
            "lane_adherence_gate": lane_adherence_gate,
            "lane_adherence_summary": lane_adherence_summary,
            "semantic_summary": semantic_summary,
            "semantic_retry_plan": semantic_retry_plan,
            "provider_error_summary": provider_error_summary,
            "run_metrics": run_metrics,
            "execution_budget_status": execution_budget_status,
            "run_control_advice": run_control_advice,
            "leader_decision": leader_decision,
            "business_no_write_proof": proof,
            "errors": proof_errors,
            "warnings": final_warnings,
        }

    graph = StateGraph(GraphState)
    graph.add_node("preflight", preflight)
    graph.add_node("approval_gate", approval_gate)
    graph.add_node("submit_worker", submit_worker)
    graph.add_node("wait_and_collect", wait_and_collect, defer=True)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "preflight")
    graph.add_edge("preflight", "approval_gate")
    graph.add_conditional_edges("approval_gate", fanout_submit)
    graph.add_edge("submit_worker", "wait_and_collect")
    graph.add_edge("wait_and_collect", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver(), name="omx_langgraph_control_plane")


def invoke_control_plane(
    state: dict[str, Any],
    *,
    runner: CommandRunner = default_command_runner,
    thread_id: str | None = None,
    checkpoint_path: Path | str | None = None,
) -> dict[str, Any]:
    tid = thread_id or f"lgcp-{uuid.uuid4().hex[:12]}"
    config = {"configurable": {"thread_id": tid}}
    initial_state = {**state}
    initial_state.setdefault("thread_id", tid)
    initial_state.setdefault("contract_version", CONTRACT_VERSION)
    workers = [str(item) for item in initial_state.get("workers", [])]
    task_contracts = _load_routed_task_contracts(
        str(initial_state.get("lane_contracts") or ""),
        workers,
        Path(initial_state["root"]).resolve(),
    )
    if task_contracts:
        initial_state["task_contracts"] = task_contracts
        initial_state["task_contract_workers"] = list(task_contracts)
    if checkpoint_path is None:
        graph = build_langgraph_control_plane(runner=runner)
        final = graph.invoke(initial_state, config=config)
        final["thread_id"] = tid
        final["snapshot"] = snapshot_to_dict(graph.get_state(config))
        return final
    from langgraph.checkpoint.sqlite import SqliteSaver

    checkpoint = Path(checkpoint_path).absolute()
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph = build_langgraph_control_plane(runner=runner, checkpointer=saver)
        final = graph.invoke(initial_state, config=config)
        final["thread_id"] = tid
        final["snapshot"] = snapshot_to_dict(graph.get_state(config))
        return final


def inspect_control_plane(
    *,
    root: Path | str,
    thread_id: str,
    runner: CommandRunner = default_command_runner,
    checkpoint_path: Path | str | None = None,
) -> dict[str, Any]:
    from langgraph.checkpoint.sqlite import SqliteSaver

    root_path = Path(root).resolve()
    checkpoint = Path(checkpoint_path).absolute() if checkpoint_path else checkpoint_db_path(root_path)
    config = {"configurable": {"thread_id": thread_id}}
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph = build_langgraph_control_plane(runner=runner, checkpointer=saver)
        snapshot = graph.get_state(config)
    return {
        "thread_id": thread_id,
        "checkpoint_path": str(checkpoint),
        "snapshot": snapshot_to_dict(snapshot),
    }


def resume_control_plane(
    *,
    root: Path | str,
    thread_id: str,
    resume_value: Any,
    runner: CommandRunner = default_command_runner,
    checkpoint_path: Path | str | None = None,
) -> dict[str, Any]:
    from langgraph.checkpoint.sqlite import SqliteSaver

    root_path = Path(root).resolve()
    checkpoint = Path(checkpoint_path).absolute() if checkpoint_path else checkpoint_db_path(root_path)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    config = {"configurable": {"thread_id": thread_id}}
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph = build_langgraph_control_plane(runner=runner, checkpointer=saver)
        result = graph.invoke(Command(resume=resume_value), config=config)
        snapshot = graph.get_state(config)
    payload = {**result}
    payload["thread_id"] = thread_id
    payload["snapshot"] = snapshot_to_dict(snapshot)
    return payload
