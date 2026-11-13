from __future__ import annotations

from typing import Any

from visible_agent_orchestrator.blackboard_contract import (
    BlackboardContractError,
    normalize_blackboard_payload,
    resolve_rollout_mode,
)


SCHEMA_VERSION = 1


class PullSchedulerGateError(Exception):
    """Raised when a live scheduler path is requested without its explicit gate."""


def build_pull_dry_run_plan(
    blackboard: dict[str, Any],
    *,
    subagent_projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_blackboard = normalize_blackboard_payload(blackboard)
    decisions: list[dict[str, Any]] = []
    decisions.extend(_closeout_decisions(subagent_projection or {}))
    decisions.extend(_blackboard_value_decisions(normalized_blackboard))
    decisions.extend(_efficiency_decisions(subagent_projection or {}))
    decisions.extend(_domain_dispatch_decisions(normalized_blackboard))
    projection = subagent_projection or {}
    next_actions = _domain_next_actions(normalized_blackboard)
    next_actions.extend(_projection_next_actions(projection))
    next_action_summary = _next_action_summary(next_actions)
    if not decisions:
        decisions.append(
            {
                "action": "no_dispatch",
                "reason": "no_missing_required_evidence_domains",
                "actuation_enabled": False,
                "would_execute": False,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "scheduler_kind": "pull_scheduler_dry_run",
        "mode": "pull_dry_run",
        "actuation_enabled": False,
        "would_dispatch": any(row.get("action") == "dispatch_domain_owner" for row in decisions),
        "rollback_mode": "legacy_push",
        "blackboard_mode": normalized_blackboard["mode"],
        "decisions": decisions,
        "next_actions": next_actions,
        "next_action_summary": next_action_summary,
        "answer": _pull_scheduler_answer(
            normalized_blackboard,
            next_actions=next_actions,
            next_action_summary=next_action_summary,
        ),
    }


def build_pull_live_plan(
    blackboard: dict[str, Any],
    *,
    subagent_projection: dict[str, Any] | None = None,
    allow_live: bool = False,
) -> dict[str, Any]:
    try:
        resolve_rollout_mode("pull_live", allow_live=allow_live)
    except BlackboardContractError as exc:
        raise PullSchedulerGateError(str(exc)) from exc
    dry_run = build_pull_dry_run_plan(blackboard, subagent_projection=subagent_projection)
    actions = [_live_action_envelope(row) for row in dry_run["decisions"] if row.get("action") == "dispatch_domain_owner"]
    return {
        "schema_version": SCHEMA_VERSION,
        "scheduler_kind": "pull_scheduler_live_plan",
        "mode": "pull_live",
        "actuation_enabled": True,
        "executed": False,
        "requires_explicit_opt_in": True,
        "rollback_mode": "legacy_push",
        "fallback_on_failure": "legacy_push",
        "terminal_truth": "report.md plus exactly one terminal marker",
        "source_scheduler_kind": dry_run["scheduler_kind"],
        "decision_count": len(dry_run["decisions"]),
        "action_count": len(actions),
        "decisions": list(dry_run["decisions"]),
        "actions": actions,
    }


def build_pull_live_executor_plan(
    action: dict[str, Any],
    *,
    target_root: str,
    worker_preflight: dict[str, Any] | None = None,
    locks: list[dict[str, Any]] | None = None,
    allow_real: bool = False,
) -> dict[str, Any]:
    target_root_value = str(target_root or "").strip()
    worker_id = str(action.get("worker_id") or "").strip()
    evidence_domain = str(action.get("evidence_domain") or "").strip()
    missing = list(action.get("missing") or []) if isinstance(action.get("missing"), list) else []
    blocked_reasons: list[str] = []

    if not allow_real:
        blocked_reasons.append("pull_live_executor_requires_allow_real")
    blocked_reasons.extend(_action_gate_reasons(action))
    if worker_preflight is None:
        blocked_reasons.append("fresh_worker_preflight_required")
    else:
        blocked_reasons.extend(
            _worker_preflight_gate_reasons(
                worker_preflight,
                target_root=target_root_value,
                worker_id=worker_id,
            )
        )
    if locks is None:
        blocked_reasons.append("fresh_locks_required")
    elif _has_active_worker_lock(locks, worker_id=worker_id):
        blocked_reasons.append("active_worker_lock_present")

    execution_allowed = not blocked_reasons
    body = _pull_live_body(evidence_domain=evidence_domain, missing=missing)
    command = [
        "omx_visible_dispatch",
        "run-one",
        "--root",
        target_root_value,
        "--worker",
        worker_id,
        "--title",
        f"pull-live evidence domain {evidence_domain}",
        "--body",
        body,
        "--timeout",
        "600",
        "--session-action",
        "keep",
        "--session-action-reason",
        "same_domain_continuation",
        "--allow-real",
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "executor_kind": "pull_live_executor_plan",
        "mode": "pull_live_executor",
        "actuation_enabled": False,
        "requires_explicit_opt_in": True,
        "execution_allowed": execution_allowed,
        "would_execute_control_plane": execution_allowed,
        "executed": False,
        "blocked_reasons": blocked_reasons,
        "worker_id": worker_id,
        "evidence_domain": evidence_domain,
        "target_root": target_root_value,
        "control_plane": "omx_visible_dispatch",
        "command": command,
        "body": body,
        "raw_tmux_input_forbidden": True,
        "direct_tui_input_forbidden": True,
        "rollback_mode": "legacy_push",
        "fallback_on_failure": "legacy_push",
        "terminal_truth": "report.md plus exactly one terminal marker",
    }


def build_pull_live_rollback_plan(executor_plan: dict[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "rollback_kind": "pull_live_rollback_plan",
        "mode": "pull_live_rollback",
        "actuation_enabled": False,
        "executed": False,
        "reason": str(reason or "").strip(),
        "source_executor_kind": str(executor_plan.get("executor_kind") or ""),
        "source_execution_allowed": bool(executor_plan.get("execution_allowed")),
        "worker_id": str(executor_plan.get("worker_id") or ""),
        "evidence_domain": str(executor_plan.get("evidence_domain") or ""),
        "recommended_mode": "legacy_push",
        "fallback_on_failure": "legacy_push",
        "duplicate_dispatch_forbidden": True,
        "lock_release_forbidden_without_terminal_truth": True,
        "terminal_truth": str(executor_plan.get("terminal_truth") or "report.md plus exactly one terminal marker"),
        "next_step": "leader_review_or_legacy_push",
    }


def _domain_dispatch_decisions(blackboard: dict[str, Any]) -> list[dict[str, Any]]:
    domains = blackboard.get("evidence_domains") if isinstance(blackboard.get("evidence_domains"), dict) else {}
    decisions: list[dict[str, Any]] = []
    for domain_id, row in sorted(domains.items()):
        if not isinstance(row, dict):
            continue
        if not bool(row.get("required")) or str(row.get("status") or "") != "missing":
            continue
        decisions.append(
            {
                "action": "dispatch_domain_owner",
                "recommendation": "launch_missing_domain",
                "evidence_domain": str(row.get("evidence_domain") or domain_id),
                "worker_id": str(row.get("worker_id") or ""),
                "reason": "required_evidence_domain_missing",
                "missing": list(row.get("missing") or []) if isinstance(row.get("missing"), list) else [],
                "source_fields": [f"blackboard.evidence_domains.{domain_id}"],
                "actuation_enabled": False,
                "would_execute": False,
            }
        )
    return decisions


def _domain_next_actions(blackboard: dict[str, Any]) -> list[dict[str, Any]]:
    domains = blackboard.get("evidence_domains") if isinstance(blackboard.get("evidence_domains"), dict) else {}
    actions: list[dict[str, Any]] = []
    for domain_id, row in sorted(domains.items()):
        if not isinstance(row, dict):
            continue
        action = _domain_next_action(domain_id, row)
        if action:
            actions.append(action)
    return actions


def _domain_next_action(domain_id: str, row: dict[str, Any]) -> dict[str, Any]:
    evidence_domain = str(row.get("evidence_domain") or domain_id)
    availability_state = str(row.get("availability_state") or "")
    consumption_state = str(row.get("consumption_state") or "")
    required = bool(row.get("required"))
    state_tags = [str(item) for item in row.get("state_tags", []) if str(item or "")]
    base: dict[str, Any] = {
        "evidence_domain": evidence_domain,
        "state_tags": state_tags,
        "actuation_enabled": False,
        "would_execute": False,
    }
    worker_id = str(row.get("worker_id") or "").strip()
    if availability_state == "missing" and required:
        return {
            "action": "launch_missing_required_domain",
            **base,
            **({"worker_id": worker_id} if worker_id else {}),
            "reason": "required_domain_missing",
            "missing": list(row.get("missing") or []) if isinstance(row.get("missing"), list) else [],
        }
    if availability_state == "blocked" and required:
        return {
            "action": "block_required_domain",
            **base,
            "reason": "required_domain_blocked",
        }
    if availability_state == "running":
        return {
            "action": "wait_for_retrieval",
            **base,
            **({"worker_id": worker_id} if worker_id else {}),
            "reason": "domain_running",
        }
    if availability_state == "stale":
        return {
            "action": "wait_for_retrieval" if required else "skip_optional_lane",
            **base,
            **({"worker_id": worker_id} if worker_id else {}),
            "reason": "required_domain_stale" if required else "optional_domain_stale",
        }
    if availability_state == "present" and consumption_state != "consumed":
        return {
            "action": "merge_ready",
            **base,
            "reason": "domain_present_unconsumed",
        }
    return {}


def _projection_next_actions(projection: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for subagent in _projection_subagents(projection):
        subagent_id = str(subagent.get("subagent_id") or "")
        state = str(subagent.get("closeout_state") or "")
        recommended = str(subagent.get("recommended_closeout_action") or "")
        if recommended == "ignore_late_optional_lane":
            actions.append(
                {
                    "action": "cancel_late_optional_lane",
                    "subagent_id": subagent_id,
                    "reason": state,
                    "actuation_enabled": False,
                    "would_execute": False,
                }
            )
            if bool(subagent.get("direct_path_converged_first")) and str(subagent.get("required_or_optional") or "") == "optional":
                actions.append(
                    {
                        "action": "skip_optional_lane",
                        "subagent_id": subagent_id,
                        "reason": state or "direct_path_converged_first",
                        "actuation_enabled": False,
                        "would_execute": False,
                    }
                )
        elif recommended == "block_or_expire_required_lane":
            actions.append(
                {
                    "action": "block_required_lane",
                    "subagent_id": subagent_id,
                    "reason": state,
                    "actuation_enabled": False,
                    "would_execute": False,
                }
            )
    return actions


def _next_action_summary(next_actions: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for row in next_actions:
        action = str(row.get("action") or "")
        if action:
            summary[action] = summary.get(action, 0) + 1
    return dict(sorted(summary.items()))


def _pull_scheduler_answer(
    blackboard: dict[str, Any],
    *,
    next_actions: list[dict[str, Any]],
    next_action_summary: dict[str, int],
) -> dict[str, Any]:
    primary = _primary_next_action(next_actions)
    if primary:
        primary_action = str(primary.get("action") or "")
        primary_reason = str(primary.get("reason") or "")
    else:
        primary_action = "no_action_required"
        primary_reason = "no_next_actions"
    answer: dict[str, Any] = {
        "answer_kind": "pull_scheduler_answer",
        "status": _answer_status(primary_action),
        "primary_action": primary_action,
        "primary_reason": primary_reason,
        "actionable": False,
        "actuation_enabled": False,
        "would_execute": False,
        "safe_next_actions": _safe_next_actions_for(next_actions),
        "forbidden_next_actions": _forbidden_next_actions(),
        "evidence": {
            "next_action_count": len(next_actions),
            "next_action_summary": dict(next_action_summary),
            "domain_state_summary": dict(blackboard.get("domain_state_summary") or {}),
        },
    }
    evidence_domain = str(primary.get("evidence_domain") or "") if primary else ""
    if evidence_domain:
        answer["primary_evidence_domain"] = evidence_domain
    subagent_id = str(primary.get("subagent_id") or "") if primary else ""
    if subagent_id:
        answer["primary_subagent_id"] = subagent_id
    return answer


def _primary_next_action(next_actions: list[dict[str, Any]]) -> dict[str, Any]:
    priority = {
        "block_required_domain": 0,
        "block_required_lane": 1,
        "launch_missing_required_domain": 2,
        "wait_for_retrieval": 3,
        "merge_ready": 4,
        "cancel_late_optional_lane": 5,
        "skip_optional_lane": 6,
    }
    candidates = [row for row in next_actions if isinstance(row, dict)]
    if not candidates:
        return {}
    return min(candidates, key=lambda row: priority.get(str(row.get("action") or ""), 99))


def _answer_status(primary_action: str) -> str:
    if primary_action in {"block_required_domain", "block_required_lane"}:
        return "blocked"
    if primary_action == "launch_missing_required_domain":
        return "needs_evidence"
    if primary_action == "wait_for_retrieval":
        return "waiting"
    return "ready"


def _safe_next_actions_for(next_actions: list[dict[str, Any]]) -> list[str]:
    actions = {str(row.get("action") or "") for row in next_actions if isinstance(row, dict)}
    safe: list[str] = []
    if "block_required_domain" in actions:
        safe.append("resolve required blocked evidence domains before dispatching more workers")
    if "block_required_lane" in actions:
        safe.append("block or expire required late lanes before closeout")
    if "launch_missing_required_domain" in actions and not any(
        action in actions for action in {"block_required_domain", "block_required_lane"}
    ):
        safe.append("prepare missing required evidence domain before any live opt-in")
    if "cancel_late_optional_lane" in actions or "skip_optional_lane" in actions:
        safe.append("cancel or ignore late optional lanes after direct convergence")
    if "merge_ready" in actions:
        safe.append("merge ready evidence domains after direct validation")
    if "wait_for_retrieval" in actions:
        safe.append("wait for running required domains before closeout")
    if not safe:
        safe.append("continue direct verification or close out if terminal contract is satisfied")
    return safe


def _forbidden_next_actions() -> list[str]:
    return [
        "do not dispatch live workers from pull_dry_run output",
        "do not cancel lanes from advisory-only output",
        "do not promote advisory ledger evidence to terminal truth",
    ]


def _action_gate_reasons(action: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if str(action.get("action") or "") != "dispatch_domain_owner":
        reasons.append("unsupported_pull_live_action")
    if not str(action.get("worker_id") or "").strip():
        reasons.append("action_worker_id_required")
    if not str(action.get("evidence_domain") or "").strip():
        reasons.append("action_evidence_domain_required")
    if action.get("sanctioned_control_plane_required") is not True:
        reasons.append("sanctioned_control_plane_required")
    if action.get("raw_tmux_input_forbidden") is not True:
        reasons.append("raw_tmux_input_must_be_forbidden")
    if action.get("executed") is True:
        reasons.append("already_executed_action_envelope")
    return reasons


def _worker_preflight_gate_reasons(
    worker_preflight: dict[str, Any],
    *,
    target_root: str,
    worker_id: str,
) -> list[str]:
    reasons: list[str] = []
    if str(worker_preflight.get("preflight_kind") or "") != "worker_binding_preflight":
        reasons.append("worker_preflight_kind_invalid")
    if str(worker_preflight.get("source") or "") != "fresh_control_plane":
        reasons.append("worker_preflight_source_not_fresh_control_plane")
    if str(worker_preflight.get("target_root") or "") != target_root:
        reasons.append("worker_preflight_target_root_mismatch")
    if str(worker_preflight.get("worker_id") or "") != worker_id:
        reasons.append("worker_preflight_worker_mismatch")
    if worker_preflight.get("ok") is not True:
        reasons.append("worker_preflight_not_ok")
    if str(worker_preflight.get("binding_confidence") or "") != "strong":
        reasons.append("worker_preflight_binding_not_strong")
    if str(worker_preflight.get("runtime_backend") or "") != "tmux-reference":
        reasons.append("worker_preflight_runtime_backend_not_tmux_reference")
    if worker_preflight.get("dispatchable") is not True:
        reasons.append("worker_preflight_not_dispatchable")
    if int(worker_preflight.get("active_alert_count") or 0) > 0:
        reasons.append("worker_preflight_active_alerts_present")
    return reasons


def _has_active_worker_lock(locks: list[dict[str, Any]], *, worker_id: str) -> bool:
    terminal_statuses = {"done", "fail", "failed", "blocked", "completed", "terminal", "archived"}
    for row in locks:
        if not isinstance(row, dict):
            continue
        if str(row.get("worker_id") or "") != worker_id:
            continue
        status = str(row.get("status") or "").strip()
        if status not in terminal_statuses:
            return True
    return False


def _pull_live_body(*, evidence_domain: str, missing: list[Any]) -> str:
    missing_text = ", ".join(str(item) for item in missing) if missing else "(none)"
    return "\n".join(
        [
            "Pull live evidence-domain task.",
            f"evidence_domain: {evidence_domain}",
            f"missing: {missing_text}",
            "Return report.md plus exactly one terminal marker.",
            "Use only sanctioned control-plane behavior; do not use raw tmux input.",
        ]
    )


def _live_action_envelope(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "dispatch_domain_owner",
        "evidence_domain": str(decision.get("evidence_domain") or ""),
        "worker_id": str(decision.get("worker_id") or ""),
        "control_plane": "omx_visible_orchestrate",
        "sanctioned_control_plane_required": True,
        "raw_tmux_input_forbidden": True,
        "would_execute_live": True,
        "executed": False,
        "rollback_mode": "legacy_push",
        "missing": list(decision.get("missing") or []) if isinstance(decision.get("missing"), list) else [],
    }


def _closeout_decisions(projection: dict[str, Any]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for subagent in _projection_subagents(projection):
        state = str(subagent.get("closeout_state") or "")
        action = str(subagent.get("recommended_closeout_action") or "")
        subagent_id = str(subagent.get("subagent_id") or "")
        if action == "ignore_late_optional_lane":
            decisions.append(
                {
                    "action": "cancel_late_optional_lane",
                    "recommendation": "cancel_late_optional_lane",
                    "subagent_id": subagent_id,
                    "reason": state,
                    "source_fields": ["opencode_subagent_projection.tasks[].subagents[].closeout_state"],
                    "actuation_enabled": False,
                    "would_execute": False,
                }
            )
            if bool(subagent.get("direct_path_converged_first")) and str(subagent.get("required_or_optional") or "") == "optional":
                decisions.append(
                    {
                        "action": "skip_optional_lane",
                        "recommendation": "skip_optional_lane",
                        "subagent_id": subagent_id,
                        "reason": state or "direct_path_converged_first",
                        "source_fields": ["opencode_subagent_projection.value_summary.direct_path_converged_first"],
                        "actuation_enabled": False,
                        "would_execute": False,
                    }
                )
        elif action == "block_or_expire_required_lane":
            decisions.append(
                {
                    "action": "block_or_expire_required_lane",
                    "recommendation": "block_required_lane",
                    "subagent_id": subagent_id,
                    "reason": state,
                    "source_fields": ["opencode_subagent_projection.tasks[].subagents[].closeout_state"],
                    "actuation_enabled": False,
                    "would_execute": False,
                }
            )
    return decisions


def _blackboard_value_decisions(blackboard: dict[str, Any]) -> list[dict[str, Any]]:
    value_summary = blackboard.get("value_summary") if isinstance(blackboard.get("value_summary"), dict) else {}
    direct_count = int(value_summary.get("direct_path_converged_first") or 0)
    if not direct_count:
        return []
    return [
        {
            "action": "direct_tools_first",
            "recommendation": "direct_tools_first",
            "reason": "direct_path_converged_first",
            "count": direct_count,
            "source_fields": ["blackboard.value_summary.direct_path_converged_first"],
            "actuation_enabled": False,
            "would_execute": False,
        }
    ]


def _efficiency_decisions(projection: dict[str, Any]) -> list[dict[str, Any]]:
    summary = projection.get("efficiency_summary") if isinstance(projection.get("efficiency_summary"), dict) else {}
    duplicate_lifecycle_rows = int(summary.get("duplicate_lifecycle_rows") or 0)
    duplicate_terminal_rows = int(summary.get("duplicate_terminal_rows") or 0)
    if not (duplicate_lifecycle_rows or duplicate_terminal_rows):
        return []
    return [
        {
            "action": "dedupe_lifecycle_noise",
            "recommendation": "dedupe_lifecycle_noise",
            "reason": "duplicate_lifecycle_or_terminal_rows",
            "duplicate_lifecycle_rows": duplicate_lifecycle_rows,
            "duplicate_terminal_rows": duplicate_terminal_rows,
            "source_fields": [
                "opencode_subagent_projection.efficiency_summary.duplicate_lifecycle_rows",
                "opencode_subagent_projection.efficiency_summary.duplicate_terminal_rows",
            ],
            "actuation_enabled": False,
            "would_execute": False,
        }
    ]


def _projection_subagents(projection: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    tasks = projection.get("tasks") if isinstance(projection.get("tasks"), list) else []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        subagents = task.get("subagents") if isinstance(task.get("subagents"), list) else []
        result.extend(row for row in subagents if isinstance(row, dict))
    return result
