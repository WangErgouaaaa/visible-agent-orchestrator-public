"""Disable stale OpenCode visible workers from the auto-submit pool.

This module only updates the project-local visible worker registry after
explicit authorization. It never starts/stops workers, sends tmux input, or
changes the default worker. It only disables auto-submit or normalizes the
inactive status of a non-default stale worker.
"""

from __future__ import annotations

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
from visible_agent_orchestrator.visible_registry_lock import (
    is_inactive_stale_visible_worker,
)


SCHEMA_VERSION = 1
UPDATE_KIND = "opencode-visible-worker-disable"
ALLOWED_POLICY_FIELDS = {
    "allow_auto_submit",
    "status",
    "warnings",
    "notes",
}


@dataclass(frozen=True)
class OpenCodeVisibleWorkerDisableRequest:
    root: str
    worker_id: str
    opencode_visible_registry_path: str
    allow_framework_registry: bool = False
    allow_worker_disable: bool = False
    proposed_status: str = "stale_binding_disabled"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_opencode_visible_worker_disable_plan(
    request: OpenCodeVisibleWorkerDisableRequest,
    *,
    rebind_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(request.root).resolve()
    registry_path = Path(request.opencode_visible_registry_path).resolve()
    registry_payload = _read_json(registry_path) if registry_path.exists() else {}
    current = _find_worker(registry_payload, request.worker_id)
    blocked: list[str] = []
    warnings: list[str] = []

    if not _is_framework_opencode_visible_registry(root, registry_path):
        blocked.append(f"registry path outside allowed OpenCode visible framework registry: {registry_path}")
    if not registry_path.exists():
        blocked.append(f"registry missing: {registry_path}")
    if not current:
        blocked.append(f"worker missing: {request.worker_id}")

    if current:
        if bool(current.get("default_for_new_tasks")):
            blocked.append("default worker cannot be disabled with this command")
        if is_inactive_stale_visible_worker(current):
            blocked.append("worker is already inactive and disabled")

    evidence = rebind_check or {}
    if not evidence:
        blocked.append("missing rebind-check evidence")
    else:
        evidence_worker_id = str(evidence.get("worker_id") or "")
        if not evidence_worker_id:
            blocked.append("rebind-check evidence must identify the checked worker")
        elif evidence_worker_id != request.worker_id:
            blocked.append(
                "rebind-check evidence worker does not match requested worker: "
                f"{evidence_worker_id} != {request.worker_id}"
            )
        if bool(evidence.get("ok")):
            blocked.append("worker still passes rebind-check; disable refused")
        status = str(evidence.get("status") or "")
        if status not in {"binding_failed", "port_not_live"}:
            blocked.append(f"rebind-check status must be binding_failed or port_not_live, got: {status or '-'}")
        errors = evidence.get("errors", []) if isinstance(evidence.get("errors"), list) else []
        if status == "binding_failed" and not errors:
            blocked.append("rebind-check must include at least one binding failure reason")
        if status == "port_not_live":
            target_owner_lock = evidence.get("target_owner_lock")
            if not isinstance(target_owner_lock, dict) or target_owner_lock:
                blocked.append("port_not_live disable requires empty target_owner_lock evidence")

    proposed = dict(current or {})
    if current:
        proposed["allow_auto_submit"] = False
        proposed["status"] = request.proposed_status
        evidence_status = str(evidence.get("status") or "failed")
        warning_text = f"disabled from auto-submit after live rebind-check {evidence_status} evidence"
        warnings_list = [str(item) for item in proposed.get("warnings", [])] if isinstance(proposed.get("warnings"), list) else []
        if warning_text not in warnings_list:
            warnings_list.append(warning_text)
        proposed["warnings"] = warnings_list
        notes_list = [str(item) for item in proposed.get("notes", [])] if isinstance(proposed.get("notes"), list) else []
        note_text = f"Auto submit disabled because live rebind-check evidence reported {evidence_status}."
        if note_text not in notes_list:
            notes_list.append(note_text)
        proposed["notes"] = notes_list

    changed_fields = _changed_fields(current, proposed) if current else []
    forbidden_changed = sorted(field for field in changed_fields if field not in ALLOWED_POLICY_FIELDS)
    if forbidden_changed:
        blocked.append(f"non-disable fields would change: {forbidden_changed}")

    next_payload = _replace_worker(registry_payload, request.worker_id, proposed) if current else registry_payload
    blocked = _dedupe(blocked)
    return {
        "schema_version": SCHEMA_VERSION,
        "update_kind": UPDATE_KIND,
        "created_at": _utc_now(),
        "ok": not blocked,
        "root": str(root),
        "worker_id": request.worker_id,
        "registry_path": str(registry_path),
        "current_registry_record": current,
        "proposed_registry_record": proposed,
        "changed_fields": changed_fields,
        "forbidden_changed_fields": forbidden_changed,
        "safe_for_disable": not blocked,
        "blocked_reasons": blocked,
        "warnings": warnings,
        "rebind_check": evidence,
        "pre_hash": _sha256_file(registry_path),
        "proposed_hash": _sha256_json(next_payload) if current else "",
        "would_write_registry": False,
        "would_execute_live": False,
        "requires_allow_framework_registry": True,
        "requires_allow_worker_disable": True,
        "no_live_actions_performed": True,
        "safe_next_actions": [
            "run opencode-visible-worker-disable with explicit allow flags",
            "rerun backend-audit after registry write",
        ] if not blocked else [
            "fix the worker or gather binding_failed/port_not_live rebind-check evidence",
        ],
    }


def write_opencode_visible_worker_disable(
    request: OpenCodeVisibleWorkerDisableRequest,
    *,
    rebind_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = build_opencode_visible_worker_disable_plan(request, rebind_check=rebind_check)
    blocked = list(plan["blocked_reasons"])
    if not request.allow_framework_registry:
        blocked.append("missing --allow-framework-registry")
    if not request.allow_worker_disable:
        blocked.append("missing --allow-worker-disable")
    blocked = _dedupe(blocked)
    if blocked:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "attempted": bool(request.allow_worker_disable),
            "wrote_registry": False,
            "registry_path": plan["registry_path"],
            "worker_id": request.worker_id,
            "changed_fields": plan["changed_fields"],
            "blocked_reasons": blocked,
            "warnings": plan["warnings"],
            "would_execute_live": False,
            "no_live_actions_performed": True,
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
            "no_live_actions_performed": True,
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
        "pre_hash": mutation["pre_hash"],
        "post_hash": mutation["post_hash"],
        "blocked_reasons": [],
        "warnings": plan["warnings"],
        "would_execute_live": False,
        "no_live_actions_performed": True,
    }


def validate_opencode_visible_worker_disable_record(payload: dict[str, Any]) -> dict[str, Any]:
    blocked: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        blocked.append("schema_version must be 1")
    if payload.get("update_kind") != UPDATE_KIND:
        blocked.append(f"update_kind must be {UPDATE_KIND}")
    if payload.get("would_execute_live") is not False:
        blocked.append("would_execute_live must be false")
    if payload.get("would_write_registry") is not False:
        blocked.append("plan would_write_registry must be false")
    if payload.get("safe_for_disable") is not True:
        blocked.append("safe_for_disable must be true")
    for field_name in _list(payload.get("changed_fields")):
        if str(field_name) not in ALLOWED_POLICY_FIELDS:
            blocked.append(f"changed field is not a disable-only policy field: {field_name}")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not blocked,
        "blocked_reasons": _dedupe(blocked),
    }


