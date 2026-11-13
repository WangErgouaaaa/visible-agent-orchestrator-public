from __future__ import annotations

from typing import Any


SCHEMA_VERSION = 1
BLACKBOARD_KIND = "evidence_domain_blackboard"
CONTRACT_KIND = "evidence_blackboard_contract"
BLACKBOARD_EVENT_KIND = "evidence_blackboard_event"
BLACKBOARD_SNAPSHOT_KIND = "evidence_blackboard_snapshot"
DEFAULT_MODE = "legacy_push"
CANONICAL_DOMAIN_STATUSES = {
    "missing",
    "running",
    "ready",
    "conflicted",
    "expired",
    "unknown_retrieval",
}
DOMAIN_STATE_SUMMARY_KEYS = (
    "required",
    "optional",
    "present",
    "missing",
    "stale",
    "consumed",
    "blocked",
    "running",
    "unknown",
)
ROLLOUT_MODES: dict[str, dict[str, Any]] = {
    "legacy_push": {
        "actuation_enabled": False,
        "description": "current push-style dispatch; contracts and advisory projections only",
        "requires_explicit_opt_in": False,
    },
    "blackboard_dry_run": {
        "actuation_enabled": False,
        "description": "collect and project evidence-domain state without dispatch control",
        "requires_explicit_opt_in": False,
    },
    "pull_dry_run": {
        "actuation_enabled": False,
        "description": "compute evidence-gap dispatch recommendations without live actuation",
        "requires_explicit_opt_in": False,
    },
    "pull_live": {
        "actuation_enabled": True,
        "description": "live pull scheduling through sanctioned control-plane surfaces",
        "requires_explicit_opt_in": True,
    },
}

BLACKBOARD_EVENT_TYPES = {
    "lane_started",
    "lane_completed",
    "lane_failed",
    "coverage_updated",
    "conflict_reported",
}
BLACKBOARD_ENTITY_TYPES = {
    "lane",
    "task",
    "planner",
    "risk",
    "semantic",
    "domain",
}
EVENT_STATUS_BY_TYPE = {
    "lane_started": "running",
    "lane_completed": "ready",
    "lane_failed": "conflicted",
    "conflict_reported": "conflicted",
}
TERMINAL_EVENT_STATUSES = {"ready", "conflicted"}

STATUS_ALIASES = {
    "missing_required_artifact": "missing",
    "domain_artifact_missing": "missing",
    "artifact_missing": "missing",
    "partial_role_output": "missing",
    "complete": "ready",
    "completed": "ready",
    "ok": "ready",
    "done": "ready",
    "merged": "ready",
    "retrieval_ready": "ready",
    "merge_ready_exact_contract": "ready",
    "started": "running",
    "submitted": "running",
    "received": "running",
    "heartbeat": "running",
    "conflict": "conflicted",
    "blocked": "conflicted",
    "failed": "conflicted",
    "cancelled": "expired",
    "canceled": "expired",
    "cancelled_before_merge": "expired",
    "direct_path_converged_first": "expired",
    "retrieval_not_ready_or_unknown": "unknown_retrieval",
    "completed_but_indexing": "unknown_retrieval",
    "completed_indexing": "unknown_retrieval",
    "retrieval_failed": "unknown_retrieval",
    "not_retrieved": "unknown_retrieval",
    "unknown": "unknown_retrieval",
}


class BlackboardContractError(Exception):
    """Raised when evidence blackboard contract input is incompatible."""


def build_blackboard_contract(*, mode: str | None = None, allow_live: bool = False) -> dict[str, Any]:
    selected_mode = resolve_rollout_mode(mode or DEFAULT_MODE, allow_live=allow_live)
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_kind": CONTRACT_KIND,
        "default_mode": DEFAULT_MODE,
        "selected_mode": selected_mode,
        "actuation_enabled": bool(ROLLOUT_MODES[selected_mode]["actuation_enabled"]),
        "modes": ROLLOUT_MODES,
        "completion_truth": "report.md plus exactly one terminal marker",
        "compatibility": {
            "legacy_domain_artifact_rows": "mapped_to_canonical_domain_status",
            "legacy_push_available": True,
            "unknown_future_schema": "fail_closed",
        },
    }


