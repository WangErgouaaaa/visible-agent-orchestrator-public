from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.subagent_display import (
    CompactProjectionOptions,
    compact_metadata,
    render_compact_projection,
)


LEDGER_JSONL = "subagent_ledger.jsonl"
LEDGER_JSON = "subagent_ledger.json"
VALID_SUBAGENT_STATUSES = {
    "created",
    "launched",
    "running",
    "waiting_input",
    "blocked",
    "failed",
    "completed",
    "unknown",
}
SUBAGENT_STATUS_RANK = {
    "": 0,
    "unknown": 0,
    "created": 10,
    "launched": 20,
    "running": 30,
    "waiting_input": 30,
    "blocked": 100,
    "failed": 100,
    "completed": 100,
}
VALID_SEMANTIC_STATUSES = {"", "passed", "failed", "blocked"}
VALID_RETRY_SCOPES = {"", "none", "lane_only", "full_run"}
VALID_REQUIRED_OR_OPTIONAL = {"", "required", "optional"}
VALID_LATEST_USEFUL_MOMENTS = {
    "",
    "before_parent_report",
    "before_report_freeze",
    "before_summarizer",
    "advisory_only",
}
VALID_RETRIEVAL_STATES = {
    "",
    "not_requested",
    "retrieval_ready",
    "completed_but_indexing",
    "retrieval_failed",
    "unretrieved_before_freeze",
    "expired_with_session_fallback",
    "unknown_task_id",
    "retrieval_not_ready_or_unknown",
    "retrieval_not_ready_or_unknown_then_ready",
}
VALID_MERGE_READINESS = {
    "",
    "merge_ready_exact_contract",
    "summary_only",
    "requires_parent_rewrite",
    "requires_direct_verification",
    "contract_risky",
    "merged",
    "not_merged",
}
VALID_SOURCE_ROOT_KINDS = {"", "source_repo", "installed_live", "fixture", "unknown"}
VALID_VALUE_CLASSIFICATIONS = {
    "",
    "unique_value_realized",
    "useful_payload_returned",
    "confidence_only",
    "duplicate_or_overlap",
    "not_retrieved",
    "retrieved_but_not_merged",
    "cancelled_before_merge",
    "direct_path_converged_first",
}
VALID_MERGE_STATUSES = {
    "",
    "merged",
    "merged_into_report",
    "retrieved_but_not_merged",
    "not_retrieved",
    "cancelled",
}
VALID_SOURCE_LABELS = {
    "",
    "direct_tool_verified",
    "subagent_reported",
    "quoted_file_evidence",
    "derived_from_multiple_files",
    "inference",
    "generic_guidance",
    "not_verified_in_current_run",
}
VALID_PARENT_ACTIONS_ON_DEGRADATION = {
    "",
    "blocked",
    "human_review_required",
    "direct_evidence_substituted",
    "confidence_downgraded_noncritical",
}
VALID_HEPHAESTUS_DRAFT_ADOPTIONS = {
    "",
    "adopted",
    "partially_adopted",
    "rejected",
    "not_applicable",
}
VALID_MOMUS_CONTEXT_ISOLATION_STATUSES = {
    "",
    "fresh_session",
    "explicit_no_prior_context",
    "not_used",
    "unknown",
    "shared_context_risk",
}
VALID_BACKGROUND_EXPIRY_STATUSES = {
    "",
    "not_background",
    "usable_before_freeze",
    "late_after_freeze",
    "expired",
    "unknown",
}
VALUE_SUMMARY_KEYS = (
    "launched",
    "retrieved",
    "merged",
    "unique_value_realized",
    "confidence_only",
    "duplicate_or_overlap",
    "not_retrieved",
    "retrieved_but_not_merged",
    "cancelled_before_merge",
    "direct_path_converged_first",
)
COST_VALUE_SUMMARY_KEYS = (
    "lane_count",
    "token_cost_lane_count",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "consumed_unique_value",
    "confidence_only",
    "duplicate_or_overlap",
    "direct_path_converged_first",
    "retrieval_failure",
    "expired_or_cancelled",
    "retrieved_not_consumed",
    "duration_ms_lane_count",
    "duration_ms_total",
    "unknown",
)
EFFICIENCY_SUMMARY_KEYS = (
    "ledger_rows",
    "deduped_subagents",
    "duplicate_lifecycle_rows",
    "duplicate_terminal_rows",
    "launched",
    "terminal",
    "retrieved",
    "retrieval_ready",
    "retrieval_not_ready",
    "retrieval_failed",
    "merged",
    "retrieved_but_not_merged",
    "not_merged",
    "optional",
    "required",
    "direct_path_converged_first",
    "artifact_summary_present",
    "artifact_path_present",
    "legacy_artifact_used",
    "source_label_present",
    "adopted_findings",
    "rejected_findings",
    "critical_rejected_findings",
    "subagent_degraded",
    "freshness_metadata_present",
)
DISPLAY_VALUE_KEYS = (
    "merged",
    "unique_value_realized",
    "confidence_only",
    "duplicate_or_overlap",
    "cancelled_before_merge",
    "retrieved_but_not_merged",
    "not_retrieved",
    "direct_path_converged_first",
)
FREEZE_UNAVAILABLE_RETRIEVAL_STATES = {
    "unretrieved_before_freeze",
    "completed_but_indexing",
    "retrieval_failed",
    "unknown_task_id",
    "retrieval_not_ready_or_unknown",
}
REPORT_FREEZE_MOMENTS = {"before_parent_report", "before_report_freeze"}
TERMINAL_SUBAGENT_STATUSES = {"blocked", "failed", "completed"}


