"""Fail-closed run manifests, supervision policies, and authorization grants."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.collaboration_profile import (
    collaboration_profile_digest,
    profile_edge,
)


SCHEMA_VERSION = 1
MODES = {"observe_only", "shadow", "preauthorized_followups"}
RETRYABLE_CATEGORIES = {"observation_read_error", "receipt_reconciliation"}
GRANT_STATES = {"active", "paused", "draining", "revoked"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_REVISION = re.compile(r"^[0-9a-f]{40}$")


class SupervisionPolicyError(ValueError):
    """Raised when a supervisor contract is invalid or grants too much."""


def load_run_manifest(path_value: Path | str, *, profile: dict[str, Any]) -> dict[str, Any]:
    return validate_run_manifest(_load_json_object(path_value, "run manifest"), profile=profile)


def load_supervision_policy(
    path_value: Path | str,
    *,
    profile: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    return validate_supervision_policy(
        _load_json_object(path_value, "supervision policy"),
        profile=profile,
        manifest=manifest,
    )


def load_authorization_grant(
    path_value: Path | str,
    *,
    manifest: dict[str, Any],
    policy: dict[str, Any],
    now: str | None = None,
) -> dict[str, Any]:
    return validate_authorization_grant(
        _load_json_object(path_value, "authorization grant"),
        manifest=manifest,
        policy=policy,
        now=now,
    )


def validate_run_manifest(
    payload: object,
    *,
    profile: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SupervisionPolicyError("run manifest must be a JSON object")
    _schema_version(payload, "run manifest")
    run_id = _safe_id(payload.get("run_id"), "run_id")
    workflow_revision = _safe_id(payload.get("workflow_revision"), "workflow_revision")
    workflow_digest = _digest(payload.get("workflow_digest"), "workflow_digest")
    profile_id = str(profile.get("profile_id") or "")
    profile_digest = collaboration_profile_digest(profile)
    if str(payload.get("collaboration_profile_id") or "") != profile_id:
        raise SupervisionPolicyError("run manifest collaboration_profile_id mismatch")
    if str(payload.get("collaboration_profile_digest") or "") != profile_digest:
        raise SupervisionPolicyError("run manifest collaboration_profile_digest mismatch")

    nodes = {
        str(node.get("node_id") or ""): node
        for node in profile.get("nodes", [])
        if isinstance(node, dict)
    }
    lanes_raw = payload.get("lanes")
    if not isinstance(lanes_raw, list) or not lanes_raw:
        raise SupervisionPolicyError("run manifest lanes must be non-empty")
    lanes: list[dict[str, str]] = []
    lane_nodes: set[str] = set()
    for raw in lanes_raw:
        if not isinstance(raw, dict):
            raise SupervisionPolicyError("run manifest lane must be an object")
        node_id = _safe_id(raw.get("node_id"), "lane node_id")
        if node_id not in nodes:
            raise SupervisionPolicyError(f"run manifest lane node is unknown: {node_id}")
        if node_id in lane_nodes:
            raise SupervisionPolicyError(f"run manifest lane node is duplicated: {node_id}")
        lane = {
            "node_id": node_id,
            "lane_id": _safe_id(raw.get("lane_id"), "lane_id"),
            "attempt_id": _safe_id(raw.get("attempt_id"), "attempt_id"),
            "task_contract_digest": _digest(
                raw.get("task_contract_digest"),
                "task_contract_digest",
            ),
        }
        task_dir = str(raw.get("task_dir") or "").strip()
        if task_dir:
            if Path(task_dir).is_absolute() or ".." in Path(task_dir).parts:
                raise SupervisionPolicyError("run manifest lane task_dir is unsafe")
            lane["task_dir"] = task_dir
        lanes.append(lane)
        lane_nodes.add(node_id)

    adoption = _normalize_adoption(
        payload.get("adoption"),
        workflow_revision=workflow_revision,
        workflow_digest=workflow_digest,
    )
    targets = _normalize_targets(payload.get("targets"), nodes, profile)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "workflow_revision": workflow_revision,
        "workflow_digest": workflow_digest,
        "collaboration_profile_id": profile_id,
        "collaboration_profile_digest": profile_digest,
        "adoption": adoption,
        "lanes": lanes,
        "targets": targets,
    }


def run_manifest_digest(manifest: dict[str, Any]) -> str:
    return _canonical_digest(manifest)


def validate_supervision_policy(
    payload: object,
    *,
    profile: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SupervisionPolicyError("supervision policy must be a JSON object")
    _schema_version(payload, "supervision policy")
    policy_id = _safe_id(payload.get("policy_id"), "policy_id")
    if str(payload.get("run_manifest_digest") or "") != run_manifest_digest(manifest):
        raise SupervisionPolicyError("supervision policy run_manifest_digest mismatch")
    if str(payload.get("collaboration_profile_id") or "") != manifest["collaboration_profile_id"]:
        raise SupervisionPolicyError("supervision policy collaboration_profile_id mismatch")
    if str(payload.get("collaboration_profile_digest") or "") != manifest["collaboration_profile_digest"]:
        raise SupervisionPolicyError("supervision policy collaboration_profile_digest mismatch")
    if str(payload.get("workflow_revision") or "") != manifest["workflow_revision"]:
        raise SupervisionPolicyError("supervision policy workflow_revision mismatch")
    mode = _enum(payload.get("mode"), MODES, "mode")
    observation_limits = _limits(
        payload.get("observation_limits"),
        {"max_cycles", "max_elapsed_ms"},
        "observation_limits",
    )
    actuation_limits = _limits(
        payload.get("actuation_limits"),
        {
            "max_actions",
            "max_worker_dispatches",
            "max_auto_hops",
            "max_reconciliations",
        },
        "actuation_limits",
    )
    candidate_edges = _candidate_edges(payload.get("candidate_edges"), profile)
    execution_authorization = _execution_authorization(
        payload.get("execution_authorization"),
        candidate_edges,
        mode,
    )
    _validate_worker_targets(
        profile=profile,
        manifest=manifest,
        allowed_edge_ids=execution_authorization["allowed_edge_ids"],
    )
    retry_policy = _retry_policy(payload.get("retry_policy"))
    escalate_on = _string_set(payload.get("escalate_on"), "escalate_on")
    required_escalations = {"terminal_conflict", "delivery_uncertain", "budget_exhausted"}
    if not required_escalations.issubset(escalate_on):
        raise SupervisionPolicyError("supervision policy escalate_on is incomplete")
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_id": policy_id,
        "run_manifest_digest": run_manifest_digest(manifest),
        "collaboration_profile_id": manifest["collaboration_profile_id"],
        "collaboration_profile_digest": manifest["collaboration_profile_digest"],
        "workflow_revision": manifest["workflow_revision"],
        "mode": mode,
        "observation_limits": observation_limits,
        "actuation_limits": actuation_limits,
        "candidate_edges": candidate_edges,
        "execution_authorization": execution_authorization,
        "retry_policy": retry_policy,
        "escalate_on": sorted(escalate_on),
    }


def supervision_policy_digest(policy: dict[str, Any]) -> str:
    return _canonical_digest(policy)


def validate_authorization_grant(
    payload: object,
    *,
    manifest: dict[str, Any],
    policy: dict[str, Any],
    now: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SupervisionPolicyError("authorization grant must be a JSON object")
    _schema_version(payload, "authorization grant")
    authorization_id = _safe_id(payload.get("authorization_id"), "authorization_id")
    if str(payload.get("run_id") or "") != manifest["run_id"]:
        raise SupervisionPolicyError("authorization grant run_id mismatch")
    if str(payload.get("workflow_revision") or "") != manifest["workflow_revision"]:
        raise SupervisionPolicyError("authorization grant workflow_revision mismatch")
    allowed_edge_ids = _string_set(payload.get("allowed_edge_ids"), "allowed_edge_ids")
    policy_allowed = set(policy["execution_authorization"]["allowed_edge_ids"])
    if not allowed_edge_ids.issubset(policy_allowed):
        raise SupervisionPolicyError("authorization grant allowed_edge_ids outside policy")
    max_actions = _positive_int(payload.get("max_actions"), "authorization grant max_actions")
    if max_actions > int(policy["actuation_limits"]["max_actions"]):
        raise SupervisionPolicyError("authorization grant max_actions exceeds policy")
    state = _enum(payload.get("state"), GRANT_STATES, "authorization grant state")
    expires_at = _timestamp(payload.get("expires_at"), "authorization grant expires_at")
    policy_digest = str(payload.get("policy_digest") or "")
    if policy_digest != supervision_policy_digest(policy):
        raise SupervisionPolicyError("authorization grant policy_digest mismatch")
    current = _timestamp(now, "now") if now else datetime.now(timezone.utc)
    if state == "active" and expires_at <= current:
        raise SupervisionPolicyError("authorization grant expired")
    return {
        "schema_version": SCHEMA_VERSION,
        "authorization_id": authorization_id,
        "run_id": manifest["run_id"],
        "workflow_revision": manifest["workflow_revision"],
        "policy_digest": policy_digest,
        "allowed_edge_ids": sorted(allowed_edge_ids),
        "max_actions": max_actions,
        "state": state,
        "expires_at": expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def authorization_grant_allows(grant: dict[str, Any], edge_id: str) -> bool:
    return (
        str(grant.get("state") or "") == "active"
        and edge_id in set(grant.get("allowed_edge_ids") or [])
    )


def _normalize_adoption(
    value: object,
    *,
    workflow_revision: str,
    workflow_digest: str,
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SupervisionPolicyError("run manifest adoption must be an object")
    status = _enum(
        value.get("status"),
        {"workflow_revision_ready", "awaiting_main_adoption", "awaiting_main_revision"},
        "run manifest adoption status",
    )
    adoption = {
        "status": status,
        "adoption_decision_id": _safe_id(
            value.get("adoption_decision_id"),
            "adoption_decision_id",
        ),
        "workflow_revision": str(value.get("workflow_revision") or ""),
        "workflow_digest": str(value.get("workflow_digest") or ""),
    }
    if status == "workflow_revision_ready" and (
        adoption["workflow_revision"] != workflow_revision
        or adoption["workflow_digest"] != workflow_digest
    ):
        raise SupervisionPolicyError("run manifest adoption does not freeze workflow revision")
    return adoption


def _normalize_targets(
    value: object,
    nodes: dict[str, dict[str, Any]],
    profile: dict[str, Any],
) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SupervisionPolicyError("run manifest targets must be an object")
    normalized: dict[str, dict[str, str]] = {}
    worker_target_effects: dict[str, set[str]] = {}
    for edge in profile.get("edges", []):
        if not isinstance(edge, dict):
            continue
        effect_class = str(edge.get("effect_class") or "")
        if effect_class not in {"read_only_worker", "write_isolated", "shared_write"}:
            continue
        target_node = str(edge.get("target_node") or "")
        worker_target_effects.setdefault(target_node, set()).add(effect_class)
    for node_id, raw in value.items():
        target_id = _safe_id(node_id, "target node_id")
        if target_id not in nodes:
            raise SupervisionPolicyError(f"run manifest target node is unknown: {target_id}")
        if not isinstance(raw, dict):
            raise SupervisionPolicyError("run manifest target must be an object")
        worker_id = _safe_id(raw.get("worker_id"), "target worker_id")
        task_title = " ".join(str(raw.get("task_title") or "").split())
        task_body = str(raw.get("task_body") or "")
        if not task_title or not task_body:
            raise SupervisionPolicyError("run manifest target task template is missing")
        target = {
            "worker_id": worker_id,
            "task_title": task_title,
            "task_body": task_body,
        }
        effects = worker_target_effects.get(target_id, set())
        if effects:
            session_context = str(
                raw.get("session_context_requirement") or ""
            )
            if session_context != "fresh_idle":
                raise SupervisionPolicyError(
                    "run manifest target session_context_requirement must be fresh_idle"
                )
            target["session_context_requirement"] = session_context
        if "write_isolated" in effects:
            target["isolated_write"] = normalize_isolated_write_contract(
                raw.get("isolated_write")
            )
        elif raw.get("isolated_write") is not None:
            raise SupervisionPolicyError(
                "isolated_write target config requires a write_isolated edge"
            )
        normalized[target_id] = target
    return normalized


def normalize_isolated_write_contract(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SupervisionPolicyError("isolated_write must be an object")
    required = {
        "worktree_path",
        "shared_checkout_path",
        "base_revision",
        "ownership_paths",
        "test_command",
        "artifact_limits",
    }
    if set(value) != required:
        raise SupervisionPolicyError("isolated_write fields are invalid")
    worktree_path = Path(str(value.get("worktree_path") or "")).expanduser()
    shared_checkout_path = Path(
        str(value.get("shared_checkout_path") or "")
    ).expanduser()
    if not worktree_path.is_absolute() or not shared_checkout_path.is_absolute():
        raise SupervisionPolicyError("isolated_write paths must be absolute")
    if worktree_path == shared_checkout_path:
        raise SupervisionPolicyError(
            "isolated_write worktree_path must differ from shared_checkout_path"
        )
    base_revision = str(value.get("base_revision") or "").strip()
    if not GIT_REVISION.fullmatch(base_revision):
        raise SupervisionPolicyError("isolated_write base_revision is invalid")
    ownership_paths = _isolated_relative_paths(value.get("ownership_paths"))
    test_command = _isolated_test_command(value.get("test_command"))
    limits = value.get("artifact_limits")
    if not isinstance(limits, dict) or set(limits) != {
        "max_changed_files",
        "max_diff_bytes",
    }:
        raise SupervisionPolicyError("isolated_write artifact_limits are invalid")
    return {
        "worktree_path": str(worktree_path),
        "shared_checkout_path": str(shared_checkout_path),
        "base_revision": base_revision,
        "ownership_paths": ownership_paths,
        "test_command": test_command,
        "artifact_limits": {
            "max_changed_files": _positive_int(
                limits.get("max_changed_files"),
                "isolated_write max_changed_files",
            ),
            "max_diff_bytes": _positive_int(
                limits.get("max_diff_bytes"),
                "isolated_write max_diff_bytes",
            ),
        },
    }


def _isolated_relative_paths(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SupervisionPolicyError("isolated_write ownership_paths are invalid")
    paths: list[str] = []
    for raw_path in value:
        path = Path(str(raw_path or ""))
        text = path.as_posix()
        if (
            not text
            or path.is_absolute()
            or ".." in path.parts
            or ".git" in path.parts
            or text == "."
        ):
            raise SupervisionPolicyError(
                "isolated_write ownership_paths are invalid"
            )
        paths.append(text)
    if len(set(paths)) != len(paths):
        raise SupervisionPolicyError("isolated_write ownership_paths are invalid")
    return paths


def _isolated_test_command(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SupervisionPolicyError("isolated_write test_command is invalid")
    command = [str(item or "") for item in value]
    if any(not item or "\n" in item or "\r" in item for item in command):
        raise SupervisionPolicyError("isolated_write test_command is invalid")
    return command


def _candidate_edges(value: object, profile: dict[str, Any]) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SupervisionPolicyError("supervision policy candidate_edges must be non-empty")
    edges: list[str] = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"edge_id"}:
            raise SupervisionPolicyError(
                "supervision policy candidate_edges must contain edge references only"
            )
        edge_id = _safe_id(raw.get("edge_id"), "candidate edge_id")
        profile_edge(profile, edge_id)
        if edge_id in edges:
            raise SupervisionPolicyError("supervision policy candidate_edges must be unique")
        edges.append(edge_id)
    return edges


def _execution_authorization(
    value: object,
    candidate_edges: list[str],
    mode: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SupervisionPolicyError("execution_authorization must be an object")
    enabled = value.get("enabled")
    if type(enabled) is not bool:
        raise SupervisionPolicyError("execution_authorization enabled must be boolean")
    allowed = _string_set(value.get("allowed_edge_ids"), "allowed_edge_ids")
    if not allowed.issubset(set(candidate_edges)):
        raise SupervisionPolicyError("execution_authorization allowed_edge_ids outside candidates")
    if mode != "preauthorized_followups" and (enabled or allowed):
        raise SupervisionPolicyError("observe/shadow mode cannot enable execution authorization")
    if mode == "preauthorized_followups" and not enabled:
        raise SupervisionPolicyError("preauthorized_followups requires execution authorization")
    return {"enabled": enabled, "allowed_edge_ids": sorted(allowed)}


def _validate_worker_targets(
    *,
    profile: dict[str, Any],
    manifest: dict[str, Any],
    allowed_edge_ids: list[str],
) -> None:
    targets = manifest.get("targets") if isinstance(manifest.get("targets"), dict) else {}
    for edge_id in allowed_edge_ids:
        edge = profile_edge(profile, edge_id)
        if edge.get("effect_class") not in {
            "read_only_worker",
            "write_isolated",
        }:
            continue
        target = targets.get(str(edge.get("target_node") or ""))
        if not isinstance(target, dict):
            raise SupervisionPolicyError(
                f"preauthorized worker edge {edge_id} requires a manifest target"
            )
        if target.get("session_context_requirement") != "fresh_idle":
            raise SupervisionPolicyError(
                f"preauthorized worker edge {edge_id} requires fresh_idle session context"
            )


def _retry_policy(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SupervisionPolicyError("retry_policy must be an object")
    categories = _string_set(value.get("retryable_categories"), "retryable_categories")
    if not categories.issubset(RETRYABLE_CATEGORIES):
        raise SupervisionPolicyError("retry_policy category is invalid")
    attempts = _nonnegative_int(value.get("max_attempts_per_lane"), "retry_policy max_attempts_per_lane")
    return {"retryable_categories": sorted(categories), "max_attempts_per_lane": attempts}


def _limits(value: object, keys: set[str], field: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != keys:
        raise SupervisionPolicyError(f"{field} fields are invalid")
    return {key: _positive_int(value[key], f"{field} {key}") for key in sorted(keys)}


def _load_json_object(path_value: Path | str, label: str) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupervisionPolicyError(f"{label} must be valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise SupervisionPolicyError(f"{label} must be a JSON object")
    return payload


def _canonical_digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _schema_version(payload: dict[str, Any], label: str) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SupervisionPolicyError(f"{label} schema_version must be {SCHEMA_VERSION}")


def _safe_id(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text):
        raise SupervisionPolicyError(f"{field} is invalid")
    return text


def _digest(value: object, field: str) -> str:
    text = str(value or "")
    if not SHA256.fullmatch(text):
        raise SupervisionPolicyError(f"{field} must be a sha256 digest")
    return text


def _enum(value: object, allowed: set[str], field: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise SupervisionPolicyError(f"{field} is invalid")
    return text


def _string_set(value: object, field: str) -> set[str]:
    if not isinstance(value, list):
        raise SupervisionPolicyError(f"{field} must be a list")
    normalized = {_safe_id(item, field) for item in value}
    if len(normalized) != len(value):
        raise SupervisionPolicyError(f"{field} must be unique")
    return normalized


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise SupervisionPolicyError(f"{field} must be a positive integer")
    return int(value)


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise SupervisionPolicyError(f"{field} must be a non-negative integer")
    return int(value)


def _timestamp(value: object, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupervisionPolicyError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise SupervisionPolicyError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)