def resolve_rollout_mode(mode: str | None, *, allow_live: bool = False) -> str:
    normalized = str(mode or DEFAULT_MODE).strip() or DEFAULT_MODE
    if normalized not in ROLLOUT_MODES:
        raise BlackboardContractError(f"unknown_rollout_mode:{normalized}")
    if normalized == "pull_live" and not allow_live:
        raise BlackboardContractError("pull_live_requires_explicit_opt_in")
    return normalized


def build_evidence_blackboard(
    board: dict[str, Any],
    subagent_projection: dict[str, Any] | None = None,
    *,
    mode: str = "blackboard_dry_run",
) -> dict[str, Any]:
    selected_mode = resolve_rollout_mode(mode)
    projection = (
        subagent_projection
        if isinstance(subagent_projection, dict)
        else board.get("opencode_subagent_projection")
        if isinstance(board.get("opencode_subagent_projection"), dict)
        else {}
    )
    domains = board.get("domain_artifacts") if isinstance(board.get("domain_artifacts"), dict) else {}
    rows = domains.get("domains") if isinstance(domains, dict) else []
    evidence_domains: dict[str, dict[str, Any]] = {}
    legacy_row_count = 0
    warnings: list[str] = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized = normalize_domain_row(row)
            if not normalized:
                continue
            legacy_row_count += 1
            evidence_domains[normalized["evidence_domain"]] = normalized
            if normalized.get("compatibility_warning"):
                warnings.append(str(normalized["compatibility_warning"]))
    projection_row_count = 0
    for row in _projection_domain_rows(projection):
        normalized = normalize_domain_row(row)
        if not normalized:
            continue
        projection_row_count += 1
        evidence_domains.setdefault(normalized["evidence_domain"], normalized)
        if normalized.get("compatibility_warning"):
            warnings.append(str(normalized["compatibility_warning"]))
    value_summary = projection.get("value_summary") if isinstance(projection.get("value_summary"), dict) else {}
    return normalize_blackboard_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "blackboard_kind": BLACKBOARD_KIND,
            "mode": selected_mode,
            "actuation_enabled": False,
            "evidence_domains": evidence_domains,
            "value_summary": dict(value_summary),
            "compatibility": {
                "legacy_domain_rows": legacy_row_count,
                "projection_subagent_rows": projection_row_count,
                "warnings": warnings,
            },
        }
    )


def normalize_blackboard_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BlackboardContractError("blackboard_payload_must_be_object")
    schema_version = int(payload.get("schema_version", SCHEMA_VERSION) or 0)
    if schema_version != SCHEMA_VERSION:
        raise BlackboardContractError(f"unsupported_blackboard_schema_version:{schema_version}")
    kind = str(payload.get("blackboard_kind") or BLACKBOARD_KIND).strip()
    if kind != BLACKBOARD_KIND:
        raise BlackboardContractError(f"unsupported_blackboard_kind:{kind}")
    mode = resolve_rollout_mode(str(payload.get("mode") or "blackboard_dry_run"))
    domains = payload.get("evidence_domains") if isinstance(payload.get("evidence_domains"), dict) else {}
    normalized_domains = {}
    for domain_id, row in domains.items():
        if not isinstance(row, dict):
            continue
        normalized = normalize_domain_row({"evidence_domain": domain_id, **row})
        if normalized:
            normalized_domains[normalized["evidence_domain"]] = normalized
    value_summary = payload.get("value_summary") if isinstance(payload.get("value_summary"), dict) else {}
    compatibility = payload.get("compatibility") if isinstance(payload.get("compatibility"), dict) else {}
    domain_state_summary = _domain_state_summary(normalized_domains)
    return {
        "schema_version": schema_version,
        "blackboard_kind": kind,
        "mode": mode,
        "actuation_enabled": False,
        "evidence_domains": normalized_domains,
        "domain_state_summary": domain_state_summary,
        "value_summary": dict(value_summary),
        "compatibility": dict(compatibility),
    }