def build_subagent_projection(
    root: Path | str,
    tasks: list[dict[str, Any]],
    *,
    active_task_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    projected_tasks: list[dict[str, Any]] = []
    status_summary: Counter[str] = Counter()
    value_summary: Counter[str] = Counter({key: 0 for key in VALUE_SUMMARY_KEYS})
    cost_value_summary: Counter[str] = Counter({key: 0 for key in COST_VALUE_SUMMARY_KEYS})
    closeout_summary: Counter[str] = Counter()
    efficiency_summary: Counter[str] = Counter({key: 0 for key in EFFICIENCY_SUMMARY_KEYS})

    for task in tasks:
        if not _is_opencode_task(task):
            continue
        ledger_missing = _ledger_path(_task_dir(root_path, task)) is None
        if _is_terminal_task(task) and ledger_missing:
            continue
        if active_task_keys is not None and ledger_missing and _task_key(task) not in active_task_keys:
            continue
        projected = _project_task(root_path, task)
        projected_tasks.append(projected)
        _accumulate_efficiency_summary(efficiency_summary, projected.get("efficiency_summary", {}))
        for subagent in projected["subagents"]:
            status_summary[str(subagent.get("status") or "unknown")] += 1
            _accumulate_value_summary(value_summary, subagent)
            _accumulate_cost_value_summary(cost_value_summary, subagent)
            _accumulate_closeout_summary(closeout_summary, subagent)

    warning_count = sum(len(task.get("warnings", [])) for task in projected_tasks)
    ledger_present_tasks = sum(1 for task in projected_tasks if task.get("ledger_status") == "present")
    subagent_count = sum(len(task.get("subagents", [])) for task in projected_tasks)
    source = "task_sidecar" if ledger_present_tasks else "none"

    cost_value_payload = _cost_value_summary_payload(cost_value_summary)
    return {
        "schema_version": 1,
        "projection_kind": "opencode_subagent_projection",
        "source": source,
        "summary": {
            "task_count": len(projected_tasks),
            "ledger_present_tasks": ledger_present_tasks,
            "subagent_count": subagent_count,
            "warning_count": warning_count,
        },
        "status_summary": dict(sorted(status_summary.items())),
        "value_summary": {key: int(value_summary.get(key, 0)) for key in VALUE_SUMMARY_KEYS},
        "cost_value_summary": cost_value_payload,
        "value_feedback": build_value_feedback_summary(cost_value_payload),
        "efficiency_summary": _efficiency_summary_payload(efficiency_summary),
        "closeout_summary": dict(sorted(closeout_summary.items())),
        "tasks": projected_tasks,
    }


def format_subagent_projection_human(projection: dict[str, Any]) -> str:
    summary = projection.get("summary", {}) if isinstance(projection.get("summary"), dict) else {}
    task_count = int(summary.get("task_count", 0) or 0)
    ledger_present = int(summary.get("ledger_present_tasks", 0) or 0)
    subagent_count = int(summary.get("subagent_count", 0) or 0)
    warning_count = int(summary.get("warning_count", 0) or 0)
    status_summary = projection.get("status_summary", {}) if isinstance(projection.get("status_summary"), dict) else {}
    status_text = ", ".join(f"{key}={value}" for key, value in status_summary.items()) or "-"
    value_text = format_value_summary(projection)
    efficiency_text = format_efficiency_summary(projection)
    lines = [
        "OpenCode subagent projection:",
        f"  source: {projection.get('source') or 'none'}",
        f"  ledger_present_tasks: {ledger_present}/{task_count}",
        f"  subagents: {subagent_count}",
        f"  warnings: {warning_count}",
        f"  status_summary: {status_text}",
    ]
    if value_text:
        lines.append(f"  value_summary: {value_text}")
    if efficiency_text:
        lines.append(f"  efficiency_summary: {efficiency_text}")
    for task in projection.get("tasks", []):
        lines.append(
            "  - "
            f"{task.get('run') or '-'} {task.get('task_id') or '-'} "
            f"worker={task.get('parent_worker_id') or '-'} "
            f"ledger={task.get('ledger_status') or '-'} "
            f"subagents={len(task.get('subagents', []))}"
        )
        for warning in task.get("warnings", []):
            lines.append(f"    warning: {warning}")
        for subagent in task.get("subagents", []):
            metadata = compact_metadata(
                "retrieval",
                subagent.get("retrieval_state"),
                "merge",
                subagent.get("merge_readiness"),
            )
            lines.append(
                "    - "
                f"{subagent.get('subagent_id') or '-'} "
                f"agent={subagent.get('agent') or '-'} "
                f"lane={subagent.get('lane') or '-'} "
                f"status={subagent.get('status') or '-'} "
                f"artifact_exists={str(bool(subagent.get('artifact_exists'))).lower()}"
                f"{metadata}"
            )
            for warning in subagent.get("warnings", []):
                lines.append(f"      warning: {warning}")
    return "\n".join(lines)


def format_subagent_projection_panel(projection: dict[str, Any]) -> str:
    return render_compact_projection(
        projection,
        CompactProjectionOptions(
            title="OpenCode Subagent Panel",
            width=None,
            label_subagent_fields=True,
            include_metadata=True,
            row_layout=True,
            warning_prefix="  ! ",
            empty_ledger_present_message="(ledger present but empty)",
        ),
    )


def _project_task(root: Path, task: dict[str, Any]) -> dict[str, Any]:
    task_dir = _task_dir(root, task)
    ledger_path = _ledger_path(task_dir)
    parent_worker_id = str(task.get("owner") or "")
    run_id = str(task.get("run") or "")
    task_id = str(task.get("id") or task.get("task_id") or "")

    subagents: list[dict[str, Any]] = []
    warnings: list[str] = []
    entries: list[dict[str, Any]] = []
    ledger_status = "unknown"
    ledger_rel = ""
    if ledger_path is None:
        warnings.append("subagent ledger missing")
    else:
        ledger_status = "present"
        ledger_rel = _rel(ledger_path, root)
        entries, parse_warnings = _read_ledger_entries(ledger_path)
        warnings.extend(parse_warnings)
        for index, entry in enumerate(entries, start=1):
            projected = _project_subagent(task_dir, entry, index=index)
            subagents.append(projected)
        subagents = _dedupe_subagents(subagents)
        for projected in subagents:
            warnings.extend(_parent_warnings(projected, parent_worker_id, run_id, task_id))
            warnings.extend(projected.get("warnings", []))

    return {
        "run": run_id,
        "task_id": task_id,
        "parent_worker_id": parent_worker_id,
        "backend": str(task.get("backend") or ""),
        "task_status": str(task.get("status") or ""),
        "task_path": str(task.get("path") or ""),
        "ledger_status": ledger_status,
        "ledger_path": ledger_rel,
        "efficiency_summary": _task_efficiency_summary(entries, subagents),
        "subagents": subagents,
        "warnings": _dedupe(warnings),
    }


def _project_subagent(task_dir: Path, entry: dict[str, Any], *, index: int) -> dict[str, Any]:
    subagent_id = str(entry.get("subagent_id") or entry.get("id") or f"subagent-{index:03d}")
    agent_name = str(entry.get("agent") or "")
    raw_parent_run_id = str(entry.get("parent_run_id") or "")
    canonical_parent_run_id = _canonical_parent_run_id(raw_parent_run_id)
    artifact = str(entry.get("artifact") or "").strip()
    artifact_path = str(entry.get("artifact_path") or "").strip()
    raw_artifact_summary = entry.get("artifact_summary")
    artifact_summary = (
        dict(raw_artifact_summary)
        if isinstance(raw_artifact_summary, dict)
        else str(raw_artifact_summary or "").strip()
    )
    status = str(entry.get("status") or "unknown")
    semantic_status = str(entry.get("semantic_status") or "")
    retry_scope = str(entry.get("retry_scope") or "")
    required_or_optional = str(entry.get("required_or_optional") or "")
    latest_useful_moment = str(entry.get("latest_useful_moment") or "")
    retrieval_state = str(entry.get("retrieval_state") or "")
    merge_readiness = str(entry.get("merge_readiness") or "")
    value_classification = str(entry.get("value_classification") or "")
    merge_status = str(entry.get("merge_status") or "")
    source_label = str(entry.get("source_label") or "")
    parent_action_on_degradation = str(entry.get("parent_action_on_degradation") or "")
    hephaestus_draft_adoption = str(entry.get("hephaestus_draft_adoption") or "")
    momus_context_isolation_status = str(entry.get("momus_context_isolation_status") or "")
    background_expiry_status = str(entry.get("background_expiry_status") or "")
    source_identity = _source_identity(entry.get("source_identity"))
    launcher_compatibility = _launcher_compatibility(entry)
    warnings: list[str] = []
    artifact_exists = False
    artifact_present = False
    artifact_kind = ""
    if status not in VALID_SUBAGENT_STATUSES:
        warnings.append(f"subagent {subagent_id} unknown status: {status}")
    if semantic_status not in VALID_SEMANTIC_STATUSES:
        warnings.append(f"subagent {subagent_id} unknown semantic_status: {semantic_status}")
    if retry_scope not in VALID_RETRY_SCOPES:
        warnings.append(f"subagent {subagent_id} unknown retry_scope: {retry_scope}")
    if required_or_optional not in VALID_REQUIRED_OR_OPTIONAL:
        warnings.append(f"subagent {subagent_id} unknown required_or_optional: {required_or_optional}")
    if latest_useful_moment not in VALID_LATEST_USEFUL_MOMENTS:
        warnings.append(f"subagent {subagent_id} unknown latest_useful_moment: {latest_useful_moment}")
    if retrieval_state not in VALID_RETRIEVAL_STATES:
        warnings.append(f"subagent {subagent_id} unknown retrieval_state: {retrieval_state}")
    if merge_readiness not in VALID_MERGE_READINESS:
        warnings.append(f"subagent {subagent_id} unknown merge_readiness: {merge_readiness}")
    if value_classification not in VALID_VALUE_CLASSIFICATIONS:
        warnings.append(f"subagent {subagent_id} unknown value_classification: {value_classification}")
    if merge_status not in VALID_MERGE_STATUSES:
        warnings.append(f"subagent {subagent_id} unknown merge_status: {merge_status}")
    if source_label not in VALID_SOURCE_LABELS:
        warnings.append(f"subagent {subagent_id} unknown source_label: {source_label}")
    if parent_action_on_degradation not in VALID_PARENT_ACTIONS_ON_DEGRADATION:
        warnings.append(f"subagent {subagent_id} unknown parent_action_on_degradation: {parent_action_on_degradation}")
    if hephaestus_draft_adoption not in VALID_HEPHAESTUS_DRAFT_ADOPTIONS:
        warnings.append(f"subagent {subagent_id} unknown hephaestus_draft_adoption: {hephaestus_draft_adoption}")
    if agent_name == "hephaestus" and status == "completed":
        if not hephaestus_draft_adoption:
            warnings.append(f"subagent {subagent_id} completed hephaestus row lacks hephaestus_draft_adoption")
        if not str(entry.get("hephaestus_adoption_diff_or_summary") or "").strip():
            warnings.append(
                f"subagent {subagent_id} completed hephaestus row lacks hephaestus_adoption_diff_or_summary"
            )
    if momus_context_isolation_status not in VALID_MOMUS_CONTEXT_ISOLATION_STATUSES:
        warnings.append(f"subagent {subagent_id} unknown momus_context_isolation_status: {momus_context_isolation_status}")
    if background_expiry_status not in VALID_BACKGROUND_EXPIRY_STATUSES:
        warnings.append(f"subagent {subagent_id} unknown background_expiry_status: {background_expiry_status}")
    source_root_kind = str(source_identity.get("root_kind") or "")
    if source_root_kind not in VALID_SOURCE_ROOT_KINDS:
        warnings.append(f"subagent {subagent_id} unknown source_identity.root_kind: {source_root_kind}")
    adopted_findings = _string_list(entry.get("adopted_findings"))
    rejected_findings = _string_list(entry.get("rejected_findings"))
    critical_rejected_findings = _string_list(entry.get("critical_rejected_findings"))
    allowed_sources = _string_list(entry.get("allowed_sources"))
    forbidden_sources = _string_list(entry.get("forbidden_sources"))
    subagent_degraded = _bool(entry.get("subagent_degraded"))
    if critical_rejected_findings and not str(entry.get("rejected_finding_downstream_action") or "").strip():
        warnings.append(f"subagent {subagent_id} critical rejected findings lack downstream action")
    if subagent_degraded and not parent_action_on_degradation:
        warnings.append(f"subagent {subagent_id} degraded without parent_action_on_degradation")
    if agent_name == "librarian" and not (
        str(entry.get("librarian_freshness_source") or "").strip()
        and str(entry.get("librarian_freshness_checked_at") or "").strip()
        and str(entry.get("librarian_freshness_basis") or "").strip()
    ):
        warnings.append(f"subagent {subagent_id} librarian evidence lacks freshness metadata")
    artifact_ref = artifact_path or artifact
    if artifact_ref:
        artifact_exists = _artifact_path(task_dir, artifact_ref).is_file()
        artifact_present = artifact_exists or bool(artifact_summary)
        artifact_kind = "path" if artifact_path else "legacy_path"
        if not artifact_exists:
            if required_or_optional == "required":
                warnings.append(f"subagent {subagent_id} required artifact missing: {artifact_ref}")
            else:
                warnings.append(f"subagent {subagent_id} artifact missing: {artifact_ref}")
    elif artifact_summary:
        artifact_present = True
        artifact_kind = "inline_summary"
    if required_or_optional == "required" and retrieval_state == "unretrieved_before_freeze":
        warnings.append(f"subagent {subagent_id} required lane unretrieved before freeze")
    closeout = _closeout_lifecycle(
        required_or_optional=required_or_optional,
        latest_useful_moment=latest_useful_moment,
        retrieval_state=retrieval_state,
        merge_status=merge_status,
        merge_readiness=merge_readiness,
        value_classification=value_classification,
        direct_path_converged_first=_bool(entry.get("direct_path_converged_first")),
    )
    if closeout["closeout_state"] == "required_unavailable_before_freeze":
        warnings.append(f"subagent {subagent_id} required lane unavailable before freeze")
    cost_value_classification = _cost_value_classification(
        value_classification=value_classification,
        merge_status=merge_status,
        retrieval_state=retrieval_state,
        closeout_state=str(closeout["closeout_state"]),
        direct_path_converged_first=_bool(entry.get("direct_path_converged_first")),
    )
    prompt_tokens = _optional_nonnegative_int(entry.get("prompt_tokens"))
    completion_tokens = _optional_nonnegative_int(entry.get("completion_tokens"))
    total_tokens = _optional_nonnegative_int(entry.get("total_tokens"))
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    token_cost_source = str(entry.get("token_cost_source") or "").strip()
    duration_ms = _optional_nonnegative_int(entry.get("duration_ms"))
    duration_ms_source = str(entry.get("duration_ms_source") or "").strip()
    if duration_ms is None:
        duration_ms = _duration_ms_from_timestamps(
            str(entry.get("started_at") or ""),
            str(entry.get("completed_at") or ""),
        )
        if duration_ms is not None and not duration_ms_source:
            duration_ms_source = "started_completed_timestamps"
    return {
        "subagent_id": subagent_id,
        "parent_worker_id": str(entry.get("parent_worker_id") or ""),
        "parent_run_id": canonical_parent_run_id,
        "parent_run_id_raw": raw_parent_run_id if raw_parent_run_id and raw_parent_run_id != canonical_parent_run_id else "",
        "parent_task_id": str(entry.get("parent_task_id") or ""),
        "agent": agent_name,
        "lane": str(entry.get("lane") or ""),
        "purpose": str(entry.get("purpose") or ""),
        "visible_role": str(entry.get("visible_role") or ""),
        "evidence_domain": str(entry.get("evidence_domain") or ""),
        "allowed_sources": allowed_sources,
        "forbidden_sources": forbidden_sources,
        "status": status,
        "started_at": str(entry.get("started_at") or ""),
        "completed_at": str(entry.get("completed_at") or ""),
        "artifact": artifact,
        "artifact_path": artifact_path or artifact,
        "artifact_summary": artifact_summary,
        "artifact_kind": artifact_kind,
        "artifact_exists": artifact_exists,
        "artifact_present": artifact_present,
        "error": str(entry.get("error") or ""),
        "usable_content": str(entry.get("usable_content") or ""),
        "parse_error": str(entry.get("parse_error") or ""),
        "completion_state": str(entry.get("completion_state") or ""),
        "semantic_status": semantic_status,
        "retry_scope": retry_scope,
        "required_or_optional": required_or_optional,
        "latest_useful_moment": latest_useful_moment,
        "retrieval_state": retrieval_state,
        "merge_readiness": merge_readiness,
        "subdomain_id": str(entry.get("subdomain_id") or ""),
        "spawned_by_worker": str(entry.get("spawned_by_worker") or ""),
        "expected_unique_value": str(entry.get("expected_unique_value") or ""),
        "unique_value_realized": str(entry.get("unique_value_realized") or ""),
        "value_classification": value_classification,
        "merge_status": merge_status,
        "downstream_consumer": str(entry.get("downstream_consumer") or ""),
        "on_critical_path": _bool(entry.get("on_critical_path")),
        "direct_path_converged_first": _bool(entry.get("direct_path_converged_first")),
        "cancel_reason": str(entry.get("cancel_reason") or ""),
        "source_label": source_label,
        "adopted_findings": adopted_findings,
        "rejected_findings": rejected_findings,
        "critical_rejected_findings": critical_rejected_findings,
        "rejected_finding_reason": str(entry.get("rejected_finding_reason") or ""),
        "rejected_finding_direct_evidence_status": str(entry.get("rejected_finding_direct_evidence_status") or ""),
        "rejected_finding_downstream_action": str(entry.get("rejected_finding_downstream_action") or ""),
        "subagent_degraded": subagent_degraded,
        "degradation_reason": str(entry.get("degradation_reason") or ""),
        "confidence_effect": str(entry.get("confidence_effect") or ""),
        "parent_action_on_degradation": parent_action_on_degradation,
        "librarian_freshness_source": str(entry.get("librarian_freshness_source") or ""),
        "librarian_freshness_checked_at": str(entry.get("librarian_freshness_checked_at") or ""),
        "librarian_freshness_basis": str(entry.get("librarian_freshness_basis") or ""),
        "hephaestus_draft_adoption": hephaestus_draft_adoption,
        "hephaestus_adoption_diff_or_summary": str(entry.get("hephaestus_adoption_diff_or_summary") or ""),
        "momus_context_isolation_status": momus_context_isolation_status,
        "background_expiry_status": background_expiry_status,
        "late_result_handling": str(entry.get("late_result_handling") or ""),
        "source_identity": source_identity,
        "trace_session_id": str(entry.get("trace_session_id") or ""),
        "trace_task_id": str(entry.get("trace_task_id") or entry.get("task_id") or ""),
        "canonical_retrieval_handle": str(entry.get("canonical_retrieval_handle") or ""),
        "fallback_session_id": str(entry.get("fallback_session_id") or ""),
        "launcher_compatibility": launcher_compatibility,
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "token_cost_available": total_tokens is not None,
        "token_cost_source": token_cost_source or ("ledger_row" if total_tokens is not None else ""),
        "duration_ms": int(duration_ms or 0),
        "duration_ms_available": duration_ms is not None,
        "duration_ms_source": duration_ms_source,
        "closeout_state": closeout["closeout_state"],
        "final_merge_allowed": closeout["final_merge_allowed"],
        "recommended_closeout_action": closeout["recommended_closeout_action"],
        "cost_value_classification": cost_value_classification,
        "warnings": warnings,
    }


def _dedupe_subagents(subagents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    by_id: dict[str, int] = {}
    for subagent in subagents:
        subagent_id = str(subagent.get("subagent_id") or "")
        key = subagent_id or f"__row_{len(result)}"
        existing_index = by_id.get(key)
        if existing_index is None:
            by_id[key] = len(result)
            result.append(subagent)
            continue
        existing = result[existing_index]
        if _subagent_status_rank(subagent) >= _subagent_status_rank(existing):
            result[existing_index] = subagent
    return result


def _accumulate_value_summary(summary: Counter[str], subagent: dict[str, Any]) -> None:
    summary["launched"] += 1
    classification = str(subagent.get("value_classification") or "")
    merge_status = str(subagent.get("merge_status") or "")
    status = str(subagent.get("status") or "")
    if (
        merge_status in {"merged", "retrieved_but_not_merged"}
        or (status == "completed" and classification not in {"not_retrieved", "cancelled_before_merge"})
    ):
        summary["retrieved"] += 1
    if merge_status == "merged":
        summary["merged"] += 1
    if merge_status in {"retrieved_but_not_merged", "not_retrieved"}:
        summary[merge_status] += 1
    if classification in VALID_VALUE_CLASSIFICATIONS and classification:
        summary[classification] += 1
    if bool(subagent.get("direct_path_converged_first")):
        summary["direct_path_converged_first"] += 1


def _accumulate_closeout_summary(summary: Counter[str], subagent: dict[str, Any]) -> None:
    state = str(subagent.get("closeout_state") or "")
    if state:
        summary[state] += 1


def _accumulate_cost_value_summary(summary: Counter[str], subagent: dict[str, Any]) -> None:
    summary["lane_count"] += 1
    if bool(subagent.get("token_cost_available")):
        summary["token_cost_lane_count"] += 1
        summary["prompt_tokens"] += int(subagent.get("prompt_tokens", 0) or 0)
        summary["completion_tokens"] += int(subagent.get("completion_tokens", 0) or 0)
        summary["total_tokens"] += int(subagent.get("total_tokens", 0) or 0)
    if bool(subagent.get("duration_ms_available")):
        summary["duration_ms_lane_count"] += 1
        summary["duration_ms_total"] += int(subagent.get("duration_ms", 0) or 0)
    classification = str(subagent.get("cost_value_classification") or "unknown")
    if classification not in COST_VALUE_SUMMARY_KEYS:
        classification = "unknown"
    if classification != "lane_count":
        summary[classification] += 1


def _accumulate_efficiency_summary(summary: Counter[str], task_summary: dict[str, Any]) -> None:
    for key in EFFICIENCY_SUMMARY_KEYS:
        summary[key] += int(task_summary.get(key, 0) or 0)


def _cost_value_summary_payload(summary: Counter[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "token_cost_available": int(summary.get("token_cost_lane_count", 0) or 0) > 0,
        "token_cost_source": "projection_rows" if int(summary.get("token_cost_lane_count", 0) or 0) > 0 else "",
        "duration_ms_available": int(summary.get("duration_ms_lane_count", 0) or 0) > 0,
        "duration_ms_source": "projection_rows" if int(summary.get("duration_ms_lane_count", 0) or 0) > 0 else "",
        **{key: int(summary.get(key, 0)) for key in COST_VALUE_SUMMARY_KEYS},
    }


def build_value_feedback_summary(cost_value_summary: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "consumed_unique_value": int(cost_value_summary.get("consumed_unique_value", 0) or 0),
        "confidence_only": int(cost_value_summary.get("confidence_only", 0) or 0),
        "duplicate_or_overlap": int(cost_value_summary.get("duplicate_or_overlap", 0) or 0),
        "direct_path_converged_first": int(cost_value_summary.get("direct_path_converged_first", 0) or 0),
        "retrieval_failure": int(cost_value_summary.get("retrieval_failure", 0) or 0),
        "retrieved_not_consumed": int(cost_value_summary.get("retrieved_not_consumed", 0) or 0),
        "expired_or_cancelled": int(cost_value_summary.get("expired_or_cancelled", 0) or 0),
        "unknown": int(cost_value_summary.get("unknown", 0) or 0),
    }
    planner_feedback = {
        "reduce_duplicate_or_confidence_lanes": summary["confidence_only"] + summary["duplicate_or_overlap"],
        "investigate_retrieval_failures": summary["retrieval_failure"],
        "optional_lanes_direct_path_converged": summary["direct_path_converged_first"],
    }
    if not any(summary.values()):
        status = "no_subagent_value_observed"
    elif (
        planner_feedback["reduce_duplicate_or_confidence_lanes"]
        or planner_feedback["investigate_retrieval_failures"]
        or summary["retrieved_not_consumed"]
        or summary["unknown"]
    ):
        status = "needs_planner_tuning"
    elif summary["consumed_unique_value"]:
        status = "unique_value_realized"
    else:
        status = "advisory_only"
    return {
        "schema_version": 1,
        "artifact_kind": "value_feedback",
        "advisory_only": True,
        "merge_gate": False,
        "status": status,
        "summary": summary,
        "planner_feedback": planner_feedback,
    }


def _efficiency_summary_payload(summary: Counter[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        **{key: int(summary.get(key, 0)) for key in EFFICIENCY_SUMMARY_KEYS},
    }


def _task_efficiency_summary(entries: list[dict[str, Any]], subagents: list[dict[str, Any]]) -> dict[str, Any]:
    summary: Counter[str] = Counter({key: 0 for key in EFFICIENCY_SUMMARY_KEYS})
    summary["ledger_rows"] = len(entries)
    summary["deduped_subagents"] = len(subagents)
    summary["duplicate_lifecycle_rows"] = max(0, len(entries) - len(subagents))
    summary["duplicate_terminal_rows"] = _duplicate_terminal_rows(entries)
    for entry in entries:
        if str(entry.get("status") or "") == "launched":
            summary["launched"] += 1
    for subagent in subagents:
        status = str(subagent.get("status") or "")
        retrieval_state = str(subagent.get("retrieval_state") or "")
        merge_status = str(subagent.get("merge_status") or "")
        required_or_optional = str(subagent.get("required_or_optional") or "")
        artifact_kind = str(subagent.get("artifact_kind") or "")
        if status in TERMINAL_SUBAGENT_STATUSES:
            summary["terminal"] += 1
        if (
            merge_status in {"merged", "retrieved_but_not_merged"}
            or retrieval_state == "retrieval_ready"
        ):
            summary["retrieved"] += 1
        if retrieval_state == "retrieval_ready":
            summary["retrieval_ready"] += 1
        elif retrieval_state in FREEZE_UNAVAILABLE_RETRIEVAL_STATES - {"retrieval_failed"}:
            summary["retrieval_not_ready"] += 1
        elif retrieval_state == "retrieval_failed":
            summary["retrieval_failed"] += 1
        if merge_status == "merged":
            summary["merged"] += 1
        elif merge_status == "retrieved_but_not_merged":
            summary["retrieved_but_not_merged"] += 1
        elif merge_status == "not_retrieved":
            summary["not_merged"] += 1
        if required_or_optional == "optional":
            summary["optional"] += 1
        elif required_or_optional == "required":
            summary["required"] += 1
        if bool(subagent.get("direct_path_converged_first")):
            summary["direct_path_converged_first"] += 1
        if bool(subagent.get("artifact_summary")):
            summary["artifact_summary_present"] += 1
        if artifact_kind == "path":
            summary["artifact_path_present"] += 1
        elif artifact_kind == "legacy_path":
            summary["legacy_artifact_used"] += 1
        if str(subagent.get("source_label") or "").strip():
            summary["source_label_present"] += 1
        summary["adopted_findings"] += len(_string_list(subagent.get("adopted_findings")))
        summary["rejected_findings"] += len(_string_list(subagent.get("rejected_findings")))
        summary["critical_rejected_findings"] += len(_string_list(subagent.get("critical_rejected_findings")))
        if bool(subagent.get("subagent_degraded")):
            summary["subagent_degraded"] += 1
        if (
            str(subagent.get("librarian_freshness_source") or "").strip()
            or str(subagent.get("librarian_freshness_checked_at") or "").strip()
            or str(subagent.get("librarian_freshness_basis") or "").strip()
        ):
            summary["freshness_metadata_present"] += 1
    return _efficiency_summary_payload(summary)


def _duplicate_terminal_rows(entries: list[dict[str, Any]]) -> int:
    terminal_counts: Counter[str] = Counter()
    for index, entry in enumerate(entries, start=1):
        if str(entry.get("status") or "") not in TERMINAL_SUBAGENT_STATUSES:
            continue
        terminal_counts[_entry_subagent_key(entry, index)] += 1
    return sum(max(0, count - 1) for count in terminal_counts.values())


def _entry_subagent_key(entry: dict[str, Any], index: int) -> str:
    return str(entry.get("subagent_id") or entry.get("id") or f"subagent-{index:03d}")


def _cost_value_classification(
    *,
    value_classification: str,
    merge_status: str,
    retrieval_state: str,
    closeout_state: str,
    direct_path_converged_first: bool,
) -> str:
    if direct_path_converged_first or value_classification == "direct_path_converged_first":
        return "direct_path_converged_first"
    if value_classification == "confidence_only":
        return "confidence_only"
    if value_classification == "duplicate_or_overlap":
        return "duplicate_or_overlap"
    if (
        value_classification == "not_retrieved"
        or merge_status == "not_retrieved"
        or retrieval_state in {"retrieval_failed", "unretrieved_before_freeze", "unknown_task_id"}
    ):
        return "retrieval_failure"
    if (
        value_classification == "cancelled_before_merge"
        or merge_status == "cancelled"
        or closeout_state in {"cancelled_before_merge", "optional_expired_after_freeze", "required_unavailable_before_freeze"}
    ):
        return "expired_or_cancelled"
    if value_classification == "unique_value_realized" and merge_status == "merged":
        return "consumed_unique_value"
    if merge_status == "retrieved_but_not_merged":
        return "retrieved_not_consumed"
    return "unknown"


def _closeout_lifecycle(
    *,
    required_or_optional: str,
    latest_useful_moment: str,
    retrieval_state: str,
    merge_status: str,
    merge_readiness: str,
    value_classification: str,
    direct_path_converged_first: bool,
) -> dict[str, Any]:
    if merge_status == "merged":
        return _closeout("merged_before_freeze", True, "merged")
    if direct_path_converged_first or value_classification == "direct_path_converged_first":
        return _closeout("direct_path_converged_first", False, "ignore_late_optional_lane")
    if merge_status == "cancelled" or value_classification == "cancelled_before_merge":
        return _closeout("cancelled_before_merge", False, "record_cancelled_lane")
    if latest_useful_moment in REPORT_FREEZE_MOMENTS and retrieval_state in FREEZE_UNAVAILABLE_RETRIEVAL_STATES:
        if required_or_optional == "required":
            return _closeout("required_unavailable_before_freeze", False, "block_or_expire_required_lane")
        return _closeout("optional_expired_after_freeze", False, "record_telemetry_only")
    if retrieval_state in {"completed_but_indexing", "unknown_task_id", "retrieval_not_ready_or_unknown"}:
        return _closeout("retrieval_ambiguous_before_freeze", False, "retry_once_or_record_unknown")
    if retrieval_state == "retrieval_ready" or merge_readiness == "merge_ready_exact_contract":
        return _closeout("merge_ready_before_freeze", True, "leader_may_merge_if_still_before_freeze")
    return _closeout("", False, "")


def _closeout(state: str, final_merge_allowed: bool, action: str) -> dict[str, Any]:
    return {
        "closeout_state": state,
        "final_merge_allowed": bool(final_merge_allowed),
        "recommended_closeout_action": action,
    }


def format_value_summary(projection: dict[str, Any]) -> str:
    summary = projection.get("value_summary") if isinstance(projection.get("value_summary"), dict) else {}
    chunks = []
    for key in DISPLAY_VALUE_KEYS:
        count = int(summary.get(key, 0) or 0)
        if count:
            chunks.append(f"{key}={count}")
    return ", ".join(chunks)


def format_efficiency_summary(projection: dict[str, Any]) -> str:
    summary = projection.get("efficiency_summary") if isinstance(projection.get("efficiency_summary"), dict) else {}
    chunks = []
    for key in (
        "launched",
        "retrieved",
        "merged",
        "duplicate_terminal_rows",
        "terminal",
        "retrieved_but_not_merged",
        "not_merged",
        "direct_path_converged_first",
        "adopted_findings",
        "rejected_findings",
        "critical_rejected_findings",
    ):
        count = int(summary.get(key, 0) or 0)
        if count:
            chunks.append(f"{key}={count}")
    return ", ".join(chunks)


def _subagent_status_rank(subagent: dict[str, Any]) -> int:
    status = str(subagent.get("status") or "")
    return SUBAGENT_STATUS_RANK.get(status, 0)


def _parent_warnings(subagent: dict[str, Any], worker_id: str, run_id: str, task_id: str) -> list[str]:
    subagent_id = str(subagent.get("subagent_id") or "-")
    checks = (
        ("parent_worker_id", worker_id),
        ("parent_run_id", run_id),
        ("parent_task_id", task_id),
    )
    warnings: list[str] = []
    for key, expected in checks:
        actual = str(subagent.get(key) or "")
        if actual and expected and not _parent_value_matches(key, actual, expected):
            warnings.append(f"subagent {subagent_id} {key} mismatch: {actual} != {expected}")
    return warnings


def _parent_value_matches(key: str, actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    if key == "parent_run_id":
        return _normalize_parent_run_id(actual) == _normalize_parent_run_id(expected)
    return False


def _normalize_parent_run_id(value: str) -> str:
    return str(value or "").strip().rstrip("/").split("/")[-1]


def _canonical_parent_run_id(value: str) -> str:
    bare = _normalize_parent_run_id(value)
    return f"visible-runs/{bare}" if bare else ""


def _read_ledger_entries(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        if path.name == LEDGER_JSONL:
            entries, warnings = _read_jsonl_entries(path)
            if not entries and not warnings:
                warnings.append("subagent ledger contains no subagent objects")
            return entries, warnings
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [], [f"subagent ledger parse error: {exc}"]
    if isinstance(data, list):
        entries = [item for item in data if isinstance(item, dict)]
        return _entries_or_empty_warning(entries)
    if isinstance(data, dict):
        rows = data.get("subagents", data.get("entries", []))
        if isinstance(rows, list):
            entries = [item for item in rows if isinstance(item, dict)]
            return _entries_or_empty_warning(entries)
    return [], ["subagent ledger contains no subagent objects"]


def _entries_or_empty_warning(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    if entries:
        return entries, []
    return [], ["subagent ledger contains no subagent objects"]


def _read_jsonl_entries(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"subagent ledger JSONL line {line_number} parse error: {exc}")
            continue
        if isinstance(data, dict):
            entries.append(data)
    return entries, warnings


def _ledger_path(task_dir: Path) -> Path | None:
    for name in (LEDGER_JSONL, LEDGER_JSON):
        path = task_dir / name
        if path.is_file():
            return path
    return None


def _task_dir(root: Path, task: dict[str, Any]) -> Path:
    task_path = Path(str(task.get("path") or ""))
    if not task_path.is_absolute():
        task_path = root / task_path
    return task_path.resolve()


def _artifact_path(task_dir: Path, artifact: str) -> Path:
    path = Path(artifact)
    if not path.is_absolute():
        path = task_dir / path
    return path.resolve()


def _source_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    keys = ("root_kind", "observed_at", "path", "mtime", "sha256")
    return {key: str(value.get(key) or "") for key in keys if str(value.get(key) or "")}


def _launcher_compatibility(entry: dict[str, Any]) -> dict[str, Any]:
    requested = str(entry.get("requested_launcher_contract") or "")
    actual = str(entry.get("actual_launcher_used") or "")
    missing = _list_str(entry.get("missing_or_unexpressed_fields"))
    mitigations = _list_str(entry.get("mitigation_steps_taken"))
    audit_status = str(entry.get("audit_status") or "")
    if not any((requested, actual, missing, mitigations, audit_status)):
        return {}
    return {
        "requested_launcher_contract": requested,
        "actual_launcher_used": actual,
        "missing_or_unexpressed_fields": missing,
        "mitigation_steps_taken": mitigations,
        "audit_status": audit_status,
    }


def _list_str(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _optional_nonnegative_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    if normalized < 0:
        return None
    return normalized


def _duration_ms_from_timestamps(started_at: str, completed_at: str) -> int | None:
    start = _parse_iso8601(started_at)
    end = _parse_iso8601(completed_at)
    if start is None or end is None:
        return None
    delta_ms = int((end - start).total_seconds() * 1000)
    if delta_ms < 0:
        return None
    return delta_ms


def _parse_iso8601(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_opencode_task(task: dict[str, Any]) -> bool:
    backend = str(task.get("backend") or "").lower()
    return "opencode" in backend


def _is_terminal_task(task: dict[str, Any]) -> bool:
    return str(task.get("status") or "").lower() in {"completed", "failed", "blocked"}


def _task_key(task: dict[str, Any]) -> tuple[str, str]:
    return str(task.get("run") or ""), str(task.get("id") or task.get("task_id") or "")


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