def _is_framework_opencode_visible_registry(root: Path, registry_path: Path) -> bool:
    allowed = (root / ".omx" / "framework" / "team" / "opencode_visible_workers.json").resolve()
    return registry_path.resolve() == allowed


def _find_worker(registry: dict[str, Any], worker_id: str) -> dict[str, Any]:
    for item in _list(registry.get("workers")):
        if isinstance(item, dict) and str(item.get("worker_id", "")) == worker_id:
            return dict(item)
    return {}


def _replace_worker(registry: dict[str, Any], worker_id: str, replacement: dict[str, Any]) -> dict[str, Any]:
    payload = dict(registry)
    workers = []
    for item in _list(registry.get("workers")):
        if isinstance(item, dict) and str(item.get("worker_id", "")) == worker_id:
            workers.append(dict(replacement))
        else:
            workers.append(item)
    payload["workers"] = workers
    return payload


def _changed_fields(current: dict[str, Any], proposed: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for key in sorted(set(current) | set(proposed)):
        if current.get(key) != proposed.get(key):
            changed.append(key)
    return changed


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item)
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_json(payload: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256((json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")).hexdigest()


def _create_backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = path.with_name(path.name + f".bak.{stamp}")
    backup = base
    counter = 1
    while backup.exists():
        backup = path.with_name(path.name + f".bak.{stamp}.{counter}")
        counter += 1
    shutil.copy2(path, backup)
    return backup