def normalize_blackboard_event(event: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise BlackboardContractError("blackboard_event_must_be_object")
    schema_version = int(event.get("schema_version", SCHEMA_VERSION) or 0)
    if schema_version != SCHEMA_VERSION:
        raise BlackboardContractError(f"unsupported_blackboard_event_schema_version:{schema_version}")
    event_id = _required_event_str(event, "event_id")
    revision = _event_revision(event.get("revision"))
    event_type = _required_event_str(event, "event_type")
    if event_type not in BLACKBOARD_EVENT_TYPES:
        raise BlackboardContractError(f"unknown_blackboard_event_type:{event_type}")
    entity_type = _required_event_str(event, "entity_type")
    if entity_type not in BLACKBOARD_ENTITY_TYPES:
        raise BlackboardContractError(f"unknown_blackboard_entity_type:{entity_type}")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise BlackboardContractError(f"blackboard_event {event_id} payload must be an object")
    return {
        "schema_version": schema_version,
        "event_kind": str(event.get("event_kind") or BLACKBOARD_EVENT_KIND),
        "event_id": event_id,
        "revision": revision,
        "created_at": _required_event_str(event, "created_at"),
        "producer": _required_event_str(event, "producer"),
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": _required_event_str(event, "entity_id"),
        "payload": dict(payload),
    }


def build_blackboard_snapshot(events: list[dict[str, Any]]) -> dict[str, Any]:
    ordered_events = _dedupe_and_order_blackboard_events(events)
    domain_rows: dict[str, dict[str, Any]] = {}
    last_terminal_status_by_entity: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    for event in ordered_events:
        row = _domain_row_from_event(event)
        if not row:
            continue
        entity_key = f"{event['entity_type']}:{event['entity_id']}"
        new_status = str(row.get("source_status") or "")
        previous_terminal = last_terminal_status_by_entity.get(entity_key, "")
        if (
            previous_terminal in TERMINAL_EVENT_STATUSES
            and (
                new_status not in TERMINAL_EVENT_STATUSES
                or previous_terminal != new_status
            )
        ):
            conflicts.append(
                {
                    "entity_type": event["entity_type"],
                    "entity_id": event["entity_id"],
                    "previous_status": previous_terminal,
                    "new_status": new_status,
                    "event_id": event["event_id"],
                }
            )
            row["source_status"] = "conflicted"
            new_status = "conflicted"
        if new_status in TERMINAL_EVENT_STATUSES:
            last_terminal_status_by_entity[entity_key] = new_status
        evidence_domain = str(row.get("evidence_domain") or "")
        if evidence_domain:
            domain_rows[evidence_domain] = {**domain_rows.get(evidence_domain, {}), **row}

    blackboard = normalize_blackboard_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "blackboard_kind": BLACKBOARD_KIND,
            "mode": "blackboard_dry_run",
            "evidence_domains": domain_rows,
            "compatibility": {
                "source": "blackboard_event_log",
                "source_event_count": len(ordered_events),
                "conflict_count": len(conflicts),
            },
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_kind": BLACKBOARD_SNAPSHOT_KIND,
        "validation_status": "valid",
        "source_event_count": len(ordered_events),
        "source_event_ids": [event["event_id"] for event in ordered_events],
        "last_revision": max((int(event["revision"]) for event in ordered_events), default=0),
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "blackboard": blackboard,
    }


