"""Lane-specific contracts for small read-only visible multiworker runs."""

from __future__ import annotations

import json
from fnmatch import fnmatch
from pathlib import Path
from typing import Any


READONLY_HINTS = (
    "read-only",
    "readonly",
    "只读",
    "不改文件",
    "no file edits",
    "no business repo writes",
    "review",
    "审查",
    "验收",
    "风险",
    "risk",
    "acceptance",
    "audit",
)

READONLY_TOOL_FALLBACK_LINES = (
    "- If read-only discovery tools fail in Target repo root, do not widen scope to a shared checkout or runtime root.",
    "- Report structured fallback metadata instead: tool name, target_repo_root, failure_type, localized_error, fallback_used, and direct_tool_fallback_command.",
    "- When fallback is used, add one Evidence detail line exactly as: readonly_tool_fallback: tool_name=<...>; target_repo_root=<...>; failure_type=<...>; localized_error=<...>; fallback_used=<...>; direct_tool_fallback_command=<...>.",
    "- Classify CodeGraph or grep/glob failures as tool limitations unless direct target-root evidence proves a repository or worker failure.",
)

LANE_CROSS_TERMS = {
    "ui-surface": {
        "hard": (
            "apps/api/",
            "server.py",
            "cors",
            "telemetry",
            "state derivation",
            "lane 2",
            "api / state / integration",
            "api handlers",
        ),
        "soft": (
            "api",
            "/api/",
            "endpoint",
            "endpoints",
            "contract",
            "contracts",
            "schema",
        ),
    },
    "data-contract": {
        "hard": (
            "apps/web/",
            "renderview",
            "onclick",
            "lane 1",
            "ui surface",
            "interaction flow",
        ),
        "soft": (
            "button",
            "buttons",
            "layout",
            "copy",
            "fallback messaging",
            "dashboard affordance",
            "dashboard affordances",
            "visible docs",
            "empty-state",
            "empty state",
        ),
    },
    "api-state": {
        "hard": (
            "apps/web/",
            "renderview",
            "onclick",
            "lane 1",
            "ui surface",
            "interaction flow",
            "button",
            "buttons",
            "layout",
            "copy",
        ),
        "soft": (
            "empty-state",
            "empty state",
            "fallback messaging",
            "dashboard affordance",
            "dashboard affordances",
            "visible docs",
            "user-facing",
        ),
    },
    "verification-observability": {
        "hard": (
            "apps/web/",
            "apps/api/",
            "api handlers",
            "state derivation",
            "cors",
            "renderview",
            "onclick",
            "lane 1",
            "lane 2",
        ),
        "soft": (
            "button",
            "buttons",
            "layout",
            "copy",
            "empty-state",
            "empty state",
            "endpoint",
            "endpoints",
            "/api/",
            "contract",
            "contracts",
            "schema",
        ),
    },
}

VERIFICATION_REFERENCE_CUES = (
    "test",
    "tests",
    "coverage",
    "regression",
    "ci",
    "telemetry",
    "metrics",
    "monitoring",
    "logging",
    "alert",
    "alerting",
    "observability",
    "deployment",
    "deploy",
    "rollback",
    "runbook",
)

API_CONTRACT_CONSUMER_CUES = (
    "api boundary",
    "contract",
    "consumer",
    "drift",
    "endpoint",
    "payload",
    "response",
    "response shape",
    "response shapes",
    "schema",
    "shared schema",
    "type validation",
)

UI_IMPLEMENTATION_CUES = (
    "button",
    "copy",
    "interaction flow",
    "layout",
    "onclick",
    "polish",
    "renderview",
    "ui polish",
)

PRIMARY_FINDINGS_HEADING = "## Primary lane findings"
EVIDENCE_TABLE_HEADING = "## Evidence table"
HANDOFF_HEADING = "## Allowed cross-lane handoff note"
OUT_OF_SCOPE_HEADING = "## Out-of-scope observations"
UNCERTAINTIES_HEADING = "## Uncertainties and not-verified items"
RECOMMENDED_PHASE_SLICE_HEADING = "## Recommended Phase Slice"

EVIDENCE_SOURCE_LABELS = (
    "direct_tool_verified",
    "subagent_reported",
    "quoted_file_evidence",
    "derived_from_multiple_files",
    "inference",
    "generic_guidance",
    "not_verified_in_current_run",
)

LANE_ADMISSIBILITY_TAGS = (
    "primary_scope",
    "handoff_context",
    "out_of_scope",
    "future_optimization",
    "implementation_suggestion",
)

FUTURE_OPTIMIZATION_CUES = (
    "future optimization",
    "future optimisation",
    "future work",
    "later optimization",
    "later optimisation",
    "nice to have",
    "follow-up",
    "future_optimization",
    "implementation_suggestion",
)

BLOCKER_CUES = (
    "blocker",
    "blocked",
    "blocking",
    "cannot proceed",
    "无法继续",
    "阻塞",
)

BLOCKER_EVIDENCE_CUES = (
    "direct_tool_verified",
    "quoted_file_evidence",
    "evidence path",
    "evidence_path",
    "command",
    "exit code",
    "exit_code",
    "file:",
    "path:",
)

ROLE_SCALE_CONTRACT_FIELDS = {
    "role",
    "depends_on",
    "read_only",
    "allowed_files",
    "write_owner",
    "integration_owned_files",
    "forbidden_paths",
    "final_writer",
    "required_artifacts",
    "required_commands",
    "terminal_truth_requirements",
    "retry_scope",
    "downstream_action_map",
    "dependency_scan_result",
}

ROLE_SCALE_RETRY_SCOPES = {
    "lane_only",
    "integration_only",
    "post_integration_test_only",
    "full_chain",
    "human_review_required",
}

CONTEXT_RESET_REASONS = {
    "reviewer_new_diff",
    "gate_new_decision",
    "coder_risk_threshold",
    "context_pressure",
    "emergency_overflow",
    "emergency_compaction",
    "manual_operator",
}


class LaneContractError(Exception):
    """Raised when explicit lane contracts are malformed or incompatible."""


def inspect_lane_contract_bundle(path_value: str | None, *, workers: list[str]) -> dict[str, Any]:
    if not str(path_value or "").strip():
        return {
            "assignments": [],
            "planner_coverage": {},
            "context_policy": {},
            "blocked_reasons": [],
            "contract_warnings": [],
        }
    path = Path(str(path_value)).expanduser().resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        assignments, contract_warnings = _inspect_lane_assignments_data(data, workers=workers)
        planner_coverage_raw = data.get("planner_coverage") if isinstance(data, dict) else None
        context_policy_raw = data.get("context_policy") if isinstance(data, dict) else None
        return {
            "assignments": assignments,
            "target_repo_root": _resolve_target_repo_root(data),
            "planner_coverage": _normalize_planner_coverage(planner_coverage_raw, assignments),
            "context_policy": _normalize_context_policy(context_policy_raw),
            "blocked_reasons": [warning["warning_message"] for warning in contract_warnings],
            "contract_warnings": contract_warnings,
        }
    except FileNotFoundError as exc:
        return {
            "assignments": [],
            "planner_coverage": {},
            "context_policy": {},
            "blocked_reasons": [f"lane contract file not found: {path}"],
            "contract_warnings": [],
        }
    except LaneContractError as exc:
        return {
            "assignments": [],
            "planner_coverage": {},
            "context_policy": {},
            "blocked_reasons": [str(exc)],
            "contract_warnings": [],
        }
    except Exception:
        return {
            "assignments": [],
            "planner_coverage": {},
            "context_policy": {},
            "blocked_reasons": [f"failed to parse lane contract file: {path}"],
            "contract_warnings": [],
        }


