"""No-live OpenCode mixed-ready registry update path.

This module promotes one OpenCode visible worker to sandbox/no-op mixed-ready
from validated OpenCode mixed-readiness evidence. It never executes live work,
dispatches run-one, writes registered-worker registries, touches .omx/state,
or enables business readiness.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.opencode_visible_registry import (
    VisibleRegistryConflict,
    mutate_visible_registry,
)
from visible_agent_orchestrator.runtime_paths import workspace_root

from .opencode_mixed_readiness_evidence import (
    validate_opencode_mixed_readiness_evidence_file,
)


SCHEMA_VERSION = 1
UPDATE_KIND = "opencode-mixed-ready-registry-update"
BACKEND_KIND = "opencode_tui"
MIXED_READY_SCOPE = "sandbox_noop_only"
ALLOWED_MIXED_TASK_KINDS = ["mixed_noop_sandbox", "mixed_terminal_protocol_smoke"]
BUSINESS_REPO_ROOT = str(workspace_root())

ALLOWED_CHANGED_FIELDS = {
    "allow_mixed_worker",
    "allowed_mixed_task_kinds",
    "blocked_smoke_evidence_ok",
    "business_ready",
    "done_smoke_evidence_ok",
    "evidence_collected",
    "fail_smoke_evidence_ok",
    "last_mixed_readiness_check",
    "mixed_ready",
    "mixed_ready_evidence_path",
    "mixed_ready_evidence_sha256",
    "mixed_ready_scope",
    "mixed_ready_updated_at",
    "mixed_ready_updated_by",
    "settle_evidence_ok",
    "worker_scoped_lock_evidence_ok",
}
FORBIDDEN_PRESERVED_FIELDS = {
    "allow_auto_submit",
    "backend",
    "backend_kind",
    "binding_confidence",
    "binding_evidence",
    "default_for_new_tasks",
    "host",
    "listener_pids",
    "opencode_pids",
    "pane_current_command",
    "pane_current_path",
    "pane_id",
    "pane_pid",
    "port",
    "strong_pid_matches",
    "user_started",
    "worker_id",
}


@dataclass(frozen=True)
class OpenCodeMixedReadyUpdateRequest:
    root: str
    worker_id: str
    opencode_visible_registry_path: str
    evidence_path: str
    allow_framework_registry: bool = False
    allow_opencode_mixed_ready_update: bool = False
    proposed_overrides: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_opencode_mixed_ready_update_plan(request: OpenCodeMixedReadyUpdateRequest) -> dict[str, Any]:
    root = Path(request.root).resolve()
    registry_path = Path(request.opencode_visible_registry_path).resolve()
    evidence_path = Path(request.evidence_path).resolve()
    registry_payload = _read_json(registry_path) if registry_path.exists() else {}
    current = _find_worker(registry_payload, request.worker_id)
    evidence_payload = _read_json(evidence_path) if evidence_path.exists() else {}
    evidence_validation = (
        validate_opencode_mixed_readiness_evidence_file(evidence_path).to_dict()
        if evidence_path.exists()
        else {"ok": False, "blocked_reasons": [f"evidence missing: {evidence_path}"]}
    )

    blocked: list[str] = []
    if not registry_path.exists():
        blocked.append(f"registry missing: {registry_path}")
    if not evidence_path.exists():
        blocked.append(f"evidence missing: {evidence_path}")
    if not _is_framework_opencode_visible_registry(root, registry_path):
        blocked.append(f"registry path outside allowed OpenCode visible framework registry: {registry_path}")
    if "registered-workers" in registry_path.parts:
        blocked.append("registered worker registry path refused")
    if ".omx/state/team" in registry_path.as_posix():
        blocked.append(".omx/state/team registry path refused")
    if _paths_overlap(str(registry_path), BUSINESS_REPO_ROOT):
        blocked.append("business repo path refused")
    if registry_path.name in {"AGENTS.md", "VISIBLE_LEADER_PROMPT_CURRENT.md"}:
        blocked.append("policy/prompt file path refused")
    if not current:
        blocked.append(f"worker missing: {request.worker_id}")
    backend = str(current.get("backend", current.get("backend_kind", ""))) if current else ""
    if current and backend != BACKEND_KIND:
        blocked.append(f"target worker backend must be {BACKEND_KIND}")
    if evidence_validation.get("ok") is not True:
        blocked.append("evidence validation ok=false")
    if evidence_validation.get("mixed_ready_candidate") is not True:
        blocked.append("mixed_ready_candidate != true")
    if evidence_validation.get("business_ready_candidate") is not False:
        blocked.append("business_ready_candidate != false")
    if evidence_payload.get("business_ready_candidate") is True:
        blocked.append("evidence payload business_ready_candidate=true is forbidden")
    if str(evidence_payload.get("worker_id", "")) != request.worker_id:
        blocked.append(f"evidence worker_id mismatch: {evidence_payload.get('worker_id', '')} != {request.worker_id}")
    if evidence_validation.get("would_update_registry") is not False:
        blocked.append("evidence would_update_registry must be false")
    for key in ("done_smoke_ok", "blocked_smoke_ok", "fail_smoke_ok", "worker_scoped_lock_ok", "worker_scoped_settle_ok"):
        if evidence_validation.get(key) is not True:
            blocked.append(f"evidence missing required component: {key}")

    proposed = _proposed_record(current, evidence_path, evidence_validation)
    if request.proposed_overrides:
        proposed.update(request.proposed_overrides)

    changed = _changed_fields(current, proposed)
    disallowed = sorted(key for key in changed if key not in ALLOWED_CHANGED_FIELDS)
    forbidden_changed = sorted(key for key in changed if key in FORBIDDEN_PRESERVED_FIELDS)
    if disallowed:
        blocked.append(f"changed fields are not mixed-readiness related: {disallowed}")
    if forbidden_changed:
        blocked.append(f"forbidden preserved fields would change: {forbidden_changed}")
    if proposed.get("business_ready") is not False:
        blocked.append("business_ready must remain false")
    if proposed.get("allow_business_repo_writes") is True:
        blocked.append("allow_business_repo_writes must remain false")
    if proposed.get("default_for_new_tasks") != current.get("default_for_new_tasks"):
        blocked.append("default_for_new_tasks must remain unchanged")
    if proposed.get("allow_auto_submit") != current.get("allow_auto_submit"):
        blocked.append("allow_auto_submit must remain unchanged")

    next_payload = _replace_worker(registry_payload, request.worker_id, proposed) if current else registry_payload
    blocked = _dedupe(blocked)
    ok = not blocked
    return {
        "schema_version": SCHEMA_VERSION,
        "update_kind": UPDATE_KIND,
        "created_at": _utc_now(),
        "ok": ok,
        "root": str(root),
        "worker_id": request.worker_id,
        "backend_kind": BACKEND_KIND,
        "registry_path": str(registry_path),
        "evidence_path": str(evidence_path),
        "evidence_sha256": _sha256_file(evidence_path),
        "evidence_validation": evidence_validation,
        "current_registry_record": current,
        "proposed_registry_record": proposed,
        "changed_fields": changed,
        "allowed_changed_fields": sorted(ALLOWED_CHANGED_FIELDS),
        "forbidden_changed_fields": forbidden_changed,
        "disallowed_changed_fields": disallowed,
        "preserved_fields": sorted(FORBIDDEN_PRESERVED_FIELDS),
        "current_mixed_ready": bool(current.get("mixed_ready", False)),
        "proposed_mixed_ready": bool(proposed.get("mixed_ready", False)),
        "current_allow_mixed_worker": bool(current.get("allow_mixed_worker", False)),
        "proposed_allow_mixed_worker": bool(proposed.get("allow_mixed_worker", False)),
        "business_ready_before": bool(current.get("business_ready", False)),
        "business_ready_after": bool(proposed.get("business_ready", False)),
        "allow_business_repo_writes_before": bool(current.get("allow_business_repo_writes", False)),
        "allow_business_repo_writes_after": bool(proposed.get("allow_business_repo_writes", False)),
        "mixed_ready_scope": str(proposed.get("mixed_ready_scope", "")),
        "allowed_mixed_task_kinds": list(_list(proposed.get("allowed_mixed_task_kinds"))),
        "would_write_registry": False,
        "would_execute_live": False,
        "would_run_one": False,
        "would_write_registered_registry": False,
        "would_write_state_team": False,
        "would_write_business_repo": False,
        "requires_allow_framework_registry": True,
        "requires_allow_opencode_mixed_ready_update": True,
        "pre_hash": _sha256_file(registry_path),
        "proposed_hash": _sha256_json(next_payload) if current else "",
        "blocked_reasons": blocked,
        "warnings": [
            "OpenCode mixed_ready registry update is no-live",
            "mixed_ready is sandbox/no-op only",
            "business_ready remains false",
            "allow_business_repo_writes remains false",
        ],
        "safe_next_actions": _safe_next_actions(ok),
        "forbidden_next_actions": _forbidden_next_actions(),
        "evidence": [
            {"source": "opencode_visible_registry", "detail": {"registry_path": str(registry_path), "worker_id": request.worker_id}},
            {"source": "opencode_mixed_readiness_evidence", "detail": {"evidence_path": str(evidence_path), "validation": evidence_validation}},
        ],
    }


def write_opencode_mixed_ready_update(request: OpenCodeMixedReadyUpdateRequest) -> dict[str, Any]:
    plan = build_opencode_mixed_ready_update_plan(request)
    blocked = list(plan["blocked_reasons"])
    if not request.allow_framework_registry:
        blocked.append("missing --allow-framework-registry")
    if not request.allow_opencode_mixed_ready_update:
        blocked.append("missing --allow-opencode-mixed-ready-update")
    blocked = _dedupe(blocked)
    if blocked:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "attempted": bool(request.allow_opencode_mixed_ready_update),
            "wrote_registry": False,
            "registry_path": plan["registry_path"],
            "worker_id": request.worker_id,
            "changed_fields": plan["changed_fields"],
            "blocked_reasons": blocked,
            "warnings": plan["warnings"],
            "would_execute_live": False,
            "would_run_one": False,
        }

    registry_path = Path(request.opencode_visible_registry_path).resolve()
    try:
        mutation = mutate_visible_registry(
            registry_path,
            lambda payload: _replace_worker(payload, request.worker_id, plan["proposed_registry_record"]),
            expected_pre_hash=plan["pre_hash"],
            backup_factory=_create_backup,
        )
    except VisibleRegistryConflict as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "attempted": True,
            "wrote_registry": False,
            "registry_path": str(registry_path),
            "worker_id": request.worker_id,
            "changed_fields": plan["changed_fields"],
            "blocked_reasons": [str(exc)],
            "warnings": plan["warnings"],
            "would_execute_live": False,
            "would_run_one": False,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "attempted": True,
        "wrote_registry": True,
        "atomic_write": True,
        "backup_path": mutation["backup_path"],
        "registry_path": str(registry_path),
        "worker_id": request.worker_id,
        "changed_fields": plan["changed_fields"],
        "forbidden_changed_fields": plan["forbidden_changed_fields"],
        "disallowed_changed_fields": plan["disallowed_changed_fields"],
        "pre_hash": mutation["pre_hash"],
        "post_hash": mutation["post_hash"],
        "blocked_reasons": [],
        "warnings": plan["warnings"],
        "would_execute_live": False,
        "would_run_one": False,
        "would_write_registered_registry": False,
        "would_write_state_team": False,
        "would_write_business_repo": False,
    }


def validate_opencode_mixed_ready_update_record(payload: dict[str, Any]) -> dict[str, Any]:
    blocked: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        blocked.append("schema_version must be 1")
    if payload.get("update_kind") != UPDATE_KIND and payload.get("wrote_registry") is not True:
        blocked.append(f"update_kind must be {UPDATE_KIND}")
    if payload.get("would_execute_live") is not False:
        blocked.append("would_execute_live must be false")
    for field_name in _list(payload.get("changed_fields")):
        if str(field_name) not in ALLOWED_CHANGED_FIELDS:
            blocked.append(f"changed field is not mixed-readiness related: {field_name}")
    if payload.get("business_ready_after") is True:
        blocked.append("business_ready_after must remain false")
    if payload.get("allow_business_repo_writes_after") is True:
        blocked.append("allow_business_repo_writes_after must remain false")
    if payload.get("mixed_ready_scope") not in {None, "", MIXED_READY_SCOPE}:
        blocked.append(f"mixed_ready_scope must be {MIXED_READY_SCOPE}")
    kinds = _list(payload.get("allowed_mixed_task_kinds"))
    if kinds and kinds != ALLOWED_MIXED_TASK_KINDS:
        blocked.append("allowed_mixed_task_kinds must stay sandbox/no-op only")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not _dedupe(blocked),
        "blocked_reasons": _dedupe(blocked),
        "warnings": ["validation is no-live and does not authorize mixed live"],
    }


def _proposed_record(current: dict[str, Any], evidence_path: Path, evidence_validation: dict[str, Any]) -> dict[str, Any]:
    now = _utc_now()
    proposed = {
        **current,
        "mixed_ready": True,
        "allow_mixed_worker": True,
        "mixed_ready_scope": MIXED_READY_SCOPE,
        "allowed_mixed_task_kinds": list(ALLOWED_MIXED_TASK_KINDS),
        "mixed_ready_evidence_path": str(evidence_path),
        "mixed_ready_evidence_sha256": _sha256_file(evidence_path),
        "mixed_ready_updated_at": now,
        "mixed_ready_updated_by": UPDATE_KIND,
        "last_mixed_readiness_check": {
            "checked_at": now,
            "ok": bool(evidence_validation.get("ok", False)),
            "mixed_ready_candidate": bool(evidence_validation.get("mixed_ready_candidate", False)),
            "business_ready_candidate": bool(evidence_validation.get("business_ready_candidate", False)),
            "would_update_registry": bool(evidence_validation.get("would_update_registry", False)),
        },
        "evidence_collected": True,
        "done_smoke_evidence_ok": bool(evidence_validation.get("done_smoke_ok", False)),
        "blocked_smoke_evidence_ok": bool(evidence_validation.get("blocked_smoke_ok", False)),
        "fail_smoke_evidence_ok": bool(evidence_validation.get("fail_smoke_ok", False)),
        "worker_scoped_lock_evidence_ok": bool(evidence_validation.get("worker_scoped_lock_ok", False)),
        "settle_evidence_ok": bool(evidence_validation.get("worker_scoped_settle_ok", False)),
        "business_ready": False,
    }
    if "allow_business_repo_writes" in current:
        proposed["allow_business_repo_writes"] = False
    return proposed


def _find_worker(registry: dict[str, Any], worker_id: str) -> dict[str, Any]:
    for item in _list(registry.get("workers")):
        if isinstance(item, dict) and str(item.get("worker_id", "")) == worker_id:
            return dict(item)
    return {}


def _replace_worker(registry: dict[str, Any], worker_id: str, replacement: dict[str, Any]) -> dict[str, Any]:
    return {
        **registry,
        "workers": [replacement if isinstance(item, dict) and str(item.get("worker_id", "")) == worker_id else item for item in _list(registry.get("workers"))],
    }


def _is_framework_opencode_visible_registry(root: Path, registry_path: Path) -> bool:
    return registry_path == (root / ".omx" / "framework" / "team" / "opencode_visible_workers.json").resolve()


def _paths_overlap(left: str, right: str) -> bool:
    left_path = Path(left).resolve()
    right_path = Path(right).resolve()
    return left_path == right_path or left_path in right_path.parents or right_path in left_path.parents


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return data


def _create_backup(path: Path) -> Path:
    backup = path.with_name(path.name + ".backup")
    index = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup.{index}")
        index += 1
    shutil.copy2(path, backup)
    return backup


def _changed_fields(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    return sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_json(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _safe_next_actions(ok: bool) -> list[str]:
    if ok:
        return ["run opencode-mixed-ready-update with explicit allow flags", "run mixed-noop-contract-plan no-live only"]
    return ["fix evidence or registry input and rerun opencode-mixed-ready-update-plan"]


def _forbidden_next_actions() -> list[str]:
    return [
        "live execution",
        "run-one",
        "mixed live",
        "business task",
        "business repo write",
        "registered worker registry write",
        ".omx/state/team write",
        "Claude pane touch",
        "raw tmux input",
        "direct /tui/*",
        "direct adapter submit",
        "rmux live",
        "Obsidian write",
    ]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


__all__ = [
    "OpenCodeMixedReadyUpdateRequest",
    "build_opencode_mixed_ready_update_plan",
    "validate_opencode_mixed_ready_update_record",
    "write_opencode_mixed_ready_update",
]
