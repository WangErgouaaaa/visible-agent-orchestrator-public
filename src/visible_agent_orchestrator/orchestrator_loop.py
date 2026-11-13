from __future__ import annotations

import json
import subprocess
import hashlib
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.blackboard_contract import build_blackboard_contract, build_evidence_blackboard
from visible_agent_orchestrator.lane_contracts import LaneContractError, inspect_lane_contract_bundle, load_lane_contract_bundle, _sources_overlap
from visible_agent_orchestrator.opencode_runtime_contracts import build_optional_lane_actuation_contract
from visible_agent_orchestrator.runtime.registry import REGISTRY_RELATIVE_PATH, load_visible_workers
from visible_agent_orchestrator.subagent_decision_control import build_opencode_subagent_decision_control_plan
from visible_agent_orchestrator.subagent_projection import build_subagent_projection


FORBIDDEN_ACTIONS = [
    "raw_tmux_input",
    "direct_tui_post",
    "lock_deletion",
    "marker_edit",
    "report_edit",
    "worker_restart_or_kill",
    "blind_redispatch",
]
TRUTH_PRECEDENCE = [
    "report.md + exactly one terminal marker",
    "task-local sidecars",
    "board projection",
    "runtime diagnostics",
    "tmux visual state",
]
TERMINAL_STATUSES = {"blocked", "cancelled", "completed", "done", "fail", "failed"}
ACTIVE_SIGNALS = {"submitted", "received", "started", "heartbeat", "running"}
RECOMMENDATION_POLICY = {
    "runtime_alert": {
        "gate": "warning",
        "description": "Watcher/runtime alerts require leader inspection before escalation.",
        "allows_dry_run_action_envelope": False,
    },
    "subagent_evidence": {
        "gate": "informational",
        "description": "Subagent projection warnings are advisory evidence and never completion truth.",
        "allows_dry_run_action_envelope": False,
    },
    "terminal_truth": {
        "gate": "hard_gate",
        "description": "Missing or uncertain terminal truth blocks any lock, redispatch, or recovery action.",
        "allows_dry_run_action_envelope": False,
    },
    "lock_consistency": {
        "gate": "hard_gate",
        "description": "Lock/task inconsistencies require terminal-truth review before any lock action.",
        "allows_dry_run_action_envelope": True,
    },
}


def build_loop_contract() -> dict[str, Any]:
    blackboard_contract = build_blackboard_contract()
    return {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_contract",
        "default_mode": "read_only_advisory",
        "actuation_enabled": False,
        "writes": [],
        "truth_precedence": TRUTH_PRECEDENCE,
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "recommendation_policy": RECOMMENDATION_POLICY,
        "blackboard_contract": blackboard_contract,
        "rollout_modes": blackboard_contract["modes"],
        "default_rollout_mode": blackboard_contract["default_mode"],
        "output_contract": {
            "contract": {
                "loop_kind": "orchestrator_loop_contract",
                "actuation_enabled": False,
                "writes": [],
            },
            "observe": {
                "loop_kind": "orchestrator_loop_observation",
                "actuation_enabled": False,
                "writes": [],
                "emits": ["summary", "workers", "recommendations"],
            },
            "advise": {
                "loop_kind": "orchestrator_loop_advisory_plan",
                "actuation_enabled": False,
                "writes": "requested advisory plan artifact only",
                "emits": ["summary", "recommendations"],
            },
            "advise_stdout_observation": {
                "loop_kind": "orchestrator_loop_advisor_handoff",
                "actuation_enabled": False,
                "emits": ["plan", "observation"],
            },
            "replan-plan": {
                "loop_kind": "orchestrator_loop_internal_replan_plan",
                "actuation_enabled": False,
                "writes": "requested replan artifact only",
                "emits": ["decision_control", "replan_plan"],
            },
            "replan-transition": {
                "loop_kind": "orchestrator_loop_internal_replan_transition",
                "actuation_enabled": False,
                "writes": "requested replan transition artifact only",
                "emits": ["attempted_action", "outcome", "next_state"],
            },
            "replan-followup": {
                "loop_kind": "orchestrator_loop_internal_replan_followup",
                "actuation_enabled": False,
                "writes": "requested replan followup artifact only",
                "emits": ["next_state", "followup_kind", "required_inputs"],
            },
            "replan-request": {
                "loop_kind": "orchestrator_loop_internal_replan_request",
                "actuation_enabled": False,
                "writes": "requested replan request artifact only",
                "emits": ["request_kind", "next_action", "consumption_mode", "required_inputs"],
            },
            "optional-lane-control-request": {
                "loop_kind": "orchestrator_loop_optional_lane_control_request",
                "actuation_enabled": False,
                "writes": "requested optional-lane control request artifact only",
                "emits": ["request_kind", "consumption_mode", "request_admissibility", "blocked_reasons"],
            },
            "request-consumer-plan": {
                "loop_kind": "orchestrator_loop_request_consumer_plan",
                "actuation_enabled": False,
                "writes": "requested request-consumer plan artifact only",
                "emits": ["consumer_mode", "next_step_kind", "source_request_kind", "blocked_reasons"],
            },
            "request-execution-gate": {
                "loop_kind": "orchestrator_loop_execution_gate_result",
                "actuation_enabled": False,
                "writes": "requested execution gate artifact only",
                "emits": ["ok", "blocked_reasons", "local_execution_allowed", "approval"],
            },
            "local-followup-executor": {
                "loop_kind": "orchestrator_loop_local_followup_executor",
                "actuation_enabled": False,
                "writes": "requested local executor output directory only",
                "emits": ["ok", "blocked_reasons", "executed_action", "artifacts"],
            },
            "runtime-handoff-plan": {
                "loop_kind": "orchestrator_loop_runtime_handoff_plan",
                "actuation_enabled": False,
                "writes": "requested runtime handoff plan artifact only",
                "emits": ["ok", "blocked_reasons", "execution_stub", "would_execute_runtime_control"],
            },
            "replan-chain": {
                "loop_kind": "orchestrator_loop_internal_replan_chain",
                "actuation_enabled": False,
                "writes": "requested chain artifact directory only",
                "emits": ["decision_control", "replan_plan", "transition", "followup"],
            },
            "actuation-plan": {
                "loop_kind": "orchestrator_loop_actuation_plan",
                "would_execute": False,
                "next_step": "leader_review_required",
            },
            "approval-actions": {
                "loop_kind": "orchestrator_loop_approval_action_plan",
                "approval_required": True,
                "would_execute": False,
            },
            "auto-actions": {
                "loop_kind": "orchestrator_loop_low_risk_auto_actions",
                "actuation_enabled": False,
                "writes": "requested artifact directory only",
            },
            "recovery-plan": {
                "loop_kind": "orchestrator_loop_recovery_plan",
                "would_execute": False,
                "next_step": "leader_review_required",
            },
        },
        "recommendation_taxonomy": [
            "wait_for_active_worker",
            "inspect_alerts",
            "inspect_subagent_warnings",
            "verify_terminal_truth",
            "leader_review_required",
            "dry_run_actuation_plan_available",
            "recovery_plan_available",
        ],
        "phases": {
            "OL-0": {
                "purpose": "Design the contract, truth order, schemas, and safety gates.",
                "actuation": "forbidden",
            },
            "OL-1": {
                "purpose": "Read board, locks, watcher, reports, markers, and sidecars into observations.",
                "actuation": "forbidden",
            },
            "OL-2": {
                "purpose": "Persist bounded advisory loop artifacts.",
                "actuation": "forbidden",
            },
            "OL-3": {
                "purpose": "Produce opt-in sanctioned-control-plane action envelopes.",
                "actuation": "dry_run_envelope_only",
            },
            "OL-4": {
                "purpose": "Produce evidence-gated recovery plans for leader review.",
                "actuation": "dry_run_recovery_plan_only",
            },
        },
    }