def validate_blackboard_snapshot(snapshot: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("snapshot_kind") != BLACKBOARD_SNAPSHOT_KIND:
        return {"ok": False, "status": "invalid_snapshot"}
    expected = build_blackboard_snapshot(events)
    expected_ids = list(expected.get("source_event_ids", []))
    actual_ids = list(snapshot.get("source_event_ids", [])) if isinstance(snapshot.get("source_event_ids"), list) else []
    expected_count = int(expected.get("source_event_count") or 0)
    actual_count = int(snapshot.get("source_event_count") or 0)
    expected_revision = int(expected.get("last_revision") or 0)
    actual_revision = int(snapshot.get("last_revision") or 0)
    if actual_ids != expected_ids or actual_count != expected_count or actual_revision != expected_revision:
        return {
            "ok": False,
            "status": "stale_snapshot",
            "expected_source_event_count": expected_count,
            "actual_source_event_count": actual_count,
            "expected_last_revision": expected_revision,
            "actual_last_revision": actual_revision,
        }
    return {
        "ok": True,
        "status": "valid",
        "expected_source_event_count": expected_count,
        "actual_source_event_count": actual_count,
        "expected_last_revision": expected_revision,
        "actual_last_revision": actual_revision,
    }


def _dedupe_and_order_blackboard_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        raise BlackboardContractError("blackboard_events_must_be_list")
    by_event_id: dict[str, dict[str, Any]] = {}
    for raw_event in events:
        event = normalize_blackboard_event(raw_event)
        event_id = str(event["event_id"])
        previous = by_event_id.get(event_id)
        if previous is not None:
            if previous != event:
                raise BlackboardContractError(f"conflicting_duplicate_event_id:{event_id}")
            continue
        by_event_id[event_id] = event
    return sorted(by_event_id.values(), key=lambda item: (int(item["revision"]), str(item["event_id"])))


def _domain_row_from_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    evidence_domain = str(payload.get("evidence_domain") or event.get("entity_id") or "").strip()
    if not evidence_domain:
        return {}
    event_type = str(event.get("event_type") or "")
    source_status = str(payload.get("status") or EVENT_STATUS_BY_TYPE.get(event_type) or "unknown_retrieval")
    return {
        "evidence_domain": evidence_domain,
        "required": bool(payload.get("required")),
        "source_status": source_status,
        "worker_id": str(payload.get("worker_id") or payload.get("worker") or ""),
        "source": "blackboard_event_log",
        "last_event_id": str(event.get("event_id") or ""),
        "last_revision": int(event.get("revision") or 0),
        "event_type": event_type,
        "producer": str(event.get("producer") or ""),
    }


def _required_event_str(event: dict[str, Any], field: str) -> str:
    value = str(event.get(field) or "").strip()
    if not value:
        raise BlackboardContractError(f"blackboard_event_missing_{field}")
    return value


def _event_revision(value: Any) -> int:
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        raise BlackboardContractError("blackboard_event_revision_must_be_positive_integer") from exc
    if revision <= 0:
        raise BlackboardContractError("blackboard_event_revision_must_be_positive_integer")
    return revision


def normalize_domain_row(row: dict[str, Any]) -> dict[str, Any]:
    evidence_domain = str(row.get("evidence_domain") or row.get("domain") or "").strip()
    if not evidence_domain:
        return {}
    source_status = str(row.get("source_status") or row.get("status") or row.get("state") or "").strip()
    missing = list(row.get("missing") or []) if isinstance(row.get("missing"), list) else []
    status = canonical_domain_status(source_status, missing=missing)
    requirement_state = "required" if bool(row.get("required")) else "optional"
    availability_state = _availability_state(status)
    consumption_state = _consumption_state(row)
    result = {
        "evidence_domain": evidence_domain,
        "required": bool(row.get("required")),
        "status": status,
        "source_status": source_status,
        "missing": missing,
        "worker_id": str(row.get("worker_id") or row.get("worker") or ""),
        "requirement_state": requirement_state,
        "availability_state": availability_state,
        "consumption_state": consumption_state,
        "state_tags": _state_tags(requirement_state, availability_state, consumption_state),
    }
    for key in (
        "source",
        "subagent_id",
        "agent",
        "lane",
        "parent_worker_id",
        "parent_run_id",
        "parent_task_id",
        "downstream_consumer",
        "expected_unique_value",
        "unique_value_realized",
        "last_event_id",
        "event_type",
        "producer",
    ):
        value = str(row.get(key) or "").strip()
        if value:
            result[key] = value
    if row.get("last_revision") not in (None, ""):
        result["last_revision"] = int(row.get("last_revision") or 0)
    if source_status and source_status != status:
        result["legacy_status_mapped"] = True
    if status == "unknown_retrieval" and source_status and source_status not in CANONICAL_DOMAIN_STATUSES:
        result["compatibility_warning"] = f"unknown_domain_status_mapped:{source_status}"
    return result


def canonical_domain_status(source_status: str, *, missing: list[Any] | None = None) -> str:
    normalized = str(source_status or "").strip()
    if normalized in CANONICAL_DOMAIN_STATUSES:
        return normalized
    if normalized in STATUS_ALIASES:
        return STATUS_ALIASES[normalized]
    if missing:
        return "missing"
    return "unknown_retrieval"


def _projection_domain_rows(projection: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    tasks = projection.get("tasks") if isinstance(projection.get("tasks"), list) else []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        subagents = task.get("subagents") if isinstance(task.get("subagents"), list) else []
        for subagent in subagents:
            if not isinstance(subagent, dict):
                continue
            row = _projection_domain_row(task, subagent)
            if row:
                rows.append(row)
    return rows


def _projection_domain_row(task: dict[str, Any], subagent: dict[str, Any]) -> dict[str, Any]:
    evidence_domain = str(subagent.get("subdomain_id") or subagent.get("lane") or subagent.get("subagent_id") or "").strip()
    if not evidence_domain:
        return {}
    required_or_optional = str(subagent.get("required_or_optional") or "").strip()
    return {
        "evidence_domain": evidence_domain,
        "required": required_or_optional == "required",
        "source_status": _source_status_from_subagent(subagent),
        "worker_id": str(subagent.get("parent_worker_id") or task.get("parent_worker_id") or ""),
        "source": "opencode_subagent_projection",
        "subagent_id": str(subagent.get("subagent_id") or ""),
        "agent": str(subagent.get("agent") or ""),
        "lane": str(subagent.get("lane") or ""),
        "parent_worker_id": str(subagent.get("parent_worker_id") or task.get("parent_worker_id") or ""),
        "parent_run_id": str(subagent.get("parent_run_id") or task.get("run") or ""),
        "parent_task_id": str(subagent.get("parent_task_id") or task.get("task_id") or ""),
        "downstream_consumer": str(subagent.get("downstream_consumer") or ""),
        "expected_unique_value": str(subagent.get("expected_unique_value") or ""),
        "unique_value_realized": str(subagent.get("unique_value_realized") or ""),
        "merge_status": str(subagent.get("merge_status") or ""),
        "consumed": str(subagent.get("merge_status") or "") == "merged",
    }


def _source_status_from_subagent(subagent: dict[str, Any]) -> str:
    merge_status = str(subagent.get("merge_status") or "")
    retrieval_state = str(subagent.get("retrieval_state") or "")
    value_classification = str(subagent.get("value_classification") or "")
    merge_readiness = str(subagent.get("merge_readiness") or "")
    status = str(subagent.get("status") or "")
    if bool(subagent.get("direct_path_converged_first")) or value_classification == "direct_path_converged_first":
        return "direct_path_converged_first"
    if merge_status == "merged":
        return "merged"
    if retrieval_state:
        return retrieval_state
    if merge_readiness:
        return merge_readiness
    if status in {"created", "launched", "running", "waiting_input"}:
        return "running"
    if status in {"blocked", "failed"}:
        return "blocked"
    if status == "completed":
        return "ready"
    return status


def _availability_state(status: str) -> str:
    if status == "ready":
        return "present"
    if status == "missing":
        return "missing"
    if status == "running":
        return "running"
    if status == "conflicted":
        return "blocked"
    if status in {"expired", "unknown_retrieval"}:
        return "stale"
    return "unknown"


def _consumption_state(row: dict[str, Any]) -> str:
    explicit = str(row.get("consumption_state") or "").strip()
    if explicit in {"consumed", "unconsumed"}:
        return explicit
    if _truthy(row.get("consumed")) or str(row.get("merge_status") or "").strip() == "merged":
        return "consumed"
    return "unconsumed"


def _state_tags(requirement_state: str, availability_state: str, consumption_state: str) -> list[str]:
    tags = [requirement_state]
    tags.append(availability_state if availability_state in DOMAIN_STATE_SUMMARY_KEYS else "unknown")
    if consumption_state == "consumed":
        tags.append("consumed")
    return tags


def _domain_state_summary(domains: dict[str, dict[str, Any]]) -> dict[str, int]:
    summary = {key: 0 for key in DOMAIN_STATE_SUMMARY_KEYS}
    for row in domains.values():
        tags = row.get("state_tags") if isinstance(row.get("state_tags"), list) else []
        for tag in tags:
            key = str(tag or "")
            if key in summary:
                summary[key] += 1
    return summary


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