def is_readonly_collaboration_request(title: str, body: str) -> bool:
    haystack = f"{title}\n{body}".lower()
    return any(hint.lower() in haystack for hint in READONLY_HINTS)


def _normalize_assignment(item: Any, *, contract_warnings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise LaneContractError("lane assignment must be an object")
    worker_id = str(item.get("worker_id") or "").strip()
    lane_id = str(item.get("lane_id") or "").strip()
    lane_focus = str(item.get("lane_focus") or "").strip()
    avoid_overlap = str(item.get("avoid_overlap") or "").strip()
    primary_allowed_scope = str(item.get("primary_allowed_scope") or "").strip()
    out_of_scope_topics = str(item.get("out_of_scope_topics") or "").strip()
    handoff_rule = str(item.get("handoff_rule") or "").strip()
    if not worker_id:
        raise LaneContractError("lane assignment missing worker_id")
    if not lane_id:
        raise LaneContractError(f"lane assignment for {worker_id} missing lane_id")
    if not lane_focus:
        raise LaneContractError(f"lane assignment for {worker_id} missing lane_focus")
    if not avoid_overlap:
        raise LaneContractError(f"lane assignment for {worker_id} missing avoid_overlap")
    normalized: dict[str, Any] = {
        "worker_id": worker_id,
        "lane_id": lane_id,
        "lane_focus": lane_focus,
        "avoid_overlap": avoid_overlap,
        "primary_allowed_scope": primary_allowed_scope,
        "out_of_scope_topics": out_of_scope_topics,
        "handoff_rule": handoff_rule,
    }
    target_repo_root = _resolve_target_repo_root(item)
    if target_repo_root:
        normalized["target_repo_root"] = target_repo_root
    allowed_changed_files = _normalize_changed_paths(item.get("allowed_changed_files"))
    if allowed_changed_files:
        normalized["allowed_changed_files"] = allowed_changed_files
    report_dependencies = _string_list(item.get("report_dependencies"))
    if report_dependencies:
        normalized["report_dependencies"] = report_dependencies
    _attach_domain_owner_contract(normalized, item)
    _validate_domain_owner_contract(normalized, contract_warnings=contract_warnings)
    _attach_role_scale_contract(normalized, item)
    _validate_role_scale_contract(normalized)
    return normalized


def _normalize_target_repo_root(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return str(Path(normalized).expanduser().resolve())


def _resolve_target_repo_root(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    target_repo_root = _normalize_target_repo_root(value.get("target_repo_root"))
    worktree = _normalize_target_repo_root(value.get("worktree"))
    if target_repo_root and worktree and target_repo_root != worktree:
        raise LaneContractError("target_repo_root conflicts with worktree")
    return target_repo_root or worktree


def _normalize_changed_paths(value: Any) -> list[str]:
    paths: list[str] = []
    for raw_path in _string_list(value):
        candidate = Path(raw_path)
        normalized = raw_path.replace("\\", "/").strip("/")
        if candidate.is_absolute() or not normalized or ".." in Path(normalized).parts:
            raise LaneContractError(f"invalid allowed_changed_file:{raw_path}")
        paths.append(normalized)
    return _dedupe(paths)


def load_lane_contract_bundle(path_value: str | None, *, workers: list[str]) -> dict[str, Any]:
    if not str(path_value or "").strip():
        return {"assignments": [], "planner_coverage": {}, "context_policy": {}}
    path = Path(str(path_value)).expanduser().resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LaneContractError(f"lane contract file not found: {path}") from exc
    except Exception as exc:
        raise LaneContractError(f"failed to parse lane contract file: {path}") from exc
    assignments = _normalize_lane_assignments_data(data, workers=workers)
    planner_coverage_raw = data.get("planner_coverage") if isinstance(data, dict) else None
    context_policy_raw = data.get("context_policy") if isinstance(data, dict) else None
    return {
        "assignments": assignments,
        "target_repo_root": _resolve_target_repo_root(data),
        "planner_coverage": _normalize_planner_coverage(planner_coverage_raw, assignments),
        "context_policy": _normalize_context_policy(context_policy_raw),
    }


def load_lane_assignments(path_value: str | None, *, workers: list[str]) -> list[dict[str, str]]:
    return load_lane_contract_bundle(path_value, workers=workers)["assignments"]


def _normalize_lane_assignments_data(data: Any, *, workers: list[str]) -> list[dict[str, Any]]:
    if isinstance(data, dict) and "assignments" not in data:
        if "lanes" in data:
            raise LaneContractError(
                "lane contract file uses top-level `lanes`; use top-level `assignments` array instead"
            )
        raise LaneContractError("lane contract file must contain a top-level `assignments` array")
    assignments_raw = data.get("assignments") if isinstance(data, dict) else data
    if not isinstance(assignments_raw, list):
        raise LaneContractError("lane contract file must contain a top-level `assignments` array")
    assignments = [_normalize_assignment(item) for item in assignments_raw]
    _apply_target_repo_root(assignments, data)
    _validate_writable_lane_targets(assignments)
    _validate_sibling_domain_sources(assignments)
    _validate_sibling_role_scale_contracts(assignments)
    worker_ids = [str(item) for item in workers if str(item)]
    expected = set(worker_ids)
    actual = {item["worker_id"] for item in assignments}
    if expected != actual:
        raise LaneContractError(
            f"lane assignments must exactly match selected workers: expected={sorted(expected)} actual={sorted(actual)}"
        )
    ordered = {item["worker_id"]: item for item in assignments}
    return [ordered[worker_id] for worker_id in worker_ids]


def _inspect_lane_assignments_data(data: Any, *, workers: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if isinstance(data, dict) and "assignments" not in data:
        if "lanes" in data:
            raise LaneContractError(
                "lane contract file uses top-level `lanes`; use top-level `assignments` array instead"
            )
        raise LaneContractError("lane contract file must contain a top-level `assignments` array")
    assignments_raw = data.get("assignments") if isinstance(data, dict) else data
    if not isinstance(assignments_raw, list):
        raise LaneContractError("lane contract file must contain a top-level `assignments` array")
    contract_warnings: list[dict[str, Any]] = []
    assignments = [_normalize_assignment(item, contract_warnings=contract_warnings) for item in assignments_raw]
    _apply_target_repo_root(assignments, data)
    _validate_writable_lane_targets(assignments)
    contract_warnings.extend(_collect_sibling_domain_overlap_warnings(assignments))
    _validate_sibling_role_scale_contracts(assignments)
    worker_ids = [str(item) for item in workers if str(item)]
    expected = set(worker_ids)
    actual = {item["worker_id"] for item in assignments}
    if expected != actual:
        raise LaneContractError(
            f"lane assignments must exactly match selected workers: expected={sorted(expected)} actual={sorted(actual)}"
        )
    ordered = {item["worker_id"]: item for item in assignments}
    return [ordered[worker_id] for worker_id in worker_ids], contract_warnings


def _apply_target_repo_root(assignments: list[dict[str, Any]], data: Any) -> None:
    target_repo_root = _resolve_target_repo_root(data)
    if not target_repo_root:
        return
    for assignment in assignments:
        assignment.setdefault("target_repo_root", target_repo_root)


def _validate_writable_lane_targets(assignments: list[dict[str, Any]]) -> None:
    for assignment in assignments:
        if assignment.get("allowed_changed_files") and not assignment.get("target_repo_root"):
            raise LaneContractError("allowed_changed_files requires target_repo_root")


def _normalize_planner_coverage(value: Any, assignments: list[dict[str, Any]]) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise LaneContractError("planner_coverage must be an object")
    schema_version = _planner_schema_version(value.get("schema_version", 1))
    changed_paths = _coverage_string_list(value.get("changed_paths"), "changed_paths")
    high_risk_paths = _coverage_string_list(value.get("high_risk_paths"), "high_risk_paths")
    required_lanes = _coverage_string_list(value.get("required_lanes"), "required_lanes")
    optional_lanes = _coverage_string_list(value.get("optional_lanes"), "optional_lanes")
    planner_warnings = _coverage_string_list(value.get("planner_warnings"), "planner_warnings")
    coverage_confidence = _coverage_confidence(value.get("coverage_confidence"))
    lane_ids = {str(item.get("lane_id") or "") for item in assignments}
    missing_required_lanes = [lane_id for lane_id in required_lanes if lane_id not in lane_ids]
    required_assignments = [
        item
        for item in assignments
        if str(item.get("lane_id") or "") in required_lanes
    ]
    high_risk_changed_paths = [
        path
        for path in changed_paths
        if _path_matches_any_source(path, high_risk_paths)
    ]
    covered_risk_paths = [
        path
        for path in high_risk_changed_paths
        if _path_covered_by_assignments(path, required_assignments)
    ]
    uncovered_risk_paths = [
        path for path in high_risk_changed_paths if path not in covered_risk_paths
    ]
    if missing_required_lanes or uncovered_risk_paths:
        status = "needs_planner_revision"
    elif coverage_confidence in {"low", "unspecified"}:
        status = "low_confidence"
    else:
        status = "covered"
    return {
        "schema_version": schema_version,
        "changed_paths": changed_paths,
        "high_risk_paths": high_risk_paths,
        "required_lanes": required_lanes,
        "optional_lanes": optional_lanes,
        "coverage_confidence": coverage_confidence,
        "planner_warnings": planner_warnings,
        "has_warnings": bool(planner_warnings),
        "covered_risk_paths": covered_risk_paths,
        "uncovered_risk_paths": uncovered_risk_paths,
        "missing_required_lanes": missing_required_lanes,
        "status": status,
    }


def _planner_schema_version(value: Any) -> int:
    try:
        schema_version = int(value)
    except (TypeError, ValueError) as exc:
        raise LaneContractError("planner_coverage schema_version must be 1") from exc
    if schema_version != 1:
        raise LaneContractError("planner_coverage schema_version must be 1")
    return schema_version


def _normalize_context_policy(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise LaneContractError("context_policy must be an object")
    schema_version = _context_policy_schema_version(value.get("schema_version", 1))
    return {
        "schema_version": schema_version,
        "min_context_epoch_by_role": _context_role_int_map(
            value.get("min_context_epoch_by_role"),
            "min_context_epoch_by_role",
        ),
        "fresh_context_required_by_role": _context_role_bool_map(
            value.get("fresh_context_required_by_role"),
            "fresh_context_required_by_role",
        ),
        "accepted_reset_reasons": _context_reset_reason_list(value.get("accepted_reset_reasons")),
    }


def _context_policy_schema_version(value: Any) -> int:
    try:
        schema_version = int(value)
    except (TypeError, ValueError) as exc:
        raise LaneContractError("context_policy schema_version must be 1") from exc
    if schema_version != 1:
        raise LaneContractError("context_policy schema_version must be 1")
    return schema_version


def _context_role_int_map(value: Any, field: str) -> dict[str, int]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise LaneContractError(f"context_policy {field} must be an object")
    result: dict[str, int] = {}
    for role, raw_epoch in value.items():
        role_name = str(role or "").strip().lower()
        if not role_name:
            raise LaneContractError(f"context_policy {field} role names must be non-empty")
        try:
            epoch = int(raw_epoch)
        except (TypeError, ValueError) as exc:
            raise LaneContractError(f"context_policy {field} values must be non-negative integers") from exc
        if epoch < 0:
            raise LaneContractError(f"context_policy {field} values must be non-negative integers")
        result[role_name] = epoch
    return result


def _context_role_bool_map(value: Any, field: str) -> dict[str, bool]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise LaneContractError(f"context_policy {field} must be an object")
    result: dict[str, bool] = {}
    for role, raw_value in value.items():
        role_name = str(role or "").strip().lower()
        if not role_name:
            raise LaneContractError(f"context_policy {field} role names must be non-empty")
        result[role_name] = _bool(raw_value)
    return result


def _context_reset_reason_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise LaneContractError("context_policy accepted_reset_reasons must be an array")
    result: list[str] = []
    for item in value:
        reason = str(item or "").strip()
        if not reason:
            continue
        if reason not in CONTEXT_RESET_REASONS:
            raise LaneContractError(f"unknown context reset reason:{reason}")
        result.append(reason)
    return _dedupe(result)


def _coverage_string_list(value: Any, field: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise LaneContractError(f"planner_coverage {field} must be an array")
    return [str(item).strip().replace("\\", "/") for item in value if str(item).strip()]


def _coverage_confidence(value: Any) -> str:
    if value in (None, ""):
        return "unspecified"
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric >= 0.8:
            return "high"
        if numeric >= 0.5:
            return "medium"
        return "low"
    text = str(value).strip().lower()
    if text in {"high", "medium", "low"}:
        return text
    raise LaneContractError("planner_coverage coverage_confidence must be high, medium, low, or a 0-1 number")


def _path_covered_by_assignments(path: str, assignments: list[dict[str, Any]]) -> bool:
    for assignment in assignments:
        if _path_matches_any_source(path, assignment.get("allowed_sources", [])):
            return True
    return False


def _path_matches_any_source(path: str, sources: list[str]) -> bool:
    return any(_path_matches_source(path, source) for source in sources)


def _path_matches_source(path: str, source: str) -> bool:
    target = _normalize_path(path)
    pattern = _normalize_path(source)
    if not target or not pattern:
        return False
    if pattern.startswith("glob:"):
        return fnmatch(target, pattern.removeprefix("glob:"))
    if any(char in pattern for char in "*?["):
        return fnmatch(target, pattern)
    normalized_pattern = pattern.rstrip("/")
    return target == normalized_pattern or target.startswith(f"{normalized_pattern}/")


def _normalize_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("./").strip("/")


def write_lane_assignments(path_value: str, assignments: list[dict[str, str]]) -> str:
    path = Path(str(path_value)).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"assignments": assignments}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path)


def _attach_domain_owner_contract(assignment: dict[str, Any], item: dict[str, Any]) -> None:
    assignment["evidence_domain"] = str(item.get("evidence_domain") or "").strip()
    assignment["ownership_reason"] = str(item.get("ownership_reason") or "").strip()
    assignment["required_or_optional"] = str(item.get("required_or_optional") or "").strip().lower()
    assignment["allowed_sources"] = _string_list(item.get("allowed_sources"))
    assignment["forbidden_sources"] = _string_list(item.get("forbidden_sources"))
    assignment["worker_authority"] = _dict_bool(item.get("worker_authority"))
    max_subagents = item.get("max_subagents", "")
    assignment["max_subagents"] = _max_subagents_value(max_subagents)
    assignment["spawn_gate"] = _dict_bool(item.get("spawn_gate"))
    assignment["expected_artifact"] = str(item.get("expected_artifact") or "").strip()
    assignment["downstream_consumer"] = str(item.get("downstream_consumer") or "").strip()
    assignment["latest_useful_moment"] = str(item.get("latest_useful_moment") or "").strip()
    assignment["drop_if_direct_path_converges"] = _bool(item.get("drop_if_direct_path_converges"))
    assignment["subdomains"] = _normalize_subdomains(item.get("subdomains"))


def _attach_role_scale_contract(assignment: dict[str, Any], item: dict[str, Any]) -> None:
    assignment["role"] = str(item.get("role") or "").strip().lower()
    assignment["depends_on"] = _string_list_field(item.get("depends_on"), "depends_on")
    assignment["read_only"] = _bool(item.get("read_only")) if "read_only" in item else False
    assignment["allowed_files"] = _string_list_field(item.get("allowed_files"), "allowed_files")
    assignment["write_owner"] = str(item.get("write_owner") or "").strip()
    assignment["integration_owned_files"] = _string_list_field(
        item.get("integration_owned_files"),
        "integration_owned_files",
    )
    assignment["forbidden_paths"] = _string_list_field(item.get("forbidden_paths"), "forbidden_paths")
    assignment["final_writer"] = str(item.get("final_writer") or "").strip()
    assignment["required_artifacts"] = _string_list_field(item.get("required_artifacts"), "required_artifacts")
    assignment["required_commands"] = _string_list_field(item.get("required_commands"), "required_commands")
    assignment["terminal_truth_requirements"] = _dict_any(
        item.get("terminal_truth_requirements"),
        "terminal_truth_requirements",
    )
    assignment["retry_scope"] = str(item.get("retry_scope") or "").strip()
    assignment["downstream_action_map"] = _dict_str(
        item.get("downstream_action_map"),
        "downstream_action_map",
    )
    assignment["dependency_scan_result"] = _normalize_dependency_scan_result(item.get("dependency_scan_result"))


def _validate_role_scale_contract(assignment: dict[str, Any]) -> None:
    if not _has_role_scale_contract(assignment):
        return
    worker_id = str(assignment.get("worker_id") or "-")
    lane_id = str(assignment.get("lane_id") or "-")
    role = str(assignment.get("role") or "")
    retry_scope = str(assignment.get("retry_scope") or "")
    if retry_scope and retry_scope not in ROLE_SCALE_RETRY_SCOPES:
        raise LaneContractError(f"lane assignment for {worker_id} invalid retry_scope:{retry_scope}")
    if role == "integrator" and not bool(assignment.get("read_only")):
        integration_files = list(assignment.get("integration_owned_files") or [])
        if not integration_files:
            raise LaneContractError(f"lane assignment for {worker_id} integrator writes require integration_owned_files")
        for path in assignment.get("allowed_files", []):
            if not _path_matches_any_source(path, integration_files):
                raise LaneContractError(
                    f"lane assignment for {worker_id} integrator allowed_files must stay within integration_owned_files"
                )
    if role == "tester":
        final_writer = str(assignment.get("final_writer") or "")
        depends_on = set(assignment.get("depends_on") or [])
        if final_writer and final_writer not in depends_on:
            raise LaneContractError(
                f"lane assignment for {worker_id} tester must depend on final_writer:{final_writer}"
            )
    if role == "reviewer" and not assignment.get("downstream_action_map"):
        raise LaneContractError(f"lane assignment for {worker_id} reviewer missing downstream_action_map")
    if role == "gate" and not assignment.get("terminal_truth_requirements") and not assignment.get("required_artifacts"):
        raise LaneContractError(
            f"lane assignment for {worker_id} gate missing terminal_truth_requirements or required_artifacts"
        )
    if role and role not in {"coder", "integrator", "tester", "reviewer", "gate"}:
        raise LaneContractError(f"lane assignment for {worker_id} unknown role:{role}")
    if role in {"coder", "integrator", "tester", "reviewer", "gate"} and not lane_id:
        raise LaneContractError(f"lane assignment for {worker_id} missing lane_id")


def _has_role_scale_contract(assignment: dict[str, Any]) -> bool:
    return any(
        (
            assignment.get("role"),
            assignment.get("depends_on"),
            assignment.get("read_only"),
            assignment.get("allowed_files"),
            assignment.get("write_owner"),
            assignment.get("integration_owned_files"),
            assignment.get("forbidden_paths"),
            assignment.get("final_writer"),
            assignment.get("required_artifacts"),
            assignment.get("required_commands"),
            assignment.get("terminal_truth_requirements"),
            assignment.get("retry_scope"),
            assignment.get("downstream_action_map"),
            assignment.get("dependency_scan_result"),
        )
    )


def _validate_domain_owner_contract(
    assignment: dict[str, Any],
    *,
    contract_warnings: list[dict[str, Any]] | None = None,
) -> None:
    authority = assignment.get("worker_authority") if isinstance(assignment.get("worker_authority"), dict) else {}
    spawn_gate = assignment.get("spawn_gate") if isinstance(assignment.get("spawn_gate"), dict) else {}
    may_spawn = bool(authority.get("may_spawn_native_subagents"))
    has_domain_contract = any(
        (
            assignment.get("evidence_domain"),
            assignment.get("ownership_reason"),
            assignment.get("required_or_optional"),
            assignment.get("allowed_sources"),
            assignment.get("forbidden_sources"),
            assignment.get("expected_artifact"),
            assignment.get("downstream_consumer"),
            may_spawn,
            assignment.get("subdomains"),
        )
    )
    if not has_domain_contract:
        return
    worker_id = str(assignment.get("worker_id") or "-")
    if may_spawn and not assignment.get("evidence_domain"):
        raise LaneContractError(f"lane assignment for {worker_id} missing evidence_domain")
    required_or_optional = str(assignment.get("required_or_optional") or "").strip()
    if required_or_optional and required_or_optional not in {"required", "optional"}:
        raise LaneContractError(f"lane assignment for {worker_id} invalid required_or_optional:{required_or_optional}")
    if may_spawn and not assignment.get("ownership_reason"):
        raise LaneContractError(f"lane assignment for {worker_id} missing ownership_reason")
    if may_spawn and not assignment.get("expected_artifact"):
        raise LaneContractError(f"lane assignment for {worker_id} missing expected_artifact")
    if may_spawn and not assignment.get("allowed_sources"):
        raise LaneContractError(f"lane assignment for {worker_id} missing allowed_sources")
    if may_spawn and int(assignment.get("max_subagents") or 0) <= 0:
        raise LaneContractError(f"lane assignment for {worker_id} max_subagents must be greater than 0")
    if spawn_gate.get("require_expected_unique_value"):
        for subdomain in assignment.get("subdomains", []):
            if not subdomain.get("expected_unique_value"):
                raise LaneContractError(
                    f"lane assignment for {worker_id} subdomain {subdomain.get('subdomain_id') or '-'} missing expected_unique_value"
                )
    if spawn_gate.get("require_latest_useful_moment") and not assignment.get("latest_useful_moment"):
        raise LaneContractError(f"lane assignment for {worker_id} missing latest_useful_moment")
    if spawn_gate.get("require_disjoint_subdomains"):
        if contract_warnings is None:
            _validate_subdomain_sources(assignment)
        else:
            contract_warnings.extend(_collect_subdomain_overlap_warnings(assignment))
    if may_spawn and not assignment.get("downstream_consumer"):
        raise LaneContractError(f"lane assignment for {worker_id} missing downstream_consumer")


def _validate_sibling_domain_sources(assignments: list[dict[str, Any]]) -> None:
    warnings = _collect_sibling_domain_overlap_warnings(assignments)
    if warnings:
        raise LaneContractError(warnings[0]["warning_message"])


def _validate_sibling_role_scale_contracts(assignments: list[dict[str, Any]]) -> None:
    rows: list[tuple[str, str, str]] = []
    for assignment in assignments:
        if bool(assignment.get("read_only")):
            continue
        worker_id = str(assignment.get("worker_id") or "-")
        lane_id = str(assignment.get("lane_id") or "-")
        for path in _role_scale_write_files(assignment):
            rows.append((worker_id, lane_id, path))
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if _sources_overlap(left[2], right[2]):
                raise LaneContractError(
                    "overlapping lane write ownership: "
                    f"{left[0]}:{left[1]}:{left[2]} overlaps {right[0]}:{right[1]}:{right[2]}"
                )


def _role_scale_write_files(assignment: dict[str, Any]) -> list[str]:
    return _dedupe(
        [
            str(item)
            for item in list(assignment.get("allowed_files") or [])
            + list(assignment.get("integration_owned_files") or [])
            if str(item).strip()
        ]
    )


def _validate_subdomain_sources(assignment: dict[str, Any]) -> None:
    warnings = _collect_subdomain_overlap_warnings(assignment)
    if warnings:
        raise LaneContractError(warnings[0]["warning_message"])


def _collect_sibling_domain_overlap_warnings(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[tuple[str, str, str]] = []
    warnings: list[dict[str, Any]] = []
    for assignment in assignments:
        domain = str(assignment.get("evidence_domain") or "")
        if not domain:
            continue
        for source in assignment.get("allowed_sources", []):
            rows.append((str(assignment.get("worker_id") or "-"), domain, source))
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if _sources_overlap(left[2], right[2]):
                warnings.append(
                    {
                        "warning_code": "warn_contract_overlap",
                        "warning_phase": "contract_validation",
                        "overlap_scope": "worker_domain",
                        "overlap_pairs": [
                            f"{left[0]}:{left[1]}:{left[2]}",
                            f"{right[0]}:{right[1]}:{right[2]}",
                        ],
                        "warning_message": (
                            "overlapping domain sources: "
                            f"{left[0]}:{left[1]}:{left[2]} overlaps {right[0]}:{right[1]}:{right[2]}"
                        ),
                        "waiver_supported": False,
                    }
                )
    return warnings


def _collect_subdomain_overlap_warnings(assignment: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[tuple[str, str, str]] = []
    warnings: list[dict[str, Any]] = []
    for subdomain in assignment.get("subdomains", []):
        subdomain_id = str(subdomain.get("subdomain_id") or "-")
        agent = str(subdomain.get("agent") or "-")
        for source in subdomain.get("allowed_sources", []):
            rows.append((subdomain_id, agent, source))
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if _sources_overlap(left[2], right[2]):
                overlap_scope = "same_role_subdomain" if left[1] == right[1] else "cross_role_subdomain"
                warnings.append(
                    {
                        "warning_code": "warn_contract_overlap",
                        "warning_phase": "contract_validation",
                        "overlap_scope": overlap_scope,
                        "overlap_pairs": [
                            f"{left[0]}:{left[1]}:{left[2]}",
                            f"{right[0]}:{right[1]}:{right[2]}",
                        ],
                        "warning_message": (
                            "overlapping subdomain sources: "
                            f"{left[0]}:{left[1]}:{left[2]} overlaps {right[0]}:{right[1]}:{right[2]}"
                        ),
                        "waiver_supported": False,
                    }
                )
    return warnings


def _normalize_subdomains(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise LaneContractError("subdomain must be an object")
        subdomain_id = _safe_subdomain_id(item.get("subdomain_id"))
        agent = str(item.get("agent") or "").strip()
        if not agent:
            raise LaneContractError(f"subdomain {subdomain_id} missing agent")
        result.append(
            {
                "subdomain_id": subdomain_id,
                "agent": agent,
                "allowed_sources": _string_list(item.get("allowed_sources")),
                "expected_unique_value": str(item.get("expected_unique_value") or "").strip(),
            }
        )
    return result


def _max_subagents_value(value: Any) -> int:
    if value in (None, ""):
        return 0
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(text)
    except (TypeError, ValueError) as exc:
        raise LaneContractError("max_subagents must be an integer") from exc


def _safe_subdomain_id(value: Any) -> str:
    raw = str(value or "").strip()
    subdomain_id = raw.replace("\\", "/")
    if not subdomain_id:
        raise LaneContractError("subdomain missing subdomain_id")
    if "/" in subdomain_id or subdomain_id in {".", ".."}:
        raise LaneContractError("subdomain_id must be a single safe path component")
    return subdomain_id


def _string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise LaneContractError("domain source fields must be arrays")
    return [str(item).strip() for item in value if str(item).strip()]


def _string_list_field(value: Any, field: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise LaneContractError(f"{field} must be an array")
    return [str(item).strip().replace("\\", "/") for item in value if str(item).strip()]


def _dict_any(value: Any, field: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise LaneContractError(f"{field} must be an object")
    return dict(value)


def _dict_str(value: Any, field: str) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise LaneContractError(f"{field} must be an object")
    return {str(key): str(item).strip() for key, item in value.items() if str(key).strip()}


def _normalize_dependency_scan_result(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise LaneContractError("dependency_scan_result must be an object")
    required = {
        "touched_files",
        "imports_or_shared_config_overlap",
        "downgrade_decision",
        "downgrade_rationale",
    }
    missing = sorted(key for key in required if key not in value)
    if missing:
        raise LaneContractError(f"dependency_scan_result missing fields:{','.join(missing)}")
    return {
        "touched_files": _string_list_field(value.get("touched_files"), "dependency_scan_result.touched_files"),
        "imports_or_shared_config_overlap": _string_list_field(
            value.get("imports_or_shared_config_overlap"),
            "dependency_scan_result.imports_or_shared_config_overlap",
        ),
        "downgrade_decision": str(value.get("downgrade_decision") or "").strip(),
        "downgrade_rationale": str(value.get("downgrade_rationale") or "").strip(),
    }


def _dict_bool(value: Any) -> dict[str, bool]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise LaneContractError("domain boolean fields must be objects")
    return {str(key): _bool(item) for key, item in value.items()}


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _sources_overlap(left: str, right: str) -> bool:
    left_norm = _source_prefix(left)
    right_norm = _source_prefix(right)
    if not left_norm or not right_norm:
        return False
    return left_norm == right_norm or left_norm.startswith(right_norm) or right_norm.startswith(left_norm)


def _source_prefix(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").strip("/")
    return f"{normalized}/" if normalized and not normalized.endswith("/") else normalized


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def derive_lane_assignments(
    workers: list[str],
    *,
    title: str,
    body: str,
    inner_mode: str = "",
) -> list[dict[str, str]]:
    if inner_mode:
        return []
    if not is_readonly_collaboration_request(title, body):
        return []
    if len(workers) == 2:
        return [
            {
                "worker_id": workers[0],
                "lane_id": "ui-surface",
                "lane_focus": "Focus on user-facing pages, UI behavior, interaction flow, copy, fallback messaging, and visible docs.",
                "avoid_overlap": "Do not spend time on backend/state internals unless they are needed to explain a user-facing symptom.",
                "primary_allowed_scope": "User-facing pages, interaction flow, fallback messaging, copy, and visible documentation.",
                "out_of_scope_topics": "API handlers, endpoint contracts, CORS policy, telemetry semantics, and backend/state internals unless needed for a visible symptom.",
                "handoff_rule": "If a cross-lane issue matters, mention it in one short handoff note only; do not promote it into a primary finding for this lane.",
            },
            {
                "worker_id": workers[1],
                "lane_id": "data-contract",
                "lane_focus": "Focus on API handlers, data fetch/state derivation, shared contracts, and risk/monitoring semantics.",
                "avoid_overlap": "Do not spend time on UI/layout details or apps/web frontend loading behavior unless they are needed as consumer evidence for a data or contract issue.",
                "primary_allowed_scope": "API handlers, state derivation, contracts, telemetry semantics, and backend risk boundaries.",
                "out_of_scope_topics": "Layout, copy, interaction polish, apps/web frontend loading behavior, and visible-surface UX details unless needed to explain a contract or state issue.",
                "handoff_rule": "If a cross-lane issue matters, mention it in one short handoff note only; do not promote it into a primary finding for this lane.",
            },
        ]
    if len(workers) == 3:
        return [
            {
                "worker_id": workers[0],
                "lane_id": "ui-surface",
                "lane_focus": "Focus on user-facing pages, UI behavior, interaction flow, copy, fallback messaging, and visible docs.",
                "avoid_overlap": "Do not spend time on backend/state internals or on test/telemetry inventory unless they are needed to explain a visible symptom.",
                "primary_allowed_scope": "User-facing pages, interaction flow, fallback messaging, copy, and visible documentation.",
                "out_of_scope_topics": "API handlers, endpoint contracts, CORS policy, state derivation, test coverage inventory, and telemetry rollout semantics unless needed for a visible symptom.",
                "handoff_rule": "If a cross-lane issue matters, mention it in one short handoff note only; do not promote it into a primary finding for this lane.",
            },
            {
                "worker_id": workers[1],
                "lane_id": "api-state",
                "lane_focus": "Focus on API handlers, request/response contracts, state derivation, and backend integration semantics.",
                "avoid_overlap": "Do not spend time on UI polish, apps/web frontend loading behavior, or test/telemetry inventory unless frontend code is needed only as consumer evidence for API contract drift.",
                "primary_allowed_scope": "API handlers, endpoint contracts, state derivation, shared schemas, and backend integration boundaries.",
                "out_of_scope_topics": "Layout, copy, interaction polish, apps/web frontend loading behavior, and broad regression/observability inventory unless needed to explain an API or state issue.",
                "handoff_rule": "If a cross-lane issue matters, mention it in one short handoff note only; do not promote it into a primary finding for this lane.",
            },
            {
                "worker_id": workers[2],
                "lane_id": "verification-observability",
                "lane_focus": "Focus on regression coverage, tests, telemetry/monitoring signals, rollout safety, and docs or runbook gaps.",
                "avoid_overlap": "Do not re-do deep UI or backend root-cause analysis; inspect them only enough to explain missing coverage, weak telemetry, or rollout risk.",
                "primary_allowed_scope": "Tests, regression coverage, telemetry signals, alertability, rollout safety, docs, and operational risk boundaries.",
                "out_of_scope_topics": "UI polish and endpoint implementation details unless they are required to explain a verification or observability gap.",
                "handoff_rule": "If a cross-lane issue matters, keep it to one short handoff note; do not turn another lane's implementation details into your main findings.",
            },
        ]
    return []


def resolve_lane_assignments(
    workers: list[str],
    *,
    title: str,
    body: str,
    inner_mode: str = "",
    lane_contracts_path: str | None = None,
) -> list[dict[str, str]]:
    explicit = load_lane_assignments(lane_contracts_path, workers=workers)
    if explicit:
        return explicit
    return derive_lane_assignments(
        workers,
        title=title,
        body=body,
        inner_mode=inner_mode,
    )


def render_lane_body(base_body: str, assignment: dict[str, Any]) -> str:
    body = str(base_body or "").rstrip()
    lane_id = str(assignment.get("lane_id") or "").strip()
    lane_focus = str(assignment.get("lane_focus") or "").strip()
    avoid_overlap = str(assignment.get("avoid_overlap") or "").strip()
    primary_allowed_scope = _resolved_primary_allowed_scope(assignment)
    out_of_scope_topics = str(assignment.get("out_of_scope_topics") or "").strip()
    handoff_rule = str(assignment.get("handoff_rule") or "").strip()
    target_repo_root = str(assignment.get("target_repo_root") or "").strip()
    report_dependencies = [str(item).strip() for item in assignment.get("report_dependencies") or [] if str(item).strip()]
    if not lane_id:
        return body
    readonly_tool_fallback_lines = (
        list(READONLY_TOOL_FALLBACK_LINES)
        if target_repo_root or bool(assignment.get("read_only"))
        else []
    )
    lines = [body] if body else []
    domain_lines = _render_domain_owner_lines(assignment)
    lines.extend(
        [
            "",
            "Lane-specific contract:",
            f"- lane: {lane_id}",
            f"- focus: {lane_focus}",
            f"- avoid_overlap: {avoid_overlap}",
            f"- Primary allowed scope: {primary_allowed_scope}",
            f"- Out-of-scope topics: {out_of_scope_topics}",
            f"- Cross-lane handoff rule: {handoff_rule}",
            f"- Target repo root: {target_repo_root or '-'}",
            "- Treat relative paths in this contract as relative to Target repo root, not the control root or installed runtime root.",
            *readonly_tool_fallback_lines,
            f"- Report dependencies: {', '.join(report_dependencies) or '-'}",
            "- If report dependencies are listed, wait for exactly those paths; do not infer or rename dependency reports.",
            "- Required report structure:",
            f"  - {PRIMARY_FINDINGS_HEADING}",
            "    Put only lane-specific main findings here.",
            f"  - {EVIDENCE_TABLE_HEADING}",
            "    Include columns: Claim | Source label | Evidence path | Evidence detail | Verification state.",
            f"  - {HANDOFF_HEADING}",
            "    Use only for brief cross-lane handoff notes; keep it short.",
            f"  - {OUT_OF_SCOPE_HEADING}",
            "    Optional, max 1-2 short bullets.",
            f"  - {UNCERTAINTIES_HEADING}",
            "    List gaps as not verified instead of turning them into blockers.",
            f"  - {RECOMMENDED_PHASE_SLICE_HEADING}",
            "    Optional. Put `future_optimization` and `implementation_suggestion` only here, not in Primary lane findings.",
            "- Required evidence source labels:",
            "  - " + ", ".join(EVIDENCE_SOURCE_LABELS),
            "- Required lane-admissibility tags:",
            "  - " + ", ".join(LANE_ADMISSIBILITY_TAGS),
            "- For strict audits, avoid generic recap and broad narration; write compact findings that the leader can merge directly.",
            "- A blocker claim must include a direct evidence path plus quoted behavior or a parent-verifiable command/result.",
            "- Without direct evidence, label negative findings `suspected_gap` or `negative_finding_requires_parent_confirmation`.",
            "- Keep `future_optimization` and `implementation_suggestion` out of Primary lane findings unless the task explicitly asks for future work.",
            "- output: report concrete findings, inspected files, commands run, and risks for this lane only.",
            "- Do not create a section for another lane or promote cross-lane observations into primary findings.",
            "- Do not decide global pass/block from mid-run lock or worker samples; report them as provisional unless the leader supplies final postflight evidence.",
            "- Before finishing, run a lane self-check:",
            "  - verify Primary lane findings stay in-lane",
            "  - verify cross-lane content appears only in the handoff or out-of-scope sections",
            "  - verify no second-lane section was created",
            "  - verify each important claim has an evidence source label",
            "  - verify blocker claims have direct evidence",
            "  - verify future optimization suggestions are not primary findings and stay in Recommended Phase Slice",
            "- If the self-check fails, rewrite the report once before marking done.",
        ]
    )
    lines.extend(domain_lines)
    return "\n".join(lines)


def _resolved_primary_allowed_scope(assignment: dict[str, Any]) -> str:
    primary_allowed_scope = str(assignment.get("primary_allowed_scope") or "").strip()
    if primary_allowed_scope:
        return primary_allowed_scope
    allowed_sources = [str(item).strip() for item in assignment.get("allowed_sources") or [] if str(item).strip()]
    if allowed_sources:
        return f"Allowed sources only: {', '.join(allowed_sources)}"
    return ""


def _render_domain_owner_lines(assignment: dict[str, Any]) -> list[str]:
    evidence_domain = str(assignment.get("evidence_domain") or "").strip()
    ownership_reason = str(assignment.get("ownership_reason") or "").strip()
    required_or_optional = str(assignment.get("required_or_optional") or "").strip()
    allowed_sources = [str(item).strip() for item in assignment.get("allowed_sources") or [] if str(item).strip()]
    forbidden_sources = [str(item).strip() for item in assignment.get("forbidden_sources") or [] if str(item).strip()]
    expected_artifact = str(assignment.get("expected_artifact") or "").strip()
    downstream_consumer = str(assignment.get("downstream_consumer") or "").strip()
    latest_useful_moment = str(assignment.get("latest_useful_moment") or "").strip()
    authority = assignment.get("worker_authority") if isinstance(assignment.get("worker_authority"), dict) else {}
    spawn_gate = assignment.get("spawn_gate") if isinstance(assignment.get("spawn_gate"), dict) else {}
    subdomains = assignment.get("subdomains") if isinstance(assignment.get("subdomains"), list) else []
    has_source_boundary = any(
        (
            evidence_domain,
            ownership_reason,
            allowed_sources,
            forbidden_sources,
            expected_artifact,
            downstream_consumer,
            latest_useful_moment,
            authority,
            spawn_gate,
            subdomains,
        )
    )
    if not has_source_boundary:
        return []
    lines = [
        "",
        "Worker domain-owner contract:" if evidence_domain else "Worker source-boundary contract:",
    ]
    if evidence_domain:
        lines.append(f"- evidence_domain: {evidence_domain}")
    if evidence_domain or ownership_reason:
        lines.append(f"- ownership_reason: {ownership_reason or '-'}")
    lines.extend(
        [
            f"- allowed_sources: {', '.join(allowed_sources) or '-'}",
            f"- forbidden_sources: {', '.join(forbidden_sources) or '-'}",
        ]
    )
    has_domain_owner_details = bool(
        evidence_domain
        or ownership_reason
        or expected_artifact
        or downstream_consumer
        or latest_useful_moment
        or authority
        or spawn_gate
        or subdomains
    )
    if has_domain_owner_details:
        lines.extend(
            [
                f"- expected_artifact: {expected_artifact or '-'}",
                f"- required_or_optional: {required_or_optional or '-'}",
                f"- downstream_consumer: {downstream_consumer or '-'}",
                f"- latest_useful_moment: {latest_useful_moment or '-'}",
                f"- drop_if_direct_path_converges: {str(bool(assignment.get('drop_if_direct_path_converges'))).lower()}",
                f"- may_spawn_native_subagents: {str(bool(authority.get('may_spawn_native_subagents'))).lower()}",
                f"- may_reinterpret_global_goal: {str(bool(authority.get('may_reinterpret_global_goal'))).lower()}",
                f"- may_write_runtime_state: {str(bool(authority.get('may_write_runtime_state'))).lower()}",
                f"- require_expected_unique_value: {str(bool(spawn_gate.get('require_expected_unique_value'))).lower()}",
                f"- require_disjoint_subdomains: {str(bool(spawn_gate.get('require_disjoint_subdomains'))).lower()}",
                "- Do not reinterpret the global task; own only this evidence domain.",
                "- Spawn native subagents only for disjoint subdomains with expected unique value.",
                "- Produce the expected domain artifact for the Main Agent instead of long prose.",
            ]
        )
    else:
        lines.append("- Stay within the listed allowed_sources and forbidden_sources.")
    if subdomains:
        lines.append("- subdomains:")
        for subdomain in subdomains:
            lines.append(
                "  - "
                f"{subdomain.get('subdomain_id') or '-'} "
                f"agent={subdomain.get('agent') or '-'} "
                f"allowed_sources={', '.join(subdomain.get('allowed_sources') or []) or '-'} "
                f"expected_unique_value={subdomain.get('expected_unique_value') or '-'}"
            )
    return lines


def analyze_lane_report_adherence(report_text: str, assignment: dict[str, Any]) -> dict[str, Any]:
    lane_id = str(assignment.get("lane_id") or "").strip()
    text = str(report_text or "")
    if not lane_id or not text.strip():
        return {"ok": True, "status": "not_evaluated", "matched_terms": []}
    haystack = text.lower()
    term_bucket = LANE_CROSS_TERMS.get(lane_id, {})
    hard_terms = tuple(term_bucket.get("hard", ())) if isinstance(term_bucket, dict) else ()
    soft_terms = tuple(term_bucket.get("soft", ())) if isinstance(term_bucket, dict) else ()
    matched_terms = [term for term in (*hard_terms, *soft_terms) if term in haystack]
    lower_lines = [line.strip().lower() for line in text.splitlines()]
    def _find_heading(heading: str) -> int:
        target = heading.lower()
        for index, line in enumerate(lower_lines):
            if line == target or line.startswith(f"{target} ") or line.startswith(f"{target}:"):
                return index
        return -1

    primary_idx = _find_heading(PRIMARY_FINDINGS_HEADING)
    evidence_idx = _find_heading(EVIDENCE_TABLE_HEADING)
    handoff_idx = _find_heading(HANDOFF_HEADING)
    out_idx = _find_heading(OUT_OF_SCOPE_HEADING)
    uncertainties_idx = _find_heading(UNCERTAINTIES_HEADING)
    recommended_idx = _find_heading(RECOMMENDED_PHASE_SLICE_HEADING)

    def _section_lines(start: int, end_candidates: list[int]) -> list[str]:
        if start < 0:
            return []
        end = min([idx for idx in end_candidates if idx > start], default=len(text.splitlines()))
        return [line.lower() for line in text.splitlines()[start + 1 : end]]

    def _section_text(start: int, end_candidates: list[int]) -> str:
        return "\n".join(_section_lines(start, end_candidates))

    def _verification_reference_line(line: str) -> bool:
        if lane_id != "verification-observability":
            return False
        return any(cue in line for cue in VERIFICATION_REFERENCE_CUES)

    def _verification_inventory_line(index: int) -> bool:
        if lane_id != "verification-observability":
            return False
        inventory = False
        for previous in primary_lines[: index + 1]:
            stripped = previous.strip()
            if stripped.startswith(("### ", "## ")):
                inventory = False
            if stripped in {"findings:", "risks:", "impact:"}:
                inventory = False
            if stripped in {"inspected files:", "files inspected:", "commands run:"}:
                inventory = True
                continue
        return inventory

    def _api_contract_consumer_line(term: str, line: str) -> bool:
        if lane_id not in {"api-state", "data-contract"}:
            return False
        if term != "apps/web/":
            return False
        if not any(cue in line for cue in API_CONTRACT_CONSUMER_CUES):
            return False
        return not any(cue in line for cue in UI_IMPLEMENTATION_CUES)

    section_boundaries = [evidence_idx, handoff_idx, out_idx, uncertainties_idx, recommended_idx]
    primary_lines = _section_lines(primary_idx, section_boundaries)
    primary_text = "\n".join(primary_lines)
    evidence_text = _section_text(evidence_idx, [handoff_idx, out_idx, uncertainties_idx, recommended_idx])
    handoff_text = _section_text(handoff_idx, [out_idx, uncertainties_idx, recommended_idx])
    out_text = _section_text(out_idx, [uncertainties_idx, recommended_idx])
    uncertainties_text = _section_text(uncertainties_idx, [recommended_idx])
    recommended_text = _section_text(recommended_idx, [])
    allowed_hard_primary_matches = list(
        dict.fromkeys(
            term
            for term in hard_terms
            for line in primary_lines
            if term in line and _api_contract_consumer_line(term, line)
        )
    )
    hard_primary_matches = list(
        dict.fromkeys(
            term
            for term in hard_terms
            for index, line in enumerate(primary_lines)
            if term in line
            and not (
                _verification_reference_line(line)
                or _verification_inventory_line(index)
                or _api_contract_consumer_line(term, line)
            )
        )
    )
    soft_primary_matches = [term for term in soft_terms if term in primary_text]
    handoff_matches = [
        term
        for term in (*hard_terms, *soft_terms)
        if term in handoff_text or term in out_text or term in uncertainties_text or term in recommended_text
    ]
    future_primary_matches = [term for term in FUTURE_OPTIMIZATION_CUES if term in primary_text]
    recommended_future_matches = [term for term in FUTURE_OPTIMIZATION_CUES if term in recommended_text]
    evidence_rows = [
        line
        for line in evidence_text.splitlines()
        if line.strip().startswith("|") and "source label" not in line and "---" not in line
    ]
    blocker_evidence_text = f"{primary_text}\n" + "\n".join(evidence_rows)
    blocker_without_evidence = (
        any(cue in primary_text for cue in BLOCKER_CUES)
        and not any(cue in blocker_evidence_text for cue in BLOCKER_EVIDENCE_CUES)
    )

    if hard_primary_matches:
        return {
            "ok": False,
            "status": "cross_lane_bleed_suspected",
            "matched_terms": hard_primary_matches,
        }
    if future_primary_matches:
        return {
            "ok": False,
            "status": "primary_future_optimization_suspected",
            "matched_terms": list(dict.fromkeys(future_primary_matches)),
        }
    if blocker_without_evidence:
        return {
            "ok": False,
            "status": "blocker_evidence_missing",
            "matched_terms": [cue for cue in BLOCKER_CUES if cue in primary_text],
        }
    if soft_primary_matches and (handoff_idx >= 0 or out_idx >= 0):
        return {
            "ok": True,
            "status": "handoff_only_spill",
            "matched_terms": list(dict.fromkeys(soft_primary_matches + handoff_matches)),
        }
    if allowed_hard_primary_matches:
        return {
            "ok": True,
            "status": "handoff_only_spill",
            "matched_terms": list(dict.fromkeys(allowed_hard_primary_matches + handoff_matches)),
        }
    if recommended_future_matches:
        return {
            "ok": True,
            "status": "handoff_only_spill",
            "matched_terms": list(dict.fromkeys(recommended_future_matches + handoff_matches)),
        }
    if handoff_matches:
        return {
            "ok": True,
            "status": "handoff_only_spill",
            "matched_terms": handoff_matches,
        }
    if len(matched_terms) >= 2:
        return {
            "ok": False,
            "status": "cross_lane_bleed_suspected",
            "matched_terms": matched_terms,
        }
    return {
        "ok": True,
        "status": "aligned",
        "matched_terms": matched_terms,
    }