def observe_once(
    root: Path | str,
    *,
    board_payload: dict[str, Any] | None = None,
    lock_rows: list[dict[str, Any]] | None = None,
    run_metrics_data: dict[str, Any] | None = None,
    decision_control_data: dict[str, Any] | None = None,
    locks_command: str | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    board = board_payload if board_payload is not None else _load_board_payload(root_path)
    locks = lock_rows if lock_rows is not None else _load_lock_rows(root_path, locks_command)
    workers = _workers_from_board(board)
    tasks = _tasks_from_board(board)
    subagent_projection = board.get("opencode_subagent_projection") if isinstance(board.get("opencode_subagent_projection"), dict) else {}
    blackboard = _build_blackboard(board, subagent_projection)
    watcher = board.get("visible_watcher") if isinstance(board.get("visible_watcher"), dict) else {}
    watcher_alert_count = _int(watcher.get("alert_count")) if isinstance(watcher, dict) else 0
    watcher_snapshot_status = str(watcher.get("snapshot_status") or "")
    visible_workers_block = board.get("visible_workers") if isinstance(board.get("visible_workers"), dict) else {}
    registry_error = str(visible_workers_block.get("registry_error") or "")
    active_lock_count = len(locks)
    active_locks = _normalized_locks(locks)
    subagent_warning_count = _subagent_warning_count(subagent_projection)
    domain_missing_count = _domain_missing_count(blackboard)
    domain_state_summary = (
        blackboard.get("domain_state_summary")
        if isinstance(blackboard.get("domain_state_summary"), dict)
        else {}
    )
    low_value_subagent_count = _low_value_subagent_count(blackboard)
    direct_path_converged_first_count = _int(blackboard.get("value_summary", {}).get("direct_path_converged_first"))
    decision_control = _compact_decision_control(decision_control_data)
    efficiency_summary = (
        subagent_projection.get("efficiency_summary")
        if isinstance(subagent_projection.get("efficiency_summary"), dict)
        else {}
    )
    duplicate_lifecycle_rows = _int(efficiency_summary.get("duplicate_lifecycle_rows"))
    duplicate_terminal_rows = _int(efficiency_summary.get("duplicate_terminal_rows"))
    execution_budget_status = _execution_budget_status(run_metrics_data)

    observations: dict[str, Any] = {}
    for worker_id, worker in workers.items():
        worker_tasks = [task for task in tasks if _worker_for_task(task) == worker_id]
        worker_locks = [row for row in locks if _worker_for_lock(row) == worker_id]
        classification = _classify_worker(worker_tasks, worker_locks, worker)
        observations[worker_id] = {
            "worker_id": worker_id,
            "classification": classification,
            "active_lock_count": len(worker_locks),
            "task_count": len(worker_tasks),
            "alert_count": _int(worker.get("active_alert_count")),
        }

    recommendations: list[dict[str, Any]] = []
    if str(execution_budget_status.get("status") or "") in {"exhausted", "unavailable"}:
        recommendations.append(
            _with_policy(
                {
                    "recommendation": "stop_additional_followup",
                    "category": "execution_budget",
                    "severity": "info",
                    "source": "langgraph_run_metrics",
                    "next_action": "do_not_start_retry_or_new_iteration",
                    "reason": ", ".join(
                        str(item) for item in execution_budget_status.get("reasons", [])
                    ),
                    "source_fields": ["execution_budget_status"],
                }
            )
        )
    if watcher_alert_count:
        recommendations.append(
            _with_policy(
                {
                "recommendation": "inspect_alerts",
                "category": "runtime_alert",
                "severity": "warning",
                "source": "visible_watcher",
                "next_action": "inspect_watcher_alerts",
                "reason": f"watcher reported {watcher_alert_count} alert(s)",
                }
            )
        )
    if watcher_snapshot_status and watcher_snapshot_status != "present":
        recommendations.append(
            _with_policy(
                {
                "recommendation": "inspect_watcher_snapshot",
                "category": "runtime_alert",
                "severity": "warning",
                "source": "visible_watcher",
                "next_action": "inspect_watcher_snapshot",
                "reason": (
                    f"watcher snapshot {watcher_snapshot_status}; "
                    "task and subagent visibility degraded"
                ),
                }
            )
        )
    if registry_error:
        recommendations.append(
            _with_policy(
                {
                "recommendation": "inspect_worker_registry",
                "category": "registry_integrity",
                "severity": "warning",
                "source": "visible_worker_registry",
                "next_action": "inspect_worker_registry",
                "reason": f"visible worker registry unreadable: {registry_error}",
                }
            )
        )
    if subagent_warning_count:
        recommendations.append(
            _with_policy(
                {
                "recommendation": "inspect_subagent_warnings",
                "category": "subagent_evidence",
                "severity": "warning",
                "source": "opencode_subagent_projection",
                "next_action": "inspect_subagent_projection",
                "reason": f"subagent projection reported {subagent_warning_count} warning(s)",
                }
            )
        )
    for domain in blackboard.get("evidence_domains", {}).values():
        if not isinstance(domain, dict):
            continue
        if bool(domain.get("required")) and str(domain.get("status") or "") == "missing":
            recommendations.append(
                _with_policy(
                    {
                    "recommendation": "launch_missing_domain",
                    "legacy_recommendation": "provide_required_domain_artifact",
                    "category": "evidence_domain",
                    "severity": "warning",
                    "source": "domain_artifacts",
                    "next_action": "produce_or_attach_required_domain_artifact",
                    "evidence_domain": domain.get("evidence_domain") or "",
                    "worker_id": domain.get("worker_id") or "",
                    "missing": list(domain.get("missing") or []),
                    "source_fields": [f"domain_artifacts.domains.{domain.get('evidence_domain') or ''}"],
                    "reason": "required evidence domain is missing artifact(s)",
                    }
                )
            )
    for subagent in _projection_subagents(subagent_projection):
        if bool(subagent.get("direct_path_converged_first")) and str(subagent.get("required_or_optional") or "") == "optional":
            recommendations.append(
                _with_policy(
                    {
                    "recommendation": "skip_optional_lane",
                    "legacy_recommendation": "cancel_optional_lane",
                    "category": "subagent_value",
                    "severity": "info",
                    "source": "opencode_subagent_projection",
                    "next_action": "avoid_relaunching_optional_duplicate_lane",
                    "subagent_id": subagent.get("subagent_id") or "",
                    "agent": subagent.get("agent") or "",
                    "source_fields": ["opencode_subagent_projection.value_summary.direct_path_converged_first"],
                    "reason": "direct path converged before optional subagent value was needed",
                    }
                )
            )
        if str(subagent.get("recommended_closeout_action") or "") == "ignore_late_optional_lane":
            recommendations.append(
                _with_policy(
                    {
                    "recommendation": "cancel_late_optional_lane",
                    "legacy_recommendation": "cancel_optional_lane",
                    "category": "subagent_value",
                    "severity": "info",
                    "source": "opencode_subagent_projection",
                    "next_action": "ignore_late_optional_lane_after_parent_closeout",
                    "subagent_id": subagent.get("subagent_id") or "",
                    "agent": subagent.get("agent") or "",
                    "source_fields": ["opencode_subagent_projection.tasks[].subagents[].closeout_state"],
                    "reason": str(subagent.get("closeout_state") or "late optional lane no longer useful"),
                    }
                )
            )
    if duplicate_lifecycle_rows or duplicate_terminal_rows:
        recommendations.append(
            _with_policy(
                {
                "recommendation": "dedupe_lifecycle_noise",
                "category": "subagent_evidence",
                "severity": "info",
                "source": "opencode_subagent_projection",
                "next_action": "dedupe_repeated_lifecycle_rows_before_value_accounting",
                "duplicate_lifecycle_rows": duplicate_lifecycle_rows,
                "duplicate_terminal_rows": duplicate_terminal_rows,
                "source_fields": [
                    "opencode_subagent_projection.efficiency_summary.duplicate_lifecycle_rows",
                    "opencode_subagent_projection.efficiency_summary.duplicate_terminal_rows",
                ],
                "reason": "duplicate lifecycle or terminal rows were observed",
                }
            )
        )
    if direct_path_converged_first_count:
        recommendations.append(
            _with_policy(
                {
                "recommendation": "direct_tools_first",
                "category": "subagent_value",
                "severity": "info",
                "source": "opencode_subagent_projection",
                "next_action": "prefer_direct_tools_before_optional_nested_fanout",
                "count": direct_path_converged_first_count,
                "source_fields": ["opencode_subagent_projection.value_summary.direct_path_converged_first"],
                "reason": "direct path converged before optional nested fanout was needed",
                }
            )
        )
    if low_value_subagent_count:
        recommendations.append(
            _with_policy(
                {
                "recommendation": "skip_optional_lane",
                "legacy_recommendation": "flatten_low_value_nested_fanout",
                "category": "subagent_value",
                "severity": "info",
                "source": "opencode_subagent_projection",
                "next_action": "prefer_direct_or_fewer_subagent_lanes_for_repeated_low_value_domains",
                "source_fields": ["opencode_subagent_projection.value_summary"],
                "reason": f"{low_value_subagent_count} low-value subagent lane(s) observed",
                }
            )
        )
    for row in locks:
        worker_id = _worker_for_lock(row)
        run = _run_key(str(row.get("run") or ""))
        task_id = str(row.get("task") or row.get("task_id") or row.get("id") or "")
        if worker_id and run and task_id and not _task_exists(tasks, run, task_id):
            recommendations.append(
                _with_policy(
                    {
                    "recommendation": "verify_terminal_truth",
                    "category": "terminal_truth",
                    "severity": "info",
                    "source": "visible_locks",
                    "next_action": "leader_review_terminal_truth_before_lock_action",
                    "worker_id": worker_id,
                    "run": run,
                    "task_id": task_id,
                    "reason": "active lock has no matching task row in board payload",
                    }
                )
            )
            continue
        task = _find_task(tasks, run, task_id)
        if task and not _is_terminal_task(task):
            recommendations.append(
                _with_policy(
                    {
                    "recommendation": "wait_for_active_worker",
                    "category": "worker_runtime",
                    "severity": "info",
                    "source": "visible_locks",
                    "next_action": "wait_and_observe_without_releasing_lock",
                    "worker_id": worker_id,
                    "run": run,
                    "task_id": task_id,
                    "reason": "active lock points at non-terminal task",
                    "forbidden": ["release_lock", "redispatch", "raw_tmux_input"],
                    }
                )
            )
        if task and _is_terminal_task(task):
            recommendations.append(
                _with_policy(
                    {
                    "recommendation": "review_stale_lock",
                    "category": "lock_consistency",
                    "severity": "warning",
                    "source": "visible_locks",
                    "next_action": "leader_review_terminal_truth_before_lock_action",
                    "worker_id": worker_id,
                    "run": run,
                    "task_id": task_id,
                    "reason": "active lock points at task with terminal board status",
                    }
                )
            )

    recommendation_categories = _recommendation_categories(recommendations)

    return {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_observation",
        "generated_at": _utc_now(),
        "root": str(root_path),
        "actuation_enabled": False,
        "writes": [],
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
        "watcher_snapshot": {
            "generated_at": str(watcher.get("generated_at") or ""),
            "status": watcher_snapshot_status,
        },
        "summary": {
            "worker_count": len(workers),
            "task_count": len(tasks),
            "active_lock_count": active_lock_count,
            "watcher_alert_count": watcher_alert_count,
            "subagent_warning_count": subagent_warning_count,
            "domain_missing_count": domain_missing_count,
            "domain_state_summary": dict(domain_state_summary),
            "low_value_subagent_count": low_value_subagent_count,
            "direct_path_converged_first_count": direct_path_converged_first_count,
            "execution_budget_status": str(execution_budget_status.get("status") or ""),
            "decision_control_primary_recommendation": str(decision_control.get("primary_recommendation") or ""),
            "recommendation_count": len(recommendations),
            "recommendation_categories": recommendation_categories,
        },
        "blackboard": blackboard,
        "decision_control": decision_control,
        "active_locks": active_locks,
        "workers": observations,
        "recommendations": recommendations,
    }


def write_advisory_plan(observation: dict[str, Any], output: Path | str) -> dict[str, Any]:
    output_path = Path(output).resolve()
    payload = {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_advisory_plan",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "writes": [str(output_path)],
        "observation_kind": str(observation.get("loop_kind") or ""),
        "summary": _plan_summary(observation),
        "recommendations": list(observation.get("recommendations") or []),
        "decision_control": _compact_decision_control(observation.get("decision_control")),
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_replan_plan(observation: dict[str, Any], output: Path | str) -> dict[str, Any]:
    output_path = Path(output).resolve()
    decision_control = _compact_decision_control(observation.get("decision_control"))
    replan_plan = decision_control.get("replan_plan") if isinstance(decision_control.get("replan_plan"), dict) else {}
    payload = {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_internal_replan_plan",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "writes": [str(output_path)],
        "observation_kind": str(observation.get("loop_kind") or ""),
        "decision_control": decision_control,
        "replan_plan": dict(replan_plan),
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_replan_transition(
    replan_artifact: dict[str, Any],
    *,
    attempted_action: str,
    outcome: str,
    output: Path | str,
) -> dict[str, Any]:
    output_path = Path(output).resolve()
    replan_plan = _extract_replan_plan(replan_artifact)
    state_machine = replan_plan.get("state_machine") if isinstance(replan_plan.get("state_machine"), dict) else {}
    current_state = str(replan_plan.get("replan_state") or state_machine.get("current_state") or "")
    action = str(attempted_action or "").strip()
    normalized_outcome = str(outcome or "").strip().lower()
    allowed_actions = [str(item) for item in list(state_machine.get("allowed_actions") or []) if str(item)]
    blocked_reasons: list[str] = []
    if action not in allowed_actions:
        transition_allowed = False
        transition_reason = "attempted_action_not_allowed"
        next_state = current_state
        blocked_reasons.append(f"attempted_action_not_allowed_for_state:{action}:{current_state}")
    else:
        transition_allowed = True
        if normalized_outcome == "success":
            transition_reason = "success_path"
            next_state = str(state_machine.get("success_outcome") or "")
        else:
            transition_reason = "failure_path"
            next_state = str(state_machine.get("failure_state") or "")
    payload = {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_internal_replan_transition",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "writes": [str(output_path)],
        "attempted_action": action,
        "outcome": normalized_outcome,
        "transition_allowed": transition_allowed,
        "transition_reason": transition_reason,
        "current_state": current_state,
        "next_state": next_state,
        "replan_plan": dict(replan_plan),
        "blocked_reasons": blocked_reasons,
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_replan_followup(transition_artifact: dict[str, Any], output: Path | str) -> dict[str, Any]:
    output_path = Path(output).resolve()
    transition = _extract_replan_transition(transition_artifact)
    next_state = str(transition.get("next_state") or "")
    followup_kind, required_inputs = _followup_spec_for_state(next_state)
    payload = {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_internal_replan_followup",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "writes": [str(output_path)],
        "current_state": str(transition.get("current_state") or ""),
        "attempted_action": str(transition.get("attempted_action") or ""),
        "outcome": str(transition.get("outcome") or ""),
        "next_state": next_state,
        "followup_kind": followup_kind,
        "required_inputs": required_inputs,
        "transition": dict(transition),
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_replan_request(
    followup_artifact: dict[str, Any],
    *,
    decision_control_artifact: dict[str, Any] | None = None,
    lane_contracts_path: str = "",
    output: Path | str,
) -> dict[str, Any]:
    output_path = Path(output).resolve()
    followup = dict(followup_artifact) if isinstance(followup_artifact, dict) else {}
    decision_control_raw = (
        decision_control_artifact if isinstance(decision_control_artifact, dict) else {}
    )
    decision_control = _compact_decision_control(decision_control_raw)
    resolved_lane_contracts_path = _resolved_lane_contracts_path(
        lane_contracts_path,
        decision_control_artifact=decision_control_raw,
    )
    followup_kind = str(followup.get("followup_kind") or "")
    required_inputs = [str(item) for item in list(followup.get("required_inputs") or []) if str(item)]
    request_kind, next_action, consumption_mode, local_execution_allowed, request_payload = _replan_request_spec(
        followup_kind,
        decision_control_raw=decision_control_raw,
        resolved_lane_contracts_path=resolved_lane_contracts_path,
    )
    payload = {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_internal_replan_request",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "would_execute": False,
        "writes": [str(output_path)],
        "followup_kind": followup_kind,
        "request_kind": request_kind,
        "next_action": next_action,
        "consumption_mode": consumption_mode,
        "local_execution_allowed": local_execution_allowed,
        "required_inputs": required_inputs,
        "lane_contracts_path": resolved_lane_contracts_path,
        "decision_control": decision_control,
        "request_payload": request_payload,
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_optional_lane_control_request(
    subagent_artifact: dict[str, Any],
    *,
    action: str,
    reason: str,
    output: Path | str,
) -> dict[str, Any]:
    output_path = Path(output).resolve()
    runtime_contract = build_optional_lane_actuation_contract()
    normalized_action = str(action or "").strip()
    normalized_reason = str(reason or "").strip()
    target = _optional_lane_control_target(subagent_artifact)
    blocked_reasons = _optional_lane_control_blocked_reasons(
        target,
        action=normalized_action,
        runtime_contract=runtime_contract,
    )
    request_admissibility = "ready" if not blocked_reasons else "blocked"
    payload = {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_optional_lane_control_request",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "would_execute": False,
        "writes": [str(output_path)],
        "request_kind": "optional_lane_control_request",
        "consumption_mode": "runtime_dependent_followup",
        "local_execution_allowed": False,
        "runtime_execution_required": True,
        "next_action": normalized_action,
        "request_admissibility": request_admissibility,
        "blocked_reasons": blocked_reasons,
        "target": target,
        "runtime_contract": runtime_contract,
        "request_payload": (
            {
                "route_params": {
                    "session_id": str(target.get("route_session_id") or ""),
                    "task_id": str(target.get("route_task_id") or ""),
                },
                "request_body": {
                    "action": normalized_action,
                    "canonical_retrieval_handle": str(target.get("canonical_retrieval_handle") or ""),
                    "reason": normalized_reason,
                    "parent_task_id": str(target.get("parent_task_id") or ""),
                },
            }
            if not blocked_reasons
            else {}
        ),
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_request_consumer_plan(request_artifact: dict[str, Any], output: Path | str) -> dict[str, Any]:
    output_path = Path(output).resolve()
    request = dict(request_artifact) if isinstance(request_artifact, dict) else {}
    source_loop_kind = str(request.get("loop_kind") or "").strip()
    source_request_kind = str(request.get("request_kind") or "").strip()
    consumer_mode, next_step_kind, plan_payload, blocked_reasons = _request_consumer_spec(request)
    payload = {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_request_consumer_plan",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "would_execute": False,
        "actionable": False,
        "writes": [str(output_path)],
        "source_loop_kind": source_loop_kind,
        "source_request_kind": source_request_kind,
        "consumer_mode": consumer_mode,
        "next_step_kind": next_step_kind,
        "required_inputs": [str(item) for item in list(request.get("required_inputs") or []) if str(item)],
        "blocked_reasons": blocked_reasons,
        "plan_payload": plan_payload,
        "source_request": request,
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_request_execution_gate(consumer_plan: dict[str, Any], output: Path | str) -> dict[str, Any]:
    output_path = Path(output).resolve()
    plan = dict(consumer_plan) if isinstance(consumer_plan, dict) else {}
    consumer_mode = str(plan.get("consumer_mode") or "").strip()
    next_step_kind = str(plan.get("next_step_kind") or "").strip()
    input_blocked = [str(item) for item in list(plan.get("blocked_reasons") or []) if str(item)]
    gate_ok, blocked_reasons, local_execution_allowed = _execution_gate_decision(plan, input_blocked)
    approval = _execution_gate_approval(plan, next_step_kind, local_execution_allowed) if gate_ok else {}
    payload = {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_execution_gate_result",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "would_execute": False,
        "actionable": False,
        "ok": gate_ok,
        "writes": [str(output_path)],
        "consumer_mode": consumer_mode,
        "next_step_kind": next_step_kind,
        "local_execution_allowed": local_execution_allowed,
        "blocked_reasons": blocked_reasons,
        "approval": approval,
        "source_consumer_plan": plan,
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def run_local_followup_executor(gate_artifact: dict[str, Any], output_dir: Path | str) -> dict[str, Any]:
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    gate = dict(gate_artifact) if isinstance(gate_artifact, dict) else {}
    next_step_kind = str(gate.get("next_step_kind") or "").strip()
    blocked_reasons = [str(item) for item in list(gate.get("blocked_reasons") or []) if str(item)]
    artifacts: list[str] = []
    ok = False
    executed_action = ""
    if not bool(gate.get("ok")):
        blocked_reasons.append("gate_not_ok")
    if not bool(gate.get("local_execution_allowed")):
        blocked_reasons.append("local_execution_not_allowed")
    if next_step_kind != "replacement_contract_draft":
        blocked_reasons.append(f"unsupported_local_action:{next_step_kind}")
    source_plan = gate.get("source_consumer_plan") if isinstance(gate.get("source_consumer_plan"), dict) else {}
    lane_contracts_path = str((source_plan.get("plan_payload") or {}).get("lane_contracts_path") or "").strip()
    if next_step_kind == "replacement_contract_draft" and not blocked_reasons:
        draft_path = output_path / "replacement_contract_draft.json"
        try:
            draft_payload = _build_replacement_contract_draft(lane_contracts_path)
        except (LaneContractError, FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as exc:
            blocked_reasons.append(f"contract_invalid:{exc}")
        else:
            draft_path.write_text(json.dumps(draft_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            artifacts.append(str(draft_path))
            ok = True
            executed_action = next_step_kind
    result_path = output_path / "local_followup_executor.json"
    payload = {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_local_followup_executor",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "would_execute": False,
        "ok": ok,
        "writes": [str(result_path), *artifacts],
        "executed_action": executed_action,
        "next_step_kind": next_step_kind,
        "blocked_reasons": _dedupe_strings(blocked_reasons),
        "artifacts": artifacts,
        "source_gate": gate,
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
    }
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_runtime_handoff_plan(consumer_plan: dict[str, Any], output: Path | str) -> dict[str, Any]:
    output_path = Path(output).resolve()
    plan = dict(consumer_plan) if isinstance(consumer_plan, dict) else {}
    blocked_reasons = [str(item) for item in list(plan.get("blocked_reasons") or []) if str(item)]
    plan_payload = plan.get("plan_payload") if isinstance(plan.get("plan_payload"), dict) else {}
    runtime_contract = plan_payload.get("runtime_contract") if isinstance(plan_payload.get("runtime_contract"), dict) else {}
    request_payload = plan_payload.get("request_payload") if isinstance(plan_payload.get("request_payload"), dict) else {}
    target = plan_payload.get("target") if isinstance(plan_payload.get("target"), dict) else {}
    if str(plan.get("consumer_mode") or "") != "runtime_handoff_plan":
        blocked_reasons.append("not_runtime_handoff_plan")
    if not bool(plan_payload.get("handoff_ready")):
        blocked_reasons.append("handoff_not_ready")
    if str(runtime_contract.get("callable_surface") or "") != "control_background_lane":
        blocked_reasons.append("runtime_contract_invalid")
    if not isinstance(request_payload.get("route_params"), dict) or not isinstance(request_payload.get("request_body"), dict):
        blocked_reasons.append("runtime_request_payload_invalid")
    ok = not blocked_reasons
    payload = {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_runtime_handoff_plan",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "would_execute": False,
        "would_execute_runtime_control": False,
        "execution_stub": True,
        "ok": ok,
        "writes": [str(output_path)],
        "next_action": "handoff_to_runtime_owner",
        "handoff_recipe": {
            "callable_surface": str(runtime_contract.get("callable_surface") or ""),
            "route_params_source": "request_payload.route_params",
            "request_body_source": "request_payload.request_body",
            "operator_step": "runtime_owner_review_required",
        },
        "blocked_reasons": _dedupe_strings(blocked_reasons),
        "runtime_contract": runtime_contract,
        "request_payload": request_payload,
        "target": target,
        "source_consumer_plan": plan,
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_replan_chain(
    request: dict[str, Any],
    *,
    output_dir: Path | str,
    transition_outcome: str,
) -> dict[str, Any]:
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    decision_control = build_opencode_subagent_decision_control_plan(request)
    decision_control_path = output_path / "decision_control_plan.json"
    decision_control_path.write_text(json.dumps(decision_control, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    writes = [str(decision_control_path)]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_internal_replan_chain",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "would_execute": False,
        "writes": [],
        "chain_status": "blocked",
        "decision_control": decision_control,
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
    }
    if str(decision_control.get("primary_recommendation") or "") != "warn_contract_overlap":
        payload["writes"] = writes
        payload["blocked_reasons"] = ["not_warn_contract_overlap_chain_candidate"]
        return payload

    observation = {
        "loop_kind": "orchestrator_loop_observation",
        "decision_control": decision_control,
    }
    replan_plan_path = output_path / "internal_replan_plan.json"
    replan_plan = write_replan_plan(observation, replan_plan_path)
    transition_path = output_path / "internal_replan_transition.json"
    transition = write_replan_transition(
        replan_plan,
        attempted_action=str((replan_plan.get("replan_plan") or {}).get("recommended_action") or ""),
        outcome=transition_outcome,
        output=transition_path,
    )
    followup_path = output_path / "internal_replan_followup.json"
    followup = write_replan_followup(transition, followup_path)
    request_path = output_path / "internal_replan_request.json"
    request_artifact = write_replan_request(
        followup,
        decision_control_artifact=decision_control,
        lane_contracts_path=str(request.get("lane_contracts_path") or ""),
        output=request_path,
    )
    writes.extend(
        [
            str(replan_plan_path.resolve()),
            str(transition_path.resolve()),
            str(followup_path.resolve()),
            str(request_path.resolve()),
        ]
    )
    payload.update(
        {
            "writes": writes,
            "chain_status": "completed",
            "replan_plan": replan_plan,
            "transition": transition,
            "followup": followup,
            "request": request_artifact,
        }
    )
    return payload


def _compact_decision_control(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    lanes = value.get("lanes") if isinstance(value.get("lanes"), list) else []
    consumer_policy = value.get("consumer_policy") if isinstance(value.get("consumer_policy"), dict) else {}
    replan_plan = value.get("replan_plan") if isinstance(value.get("replan_plan"), dict) else {}
    return {
        "artifact_kind": str(value.get("artifact_kind") or ""),
        "primary_recommendation": str(value.get("primary_recommendation") or ""),
        "decision_phase": str(value.get("decision_phase") or ""),
        "decision_reason_code": str(value.get("decision_reason_code") or ""),
        "launchable_lane_count": _int(value.get("launchable_lane_count")),
        "consumer_policy": {
            "policy_kind": str(consumer_policy.get("policy_kind") or ""),
            "user_visible_by_default": bool(consumer_policy.get("user_visible_by_default")),
            "leader_action": str(consumer_policy.get("leader_action") or ""),
            "gate_action": str(consumer_policy.get("gate_action") or ""),
            "allowed_followups": [str(item) for item in list(consumer_policy.get("allowed_followups") or []) if str(item)],
            "forbidden_followups": [str(item) for item in list(consumer_policy.get("forbidden_followups") or []) if str(item)],
            "escalation_conditions": [
                str(item) for item in list(consumer_policy.get("escalation_conditions") or []) if str(item)
            ],
        }
        if consumer_policy
        else {},
        "replan_plan": {
            "plan_kind": str(replan_plan.get("plan_kind") or ""),
            "replan_state": str(replan_plan.get("replan_state") or ""),
            "recommended_action": str(replan_plan.get("recommended_action") or ""),
            "candidate_actions": [str(item) for item in list(replan_plan.get("candidate_actions") or []) if str(item)],
            "allowed_to_execute_now": bool(replan_plan.get("allowed_to_execute_now")),
            "user_prompt_required": bool(replan_plan.get("user_prompt_required")),
            "state_machine": {
                "state_kind": str((replan_plan.get("state_machine") or {}).get("state_kind") or ""),
                "current_state": str((replan_plan.get("state_machine") or {}).get("current_state") or ""),
                "allowed_actions": [
                    str(item)
                    for item in list(((replan_plan.get("state_machine") or {}).get("allowed_actions") or []))
                    if str(item)
                ],
                "success_outcome": str((replan_plan.get("state_machine") or {}).get("success_outcome") or ""),
                "failure_state": str((replan_plan.get("state_machine") or {}).get("failure_state") or ""),
            }
            if isinstance(replan_plan.get("state_machine"), dict)
            else {},
            "escalation_conditions": [
                str(item) for item in list(replan_plan.get("escalation_conditions") or []) if str(item)
            ],
        }
        if replan_plan
        else {},
        "lanes": [
            {
                "worker_id": str(item.get("worker_id") or ""),
                "lane_id": str(item.get("lane_id") or ""),
                "evidence_domain": str(item.get("evidence_domain") or ""),
                "spawn_recommendation": str(item.get("spawn_recommendation") or ""),
                "reason": str(item.get("reason") or ""),
            }
            for item in lanes
            if isinstance(item, dict)
        ],
    }


def _extract_replan_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    loop_kind = str(value.get("loop_kind") or "")
    if loop_kind == "orchestrator_loop_internal_replan_plan":
        extracted = value.get("replan_plan")
        return dict(extracted) if isinstance(extracted, dict) else {}
    extracted = value.get("replan_plan")
    return dict(extracted) if isinstance(extracted, dict) else {}


def _extract_replan_transition(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return dict(value)


def _resolved_lane_contracts_path(
    lane_contracts_path: str,
    *,
    decision_control_artifact: dict[str, Any],
) -> str:
    explicit = str(lane_contracts_path or "").strip()
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    embedded = str(decision_control_artifact.get("lane_contracts_path") or "").strip()
    if embedded:
        return str(Path(embedded).expanduser().resolve())
    return ""


def _replan_request_spec(
    followup_kind: str,
    *,
    decision_control_raw: dict[str, Any],
    resolved_lane_contracts_path: str,
) -> tuple[str, str, str, bool, dict[str, Any]]:
    kind = str(followup_kind or "").strip()
    if kind == "decision_control_rerun_request":
        return (
            "decision_control_rerun_request",
            "rerun_decision_control",
            "local_followup",
            True,
            {
                "lane_contracts_path": resolved_lane_contracts_path,
                "rerun_mode": "decision_control",
            },
        )
    if kind == "direct_fallback_task":
        return (
            "direct_fallback_task_request",
            "direct_local_tools",
            "local_followup",
            True,
            {
                "recommended_action": "direct_local_tools",
                "local_tool_scope": "",
                "verification_target": "",
            },
        )
    if kind == "optional_lane_drop_contract_patch":
        return (
            "optional_lane_drop_contract_patch_request",
            "optional_lane_removal_via_new_contract",
            "local_followup",
            True,
            {
                "lane_contracts_path": resolved_lane_contracts_path,
                "optional_lane_selection": _optional_lane_selection(decision_control_raw),
            },
        )
    if kind == "replacement_contract_draft":
        return (
            "replacement_contract_draft_request",
            "replacement_contract",
            "local_followup",
            True,
            {
                "lane_contracts_path": resolved_lane_contracts_path,
                "contract_warning_summary": _contract_warning_summary(decision_control_raw),
            },
        )
    return (
        "replan_escalation_note_request",
        "manual_escalation",
        "manual_escalation",
        False,
        {
            "reason": "followup requires leader review",
        },
    )


def _optional_lane_control_target(subagent_artifact: dict[str, Any]) -> dict[str, Any]:
    row = dict(subagent_artifact) if isinstance(subagent_artifact, dict) else {}
    route_session_id = str(row.get("trace_session_id") or row.get("fallback_session_id") or "").strip()
    route_task_id = str(row.get("trace_task_id") or row.get("task_id") or "").strip()
    required_or_optional = str(row.get("required_or_optional") or "").strip()
    retrieval_state = str(row.get("retrieval_state") or "").strip()
    return {
        "subagent_id": str(row.get("subagent_id") or "").strip(),
        "agent": str(row.get("agent") or "").strip(),
        "required_or_optional": required_or_optional,
        "retrieval_state": retrieval_state,
        "canonical_retrieval_handle": str(row.get("canonical_retrieval_handle") or "").strip(),
        "parent_task_id": str(row.get("parent_task_id") or "").strip(),
        "route_session_id": route_session_id,
        "route_task_id": route_task_id,
        "route_session_source": (
            "trace_session_id"
            if str(row.get("trace_session_id") or "").strip()
            else "fallback_session_id"
            if str(row.get("fallback_session_id") or "").strip()
            else ""
        ),
        "route_task_source": (
            "trace_task_id"
            if str(row.get("trace_task_id") or "").strip()
            else "task_id"
            if str(row.get("task_id") or "").strip()
            else ""
        ),
    }


def _optional_lane_control_blocked_reasons(
    target: dict[str, Any],
    *,
    action: str,
    runtime_contract: dict[str, Any],
) -> list[str]:
    blocked_reasons: list[str] = []
    allowed_actions = runtime_contract.get("request", {}).get("allowed_actions")
    allowed = [str(item) for item in list(allowed_actions or []) if str(item)]
    normalized_action = str(action or "").strip()
    if normalized_action not in allowed:
        blocked_reasons.append(f"invalid_action:{normalized_action}")
    if str(target.get("required_or_optional") or "") != "optional":
        blocked_reasons.append("required_lane_rejected")
    if not str(target.get("canonical_retrieval_handle") or ""):
        blocked_reasons.append("missing_canonical_retrieval_handle")
    if not str(target.get("parent_task_id") or ""):
        blocked_reasons.append("missing_parent_task_id")
    if not str(target.get("route_session_id") or ""):
        blocked_reasons.append("missing_route_session_id")
    if not str(target.get("route_task_id") or ""):
        blocked_reasons.append("missing_route_task_id")
    return blocked_reasons


def _request_consumer_spec(request: dict[str, Any]) -> tuple[str, str, dict[str, Any], list[str]]:
    source_request_kind = str(request.get("request_kind") or "").strip()
    consumption_mode = str(request.get("consumption_mode") or "").strip()
    blocked_reasons = [str(item) for item in list(request.get("blocked_reasons") or []) if str(item)]
    if source_request_kind == "optional_lane_control_request" or consumption_mode == "runtime_dependent_followup":
        return (
            "runtime_handoff_plan",
            "runtime_optional_lane_control_handoff",
            {
                "handoff_ready": str(request.get("request_admissibility") or "") == "ready",
                "runtime_contract": dict(request.get("runtime_contract") or {}),
                "request_payload": dict(request.get("request_payload") or {}),
                "target": dict(request.get("target") or {}),
            },
            blocked_reasons,
        )
    if consumption_mode == "manual_escalation" or source_request_kind == "replan_escalation_note_request":
        return (
            "manual_escalation_plan",
            "manual_review",
            {
                "reason": str((request.get("request_payload") or {}).get("reason") or ""),
            },
            blocked_reasons,
        )
    local_map = {
        "decision_control_rerun_request": "decision_control_rerun",
        "direct_fallback_task_request": "direct_local_tools",
        "replacement_contract_draft_request": "replacement_contract_draft",
        "optional_lane_drop_contract_patch_request": "optional_lane_drop_contract_patch",
    }
    next_step_kind = local_map.get(source_request_kind, "manual_review")
    consumer_mode = "local_followup_plan" if source_request_kind in local_map else "manual_escalation_plan"
    plan_payload = {
        "local_execution_allowed": bool(request.get("local_execution_allowed")),
        "request_payload": dict(request.get("request_payload") or {}),
        "lane_contracts_path": str(request.get("lane_contracts_path") or ""),
    }
    return consumer_mode, next_step_kind, plan_payload, blocked_reasons


def _execution_gate_decision(plan: dict[str, Any], input_blocked: list[str]) -> tuple[bool, list[str], bool]:
    blocked_reasons = list(input_blocked)
    consumer_mode = str(plan.get("consumer_mode") or "").strip()
    next_step_kind = str(plan.get("next_step_kind") or "").strip()
    local_execution_allowed = False
    if consumer_mode == "manual_escalation_plan":
        return (not blocked_reasons, _dedupe_strings(blocked_reasons), False)
    if consumer_mode == "runtime_handoff_plan":
        return (not blocked_reasons, _dedupe_strings(blocked_reasons), False)
    if consumer_mode != "local_followup_plan":
        blocked_reasons.append("unknown_consumer_mode")
        return False, _dedupe_strings(blocked_reasons), False
    supported_local_kind = "replacement_contract_draft"
    explicitly_rejected_local_kinds = {
        "decision_control_rerun",
        "direct_local_tools",
        "optional_lane_drop_contract_patch",
    }
    known_local_kinds = explicitly_rejected_local_kinds | {supported_local_kind}
    if next_step_kind not in known_local_kinds:
        blocked_reasons.append(f"unknown_local_next_step:{next_step_kind}")
        return False, _dedupe_strings(blocked_reasons), False
    if next_step_kind in explicitly_rejected_local_kinds:
        blocked_reasons.append(f"unsupported_local_action:{next_step_kind}")
        return False, _dedupe_strings(blocked_reasons), False
    plan_payload = plan.get("plan_payload") if isinstance(plan.get("plan_payload"), dict) else {}
    if not bool(plan_payload.get("local_execution_allowed")):
        blocked_reasons.append("plan_payload_local_execution_not_allowed")
    if next_step_kind == supported_local_kind:
        if not str(plan_payload.get("lane_contracts_path") or "").strip():
            blocked_reasons.append("missing_lane_contracts_path")
    local_execution_allowed = not blocked_reasons
    return local_execution_allowed, _dedupe_strings(blocked_reasons), local_execution_allowed


def _execution_gate_approval(plan: dict[str, Any], next_step_kind: str, local_execution_allowed: bool) -> dict[str, Any]:
    scope = "local_followup" if local_execution_allowed else "runtime_or_manual_only"
    material = json.dumps(
        {
            "consumer_mode": plan.get("consumer_mode"),
            "next_step_kind": next_step_kind,
            "scope": scope,
        },
        sort_keys=True,
    ).encode("utf-8")
    approval_id = f"ega-{hashlib.sha256(material).hexdigest()[:12]}"
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "execution_envelope_kind": "execution_gate_approval",
        "approval_id": approval_id,
        "scope": scope,
        "expires_at": expires_at,
        "allowed_actions": [next_step_kind] if next_step_kind else [],
        "execution_stub": True,
        "actuation_enabled": False,
    }


def _build_replacement_contract_draft(lane_contracts_path: str) -> dict[str, Any]:
    path = str(lane_contracts_path or "").strip()
    if not path:
        raise ValueError("lane_contracts_path_missing")
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("lane_contracts_not_object")
    assignments = raw.get("assignments")
    if not isinstance(assignments, list) or not assignments:
        raise ValueError("assignments_missing")
    workers = [str(item.get("worker_id") or "").strip() for item in assignments if isinstance(item, dict)]
    if not all(workers):
        raise ValueError("assignment_worker_id_missing")
    inspection = inspect_lane_contract_bundle(path, workers=workers)
    blocked_reasons = [str(item) for item in list(inspection.get("blocked_reasons") or []) if str(item)]
    overlap_blockers = [item for item in blocked_reasons if item.startswith("overlapping subdomain sources:")]
    non_overlap_blockers = [item for item in blocked_reasons if item not in overlap_blockers]
    if non_overlap_blockers:
        raise ValueError(non_overlap_blockers[0])
    rewritten_assignments, rewrite_summary = _rewrite_overlap_assignments(assignments)
    rewritten_contract = {
        "assignments": rewritten_assignments,
        "planner_coverage": raw.get("planner_coverage", {}),
        "context_policy": raw.get("context_policy", {}),
    }
    _validate_replacement_contract_draft(rewritten_contract, workers=workers)
    draft_status = "manual_followup_required" if rewrite_summary["cross_role_resolution_count"] else "ready_for_revalidation"
    return {
        "draft_kind": "replacement_contract_draft",
        "draft_status": draft_status,
        "source_lane_contracts_path": str(Path(path).resolve()),
        "source_contract_warnings": [item for item in list(inspection.get("contract_warnings") or []) if isinstance(item, dict)],
        "source_blocked_reasons": blocked_reasons,
        "original_assignments": assignments,
        "assignments": rewritten_assignments,
        "planner_coverage": raw.get("planner_coverage", {}),
        "context_policy": raw.get("context_policy", {}),
        "rewrite_summary": rewrite_summary,
        "draft_notes": [
            "review rewritten assignments before reuse",
            "re-run decision control against this draft before any future semi-auto consumption",
        ],
    }


def _rewrite_overlap_assignments(assignments: list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rewritten_assignments: list[dict[str, Any]] = []
    resolution_rows: list[dict[str, Any]] = []
    merged_same_role_count = 0
    dropped_cross_role_count = 0
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        rewritten = json.loads(json.dumps(assignment))
        subdomains = [dict(item) for item in list(rewritten.get("subdomains") or []) if isinstance(item, dict)]
        kept: list[dict[str, Any]] = []
        for subdomain in subdomains:
            overlapping_keeper = _find_overlapping_keeper(kept, subdomain)
            if overlapping_keeper is None:
                kept.append(subdomain)
                continue
            overlap_left, overlap_right = _first_overlap_pair(overlapping_keeper, subdomain)
            same_role = str(overlapping_keeper.get("agent") or "") == str(subdomain.get("agent") or "")
            if same_role:
                overlapping_keeper["expected_unique_value"] = _merge_expected_unique_values(
                    str(overlapping_keeper.get("expected_unique_value") or ""),
                    str(subdomain.get("expected_unique_value") or ""),
                )
                merged_same_role_count += 1
                action = "merged_same_role_overlap"
            else:
                dropped_cross_role_count += 1
                action = "dropped_cross_role_overlap"
            resolution_rows.append(
                {
                    "lane_id": str(rewritten.get("lane_id") or ""),
                    "kept_subdomain_id": str(overlapping_keeper.get("subdomain_id") or ""),
                    "dropped_subdomain_id": str(subdomain.get("subdomain_id") or ""),
                    "kept_agent": str(overlapping_keeper.get("agent") or ""),
                    "dropped_agent": str(subdomain.get("agent") or ""),
                    "overlap_scope": "same_role_subdomain" if same_role else "cross_role_subdomain",
                    "kept_source": overlap_left,
                    "dropped_source": overlap_right,
                    "action": action,
                }
            )
        rewritten["subdomains"] = kept
        rewritten_assignments.append(rewritten)
    return rewritten_assignments, {
        "resolution_count": len(resolution_rows),
        "same_role_merge_count": merged_same_role_count,
        "cross_role_resolution_count": dropped_cross_role_count,
        "resolutions": resolution_rows,
    }


def _find_overlapping_keeper(kept: list[dict[str, Any]], candidate: dict[str, Any]) -> dict[str, Any] | None:
    for keeper in kept:
        if _subdomains_overlap(keeper, candidate):
            return keeper
    return None


def _subdomains_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for left_source in [str(item) for item in list(left.get("allowed_sources") or []) if str(item)]:
        for right_source in [str(item) for item in list(right.get("allowed_sources") or []) if str(item)]:
            if _sources_overlap(left_source, right_source):
                return True
    return False


def _first_overlap_pair(left: dict[str, Any], right: dict[str, Any]) -> tuple[str, str]:
    for left_source in [str(item) for item in list(left.get("allowed_sources") or []) if str(item)]:
        for right_source in [str(item) for item in list(right.get("allowed_sources") or []) if str(item)]:
            if _sources_overlap(left_source, right_source):
                return left_source, right_source
    return "", ""


def _merge_expected_unique_values(left: str, right: str) -> str:
    values = [str(item).strip() for item in [left, right] if str(item).strip()]
    return " / ".join(_dedupe_strings(values))


def _validate_replacement_contract_draft(contract: dict[str, Any], *, workers: list[str]) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=True) as handle:
        handle.write(json.dumps(contract, indent=2, sort_keys=True) + "\n")
        handle.flush()
        load_lane_contract_bundle(handle.name, workers=workers)


def _contract_warning_summary(decision_control_raw: dict[str, Any]) -> list[dict[str, Any]]:
    warnings = decision_control_raw.get("contract_warnings")
    rows = [row for row in warnings if isinstance(row, dict)] if isinstance(warnings, list) else []
    summary: list[dict[str, Any]] = []
    for row in rows:
        summary.append(
            {
                "warning_code": str(row.get("warning_code") or ""),
                "warning_message": str(row.get("warning_message") or ""),
            }
        )
    return summary


def _optional_lane_selection(decision_control_raw: dict[str, Any]) -> list[dict[str, Any]]:
    lanes = decision_control_raw.get("lanes")
    rows = [row for row in lanes if isinstance(row, dict)] if isinstance(lanes, list) else []
    selected: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("required_or_optional") or "") != "optional":
            continue
        selected.append(
            {
                "worker_id": str(row.get("worker_id") or ""),
                "lane_id": str(row.get("lane_id") or ""),
                "evidence_domain": str(row.get("evidence_domain") or ""),
            }
        )
    return selected


def _followup_spec_for_state(next_state: str) -> tuple[str, list[str]]:
    state = str(next_state or "").strip()
    if state == "revalidate_with_new_decision_control":
        return "decision_control_rerun_request", ["updated_lane_contracts", "rerun_decision_control"]
    if state == "direct_fallback_ready":
        return "direct_fallback_task", ["local_tool_scope", "verification_target"]
    if state == "optional_lane_drop_ready":
        return "optional_lane_drop_contract_patch", ["current_lane_contracts", "optional_lane_selection"]
    if state == "replacement_contract_required":
        return "replacement_contract_draft", ["current_lane_contracts", "overlap_scope_review"]
    return "replan_escalation_note", ["leader_review", "manual_decision"]


def build_actuation_plan(
    *,
    action: str,
    target: dict[str, Any],
    evidence: dict[str, Any],
    dry_run: bool = True,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_actuation_plan",
        "generated_at": _utc_now(),
        "action": str(action or ""),
        "target": dict(target),
        "evidence": dict(evidence),
        "dry_run": bool(dry_run),
        "would_execute": False,
        "blocked_by_policy": True,
        "policy_gate": "hard_gate",
        "action_draft": _action_draft(action),
        "sanctioned_control_plane_required": True,
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
        "next_step": "leader_review_required",
    }


def build_approval_action_plan(observation: dict[str, Any]) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    root = str(observation.get("root") or "")
    recommendations = observation.get("recommendations")
    rows = [row for row in recommendations if isinstance(row, dict)] if isinstance(recommendations, list) else []
    for row in rows:
        recommendation = str(row.get("recommendation") or "")
        worker_id = str(row.get("worker_id") or "")
        run = str(row.get("run") or "")
        task_id = str(row.get("task_id") or "")
        if recommendation == "wait_for_active_worker":
            actions.append(
                {
                    "action": "wait_for_worker",
                    "approval_required": True,
                    "would_execute": False,
                    "worker_id": worker_id,
                    "run": run,
                    "task_id": task_id,
                    "recommendation": "wait",
                    "reason": row.get("reason") or "active lock exists and task is not terminal",
                    "forbidden": list(row.get("forbidden") or ["release_lock", "redispatch", "raw_tmux_input"]),
                    "command": [
                        "omx_visible_dispatch",
                        "status",
                        "--root",
                        root,
                        "--worker",
                        worker_id,
                    ],
                }
            )
        if recommendation == "review_stale_lock":
            actions.append(
                {
                    "action": "settle_terminal_stale_lock",
                    "approval_required": True,
                    "would_execute": False,
                    "worker_id": worker_id,
                    "run": run,
                    "task_id": task_id,
                    "recommendation": "settle_after_leader_approval",
                    "reason": row.get("reason") or "active lock points at terminal task",
                    "evidence_required": [
                        "non_empty_report.md",
                        "exactly_one_terminal_marker",
                        "lock_points_at_terminal_task",
                        "leader_approval",
                    ],
                    "command": [
                        "omx_visible_dispatch",
                        "settle-latest",
                        "--root",
                        root,
                        "--worker",
                        worker_id,
                        "--allow-terminal-archive",
                    ],
                }
            )
    return {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_approval_action_plan",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "approval_required": True,
        "would_execute": False,
        "writes": [],
        "action_count": len(actions),
        "actions": actions,
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
        "next_step": "leader_review_required",
    }


def run_low_risk_auto_actions(observation: dict[str, Any], output_dir: Path | str) -> dict[str, Any]:
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    observation_path = output_path / "observation.json"
    advisory_path = output_path / "advisory-plan.json"
    summary_path = output_path / "status-summary.md"
    archive_path = output_path / "archive-candidates.json"

    observation_path.write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    advisory = write_advisory_plan(observation, advisory_path)
    summary_path.write_text(_status_summary_markdown(observation), encoding="utf-8")
    archive_path.write_text(
        json.dumps(_archive_candidates(observation), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    writes = [observation_path, advisory_path, summary_path, archive_path]
    return {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_low_risk_auto_actions",
        "generated_at": _utc_now(),
        "actuation_enabled": False,
        "would_execute_control_plane": False,
        "writes": [str(path) for path in writes],
        "executed_actions": [
            "write_observation_snapshot",
            "write_advisory_plan",
            "write_status_summary",
            "write_archive_candidates",
        ],
        "advisory_plan_summary": advisory.get("summary", {}),
        "forbidden_actions": FORBIDDEN_ACTIONS,
    }


def _with_policy(recommendation: dict[str, Any]) -> dict[str, Any]:
    category = str(recommendation.get("category") or "")
    policy = RECOMMENDATION_POLICY.get(category, {})
    return {
        "actuation_enabled": False,
        "would_execute": False,
        **recommendation,
        "policy_gate": str(policy.get("gate") or "informational"),
        "allows_dry_run_action_envelope": bool(policy.get("allows_dry_run_action_envelope")),
    }


def _action_draft(action: str) -> dict[str, Any]:
    normalized = str(action or "").strip()
    if normalized in {"release_lock", "review_lock_release"}:
        return {
            "kind": "review_lock_release",
            "control_plane": "omx_visible_dispatch",
            "preflight_required": [
                "verify_terminal_truth",
                "verify_owner_lock",
                "operator_approval",
            ],
            "execution_allowed": False,
        }
    return {
        "kind": normalized or "leader_review_required",
        "control_plane": "sanctioned_control_plane_required",
        "preflight_required": [
            "verify_terminal_truth",
            "operator_approval",
        ],
        "execution_allowed": False,
    }


def build_recovery_plan(*, scenario: str, target: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "loop_kind": "orchestrator_loop_recovery_plan",
        "generated_at": _utc_now(),
        "scenario": str(scenario or ""),
        "target": dict(target),
        "evidence": dict(evidence),
        "would_execute": False,
        "next_step": "leader_review_required",
        "evidence_gates": {
            "terminal_truth_required": True,
            "owner_lock_required": True,
            "raw_tmux_forbidden": True,
            "dry_run_before_control_plane": True,
        },
        "forbidden_actions": FORBIDDEN_ACTIONS,
        "truth_precedence": TRUTH_PRECEDENCE,
    }


def _load_board_payload(root: Path) -> dict[str, Any]:
    """Assemble the observation payload without the retired team board.

    Sources: the watcher's visible_board.json (advisory watcher telemetry,
    never terminal truth), the visible worker registry (worker rows), and
    the library subagent projection.
    """
    watcher_board, watcher_snapshot_status = _read_watcher_board(root)
    tasks = [_watcher_task_as_board_task(row) for row in watcher_board.get("tasks", []) if isinstance(row, dict)]
    board: dict[str, Any] = {
        "tasks": tasks,
        "visible_workers": _registry_visible_workers(root),
        "visible_watcher": {
            "alert_count": _int(watcher_board.get("alert_count")),
            "active_alerts": _int(watcher_board.get("active_alerts")),
            "generated_at": str(watcher_board.get("generated_at") or ""),
            "snapshot_status": watcher_snapshot_status,
        },
    }
    board["opencode_subagent_projection"] = build_subagent_projection(root, tasks)
    return board


def _read_watcher_board(root: Path) -> tuple[dict[str, Any], str]:
    path = root / ".omx" / "framework" / "team" / "visible-runs" / "_watcher" / "visible_board.json"
    if not path.is_file():
        return {}, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, "unreadable"
    if not isinstance(data, dict):
        return {}, "unreadable"
    return data, "present"


def _watcher_task_as_board_task(row: dict[str, Any]) -> dict[str, Any]:
    task = dict(row)
    task.setdefault("id", str(row.get("task") or ""))
    flow_markers = row.get("flow_markers")
    task.setdefault("signals", [str(item) for item in flow_markers] if isinstance(flow_markers, list) else [])
    return task


def _registry_visible_workers(root: Path) -> dict[str, Any]:
    """Worker rows from the registry; a malformed registry is surfaced, not hidden."""
    try:
        workers = load_visible_workers(root)
    except Exception as exc:
        if not (root / REGISTRY_RELATIVE_PATH).is_file():
            return {"workers": []}
        return {"workers": [], "registry_error": f"{type(exc).__name__}: {exc}"}
    return {"workers": workers}


def _load_lock_rows(root: Path, locks_command: str | None) -> list[dict[str, Any]]:
    command = locks_command or str(root / "bin" / "omx_visible_dispatch")
    if not Path(command).is_file():
        command = "omx_visible_dispatch"
    payload = _run_json([command, "locks", "--root", str(root), "--json"])
    rows = payload.get("locks") if isinstance(payload, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _run_json(argv: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        return {"_error": str(exc)}
    if proc.returncode != 0:
        return {"_error": proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"_error": f"invalid JSON: {exc}"}
    return data if isinstance(data, dict) else {"_error": "non-object JSON"}


def _workers_from_board(board: dict[str, Any]) -> dict[str, dict[str, Any]]:
    visible = board.get("visible_workers") if isinstance(board.get("visible_workers"), dict) else {}
    rows = visible.get("workers") if isinstance(visible, dict) else []
    result: dict[str, dict[str, Any]] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            worker_id = str(row.get("worker_id") or row.get("worker") or "")
            if worker_id:
                result[worker_id] = row
    return result


def _tasks_from_board(board: dict[str, Any]) -> list[dict[str, Any]]:
    rows = board.get("tasks")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _classify_worker(tasks: list[dict[str, Any]], locks: list[dict[str, Any]], worker: dict[str, Any]) -> str:
    if locks:
        locked_tasks: list[dict[str, Any]] = []
        for lock in locks:
            run = _run_key(str(lock.get("run") or ""))
            task_id = str(lock.get("task") or lock.get("task_id") or lock.get("id") or "")
            task = _find_task(tasks, run, task_id) if run and task_id else None
            if task:
                locked_tasks.append(task)
        if not locked_tasks:
            return "assigned"
        for task in locked_tasks:
            status = str(task.get("status") or "").lower()
            if status not in TERMINAL_STATUSES and (_signals(task) & ACTIVE_SIGNALS or status):
                return "running"
        terminal_tasks = [task for task in locked_tasks if _is_terminal_task(task)]
    else:
        terminal_tasks = [task for task in tasks if str(task.get("status") or "").lower() in TERMINAL_STATUSES]
    if terminal_tasks:
        return str(terminal_tasks[0].get("status") or "completed").lower()
    if worker:
        return "ready" if bool(worker.get("ready_for_run_one") or worker.get("submit_safe")) else "disabled"
    return "unknown"


def _build_blackboard(board: dict[str, Any], subagent_projection: dict[str, Any]) -> dict[str, Any]:
    return build_evidence_blackboard(board, subagent_projection)


def _domain_missing_count(blackboard: dict[str, Any]) -> int:
    domains = blackboard.get("evidence_domains") if isinstance(blackboard.get("evidence_domains"), dict) else {}
    return sum(
        1
        for domain in domains.values()
        if isinstance(domain, dict)
        and bool(domain.get("required"))
        and str(domain.get("status") or "") == "missing"
    )


def _low_value_subagent_count(blackboard: dict[str, Any]) -> int:
    value_summary = blackboard.get("value_summary") if isinstance(blackboard.get("value_summary"), dict) else {}
    return (
        _int(value_summary.get("duplicate_or_overlap"))
        + _int(value_summary.get("confidence_only"))
        + _int(value_summary.get("cancelled_before_merge"))
    )


def _projection_subagents(projection: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    tasks = projection.get("tasks")
    if not isinstance(tasks, list):
        return result
    for task in tasks:
        if not isinstance(task, dict):
            continue
        subagents = task.get("subagents")
        if not isinstance(subagents, list):
            continue
        result.extend(item for item in subagents if isinstance(item, dict))
    return result


def _normalized_locks(locks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in locks:
        if not isinstance(row, dict):
            continue
        result.append(
            {
                "worker_id": _worker_for_lock(row),
                "run": _run_key(str(row.get("run") or "")),
                "task_id": str(row.get("task") or row.get("task_id") or row.get("id") or ""),
                "status": str(row.get("status") or ""),
            }
        )
    return result


def _status_summary_markdown(observation: dict[str, Any]) -> str:
    summary = observation.get("summary") if isinstance(observation.get("summary"), dict) else {}
    lines = [
        "# Orchestrator Loop Status",
        "",
        f"- generated_at: {observation.get('generated_at') or '-'}",
        f"- root: {observation.get('root') or '-'}",
        f"- actuation_enabled: {str(bool(observation.get('actuation_enabled'))).lower()}",
        f"- active_lock_count: {_int(summary.get('active_lock_count'))}",
        f"- recommendation_count: {_int(summary.get('recommendation_count'))}",
        "",
        "This file is a low-risk advisory artifact only. It does not execute control-plane actions.",
        "",
    ]
    return "\n".join(lines)


def _archive_candidates(observation: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    workers = observation.get("workers") if isinstance(observation.get("workers"), dict) else {}
    for worker_id, worker in workers.items():
        if not isinstance(worker, dict):
            continue
        classification = str(worker.get("classification") or "")
        if classification in TERMINAL_STATUSES and _int(worker.get("active_lock_count")) == 0:
            candidates.append(
                {
                    "worker_id": str(worker_id),
                    "classification": classification,
                    "reason": "terminal worker observation with no active lock",
                    "archive_action": "candidate_only_no_runtime_mutation",
                }
            )
    return {
        "schema_version": 1,
        "artifact_kind": "orchestrator_loop_archive_candidates",
        "generated_at": _utc_now(),
        "candidates": candidates,
    }


def _subagent_warning_count(projection: dict[str, Any]) -> int:
    summary = projection.get("summary") if isinstance(projection.get("summary"), dict) else {}
    count = _int(summary.get("warning_count"))
    if count:
        return count
    total = 0
    tasks = projection.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict):
                total += len(task.get("warnings") or [])
    return total


def _task_exists(tasks: list[dict[str, Any]], run: str, task_id: str) -> bool:
    return any(_run_key(str(task.get("run") or "")) == run and str(task.get("id") or task.get("task_id") or "") == task_id for task in tasks)


def _find_task(tasks: list[dict[str, Any]], run: str, task_id: str) -> dict[str, Any] | None:
    for task in tasks:
        if _run_key(str(task.get("run") or "")) == run and str(task.get("id") or task.get("task_id") or "") == task_id:
            return task
    return None


def _is_terminal_task(task: dict[str, Any]) -> bool:
    status = str(task.get("status") or "").lower()
    if status in TERMINAL_STATUSES:
        return True
    terminal_markers = task.get("terminal_markers")
    return isinstance(terminal_markers, list) and bool(terminal_markers)


def _recommendation_categories(recommendations: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in recommendations:
        category = str(item.get("category") or "uncategorized")
        result[category] = result.get(category, 0) + 1
    return dict(sorted(result.items()))


def _plan_summary(observation: dict[str, Any]) -> dict[str, Any]:
    recommendations = observation.get("recommendations")
    rows = [row for row in recommendations if isinstance(row, dict)] if isinstance(recommendations, list) else []
    return {
        "recommendation_count": len(rows),
        "recommendation_categories": _recommendation_categories(rows),
    }


def _signals(task: dict[str, Any]) -> set[str]:
    signals = task.get("signals")
    if not isinstance(signals, list):
        return set()
    return {str(item).strip().lower() for item in signals if str(item).strip()}


def _worker_for_task(task: dict[str, Any]) -> str:
    return str(task.get("owner") or task.get("worker") or task.get("worker_id") or "")


def _worker_for_lock(lock: dict[str, Any]) -> str:
    return str(lock.get("worker_id") or lock.get("worker") or lock.get("owner") or "")


def _run_key(value: str) -> str:
    return str(value or "").rstrip("/").split("/")[-1]


def _execution_budget_status(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    direct = value.get("execution_budget_status")
    if isinstance(direct, dict):
        return direct
    state_payload = value.get("state")
    if isinstance(state_payload, dict) and isinstance(
        state_payload.get("execution_budget_status"), dict
    ):
        return state_payload["execution_budget_status"]
    return {}


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        item = str(value or "")
        if not item or item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
