"""Fail-closed registered worker registry planning and writes.

This module performs local JSON validation and atomic file writes only. A
registry write is inventory state; it is not live submit, current liveness,
business readiness, or mixed-worker readiness.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .authorization import validate_authorization_record_file
from .registered_worker import RegisteredWorkerRecord, validate_registered_worker_record_payload


FORBIDDEN_REGISTRY_PATH_FRAGMENTS = (
    "/.omx/state/team/",
)


@dataclass(frozen=True)
class RegisteredWorkerRegistry:
    schema_version: int
    registry_id: str
    created_at: str
    updated_at: str
    registry_kind: str
    workers: list[dict[str, Any]] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls, registry_id: str = "registered-workers-fixture") -> "RegisteredWorkerRegistry":
        now = _now()
        return cls(
            schema_version=1,
            registry_id=registry_id,
            created_at=now,
            updated_at=now,
            registry_kind="registered_worker_registry",
            workers=[],
            defaults={
                "allow_business_repo_writes": False,
                "allow_mixed_worker": False,
                "business_ready": False,
                "mixed_ready": False,
            },
            metadata={"source": "empty-default"},
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RegisteredWorkerRegistry":
        return cls(
            schema_version=int(payload.get("schema_version", 0) or 0),
            registry_id=str(payload.get("registry_id", "")),
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            registry_kind=str(payload.get("registry_kind", "")),
            workers=[_dict(item) for item in payload.get("workers", []) if isinstance(item, dict)],
            defaults=_dict(payload.get("defaults", {})),
            metadata=_dict(payload.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegisteredWorkerRegistryValidationResult:
    ok: bool
    registry_path: str = ""
    registry_id: str = ""
    worker_count: int = 0
    worker_ids: list[str] = field(default_factory=list)
    duplicate_workers: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegisteredWorkerWritePlan:
    would_write_registry: bool
    registry_path: str
    worker_id: str
    backend_kind: str
    record_ok: bool
    registry_ok_before: bool
    registry_ok_after: bool
    duplicate_worker: bool
    replace_existing: bool
    allow_register: bool
    allowed_registry_path: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegisteredWorkerWriteResult:
    attempted: bool
    wrote_registry: bool
    registry_path: str
    backup_path: str
    worker_id: str
    backend_kind: str
    record_written: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegisteredWorkerAuthorizationUpdatePlan:
    ok: bool
    would_execute_live: bool
    would_touch_worker: bool
    would_write_registry: bool
    registry_path: str
    authorization_record: str
    worker_id: str
    worker_found: bool
    authorization_ok: bool
    registry_ok_before: bool
    registry_ok_after: bool
    allowed_registry_path: bool
    changed_fields: list[str] = field(default_factory=list)
    preserved_fields: dict[str, Any] = field(default_factory=dict)
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegisteredWorkerAuthorizationUpdateResult:
    attempted: bool
    wrote_registry: bool
    would_execute_live: bool
    would_touch_worker: bool
    registry_path: str
    authorization_record: str
    backup_path: str
    worker_id: str
    changed_fields: list[str] = field(default_factory=list)
    preserved_fields: dict[str, Any] = field(default_factory=dict)
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_registered_worker_registry_file(path: Path | str) -> RegisteredWorkerRegistryValidationResult:
    registry_path = Path(path)
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _registry_invalid([f"registered worker registry missing: {registry_path}"], registry_path=str(registry_path))
    except json.JSONDecodeError as exc:
        return _registry_invalid([f"malformed JSON: {exc.msg}"], registry_path=str(registry_path))
    if not isinstance(payload, dict):
        return _registry_invalid(["registered worker registry must contain an object"], registry_path=str(registry_path))
    return validate_registered_worker_registry_payload(payload, registry_path=str(registry_path), base_path=registry_path.parent)


def validate_registered_worker_registry_payload(
    payload: dict[str, Any],
    *,
    registry_path: str = "inline",
    base_path: Path | None = None,
) -> RegisteredWorkerRegistryValidationResult:
    try:
        registry = RegisteredWorkerRegistry.from_dict(payload)
    except (TypeError, ValueError) as exc:
        return _registry_invalid([f"registered worker registry fields malformed: {exc}"], registry_path=registry_path)

    blocked: list[str] = []
    warnings = [
        "registered worker registry is not authorization",
        "registered worker registry is not live enable",
        "registered worker registry is not current liveness",
        "registered worker registry does not permit live submit",
    ]
    if registry.schema_version != 1:
        blocked.append("schema_version must be 1")
    if not registry.registry_id:
        blocked.append("missing registry_id")
    if registry.registry_kind != "registered_worker_registry":
        blocked.append("registry_kind must be registered_worker_registry")
    if not isinstance(payload.get("workers", []), list):
        blocked.append("workers must be a list")

    worker_ids: list[str] = []
    for worker in registry.workers:
        worker_id = str(worker.get("worker_id", ""))
        if worker_id:
            worker_ids.append(worker_id)
        blocked.extend(_worker_blocks(worker, base_path=base_path))

    duplicates = sorted({worker_id for worker_id in worker_ids if worker_ids.count(worker_id) > 1})
    for worker_id in duplicates:
        blocked.append(f"duplicate worker_id: {worker_id}")

    blocked = _dedupe(blocked)
    ok = not blocked
    return RegisteredWorkerRegistryValidationResult(
        ok=ok,
        registry_path=registry_path,
        registry_id=registry.registry_id,
        worker_count=len(registry.workers),
        worker_ids=worker_ids,
        duplicate_workers=duplicates,
        blocked_reasons=blocked,
        warnings=_dedupe(warnings),
        evidence=[{"source": "registered_worker_registry", "detail": {"registry_path": registry_path}}],
        safe_next_actions=_safe_next_actions(ok),
        forbidden_next_actions=_forbidden_next_actions(),
    )


def build_registered_worker_register_plan(
    *,
    record_path: Path | str,
    registry_path: Path | str,
    allow_register: bool = False,
    replace_existing: bool = False,
    allowed_registry_roots: list[Path | str] | None = None,
) -> RegisteredWorkerWritePlan:
    registry = _load_registry_or_empty(Path(registry_path))
    return _build_write_plan(
        record_path=Path(record_path),
        registry_path=Path(registry_path),
        registry_payload=registry.to_dict(),
        allow_register=allow_register,
        replace_existing=replace_existing,
        allowed_registry_roots=list(allowed_registry_roots or []),
    )


def write_registered_worker_record(
    *,
    record_path: Path | str,
    registry_path: Path | str,
    allow_register: bool = False,
    replace_existing: bool = False,
    allowed_registry_roots: list[Path | str] | None = None,
) -> RegisteredWorkerWriteResult:
    record_file = Path(record_path)
    target = Path(registry_path)
    before = _load_registry_or_empty(target)
    plan = _build_write_plan(
        record_path=record_file,
        registry_path=target,
        registry_payload=before.to_dict(),
        allow_register=allow_register,
        replace_existing=replace_existing,
        allowed_registry_roots=list(allowed_registry_roots or []),
    )
    if plan.blocked_reasons or not allow_register:
        return RegisteredWorkerWriteResult(
            attempted=bool(allow_register),
            wrote_registry=False,
            registry_path=str(target),
            backup_path="",
            worker_id=plan.worker_id,
            backend_kind=plan.backend_kind,
            record_written=False,
            blocked_reasons=plan.blocked_reasons,
            warnings=plan.warnings,
            evidence=plan.evidence,
        )

    next_payload = _registry_after_record(before.to_dict(), _read_record_worker(record_file), replace_existing=replace_existing)
    after = validate_registered_worker_registry_payload(next_payload, registry_path=str(target), base_path=target.parent)
    after_blocking = _registry_blocking_reasons_for_write(after.blocked_reasons, plan.worker_id, before_write=False)
    if after_blocking:
        return RegisteredWorkerWriteResult(
            attempted=True,
            wrote_registry=False,
            registry_path=str(target),
            backup_path="",
            worker_id=plan.worker_id,
            backend_kind=plan.backend_kind,
            record_written=False,
            blocked_reasons=after_blocking,
            warnings=plan.warnings + after.warnings,
            evidence=plan.evidence + after.evidence,
        )

    backup = _create_backup(target, before.to_dict())
    _atomic_write_json(target, next_payload)
    return RegisteredWorkerWriteResult(
        attempted=True,
        wrote_registry=True,
        registry_path=str(target),
        backup_path=str(backup),
        worker_id=plan.worker_id,
        backend_kind=plan.backend_kind,
        record_written=True,
        blocked_reasons=[],
        warnings=plan.warnings,
        evidence=plan.evidence + [{"source": "atomic_registry_write", "detail": {"backup_path": str(backup)}}],
    )


def build_registered_worker_authorization_update_plan(
    *,
    root: Path | str,
    registry_path: Path | str,
    worker_id: str,
    authorization_record_path: Path | str,
) -> RegisteredWorkerAuthorizationUpdatePlan:
    target = Path(registry_path)
    auth_path = Path(authorization_record_path)
    registry_payload = _load_registry_or_empty(target).to_dict()
    return _build_authorization_update_plan(
        root=Path(root),
        registry_path=target,
        registry_payload=registry_payload,
        worker_id=worker_id,
        authorization_record_path=auth_path,
    )


def write_registered_worker_authorization_update(
    *,
    root: Path | str,
    registry_path: Path | str,
    worker_id: str,
    authorization_record_path: Path | str,
    allow_framework_registry: bool = False,
    allow_authorization_update: bool = False,
) -> RegisteredWorkerAuthorizationUpdateResult:
    target = Path(registry_path)
    auth_path = Path(authorization_record_path)
    before = _load_registry_or_empty(target).to_dict()
    plan = _build_authorization_update_plan(
        root=Path(root),
        registry_path=target,
        registry_payload=before,
        worker_id=worker_id,
        authorization_record_path=auth_path,
    )
    blocked = list(plan.blocked_reasons)
    if not allow_framework_registry:
        blocked.append("missing --allow-framework-registry")
    if not allow_authorization_update:
        blocked.append("missing --allow-authorization-update")
    blocked = _dedupe(blocked)
    if blocked:
        return RegisteredWorkerAuthorizationUpdateResult(
            attempted=bool(allow_framework_registry or allow_authorization_update),
            wrote_registry=False,
            would_execute_live=False,
            would_touch_worker=False,
            registry_path=str(target),
            authorization_record=str(auth_path),
            backup_path="",
            worker_id=worker_id,
            changed_fields=plan.changed_fields,
            preserved_fields=plan.preserved_fields,
            blocked_reasons=blocked,
            warnings=plan.warnings,
            evidence=plan.evidence,
        )

    after_payload = _registry_after_authorization_update(before, worker_id, str(auth_path.resolve()))
    after = validate_registered_worker_registry_payload(after_payload, registry_path=str(target), base_path=target.parent)
    if not after.ok:
        return RegisteredWorkerAuthorizationUpdateResult(
            attempted=True,
            wrote_registry=False,
            would_execute_live=False,
            would_touch_worker=False,
            registry_path=str(target),
            authorization_record=str(auth_path),
            backup_path="",
            worker_id=worker_id,
            changed_fields=plan.changed_fields,
            preserved_fields=plan.preserved_fields,
            blocked_reasons=after.blocked_reasons,
            warnings=plan.warnings + after.warnings,
            evidence=plan.evidence + after.evidence,
        )

    backup = _create_backup(target, before)
    temp_path = _atomic_write_json(target, after_payload)
    return RegisteredWorkerAuthorizationUpdateResult(
        attempted=True,
        wrote_registry=True,
        would_execute_live=False,
        would_touch_worker=False,
        registry_path=str(target),
        authorization_record=str(auth_path.resolve()),
        backup_path=str(backup),
        worker_id=worker_id,
        changed_fields=plan.changed_fields,
        preserved_fields=plan.preserved_fields,
        blocked_reasons=[],
        warnings=plan.warnings,
        evidence=plan.evidence
        + [
            {
                "source": "atomic_registry_write",
                "detail": {
                    "backup_path": str(backup),
                    "temp_path": str(temp_path),
                    "target_path": str(target),
                    "atomic_replace": True,
                },
            }
        ],
    )


def registry_status(path: Path | str) -> dict[str, Any]:
    registry_path = Path(path)
    result = validate_registered_worker_registry_file(registry_path)
    payload = _load_registry_or_empty(registry_path).to_dict() if registry_path.exists() else RegisteredWorkerRegistry.empty().to_dict()
    workers = [
        {
            "worker_id": str(worker.get("worker_id", "")),
            "backend_kind": str(worker.get("backend_kind", "")),
            "registration_status": str(worker.get("registration_status", "")),
            "live_enabled": bool(worker.get("live_enabled", False)),
            "business_ready": bool(worker.get("business_ready", False)),
            "mixed_ready": bool(worker.get("mixed_ready", False)),
        }
        for worker in payload.get("workers", [])
        if isinstance(worker, dict)
    ]
    return {
        "ok": result.ok,
        "registry_path": str(registry_path),
        "registry_id": result.registry_id,
        "worker_count": len(workers),
        "workers": workers,
        "blocked_reasons": result.blocked_reasons,
        "warnings": result.warnings + ["registry status is read-only and does not perform live health checks"],
        "forbidden_next_actions": result.forbidden_next_actions,
    }


def _build_write_plan(
    *,
    record_path: Path,
    registry_path: Path,
    registry_payload: dict[str, Any],
    allow_register: bool,
    replace_existing: bool,
    allowed_registry_roots: list[Path | str],
) -> RegisteredWorkerWritePlan:
    record_payload = _read_json(record_path)
    worker = _record_payload_to_registry_worker(record_payload, record_path=record_path)
    record_validation = validate_registered_worker_record_payload(
        _registry_worker_to_record_payload(worker),
        evidence_source=str(record_path),
        base_path=record_path.parent,
    )
    before = validate_registered_worker_registry_payload(registry_payload, registry_path=str(registry_path), base_path=registry_path.parent)
    after_payload = _registry_after_record(registry_payload, worker, replace_existing=replace_existing)
    after = validate_registered_worker_registry_payload(after_payload, registry_path=str(registry_path), base_path=registry_path.parent)
    existing_ids = [str(item.get("worker_id", "")) for item in registry_payload.get("workers", []) if isinstance(item, dict)]
    duplicate_worker = worker["worker_id"] in existing_ids
    allowed_path = _registry_path_allowed(registry_path, allowed_registry_roots)
    before_blocking = _registry_blocking_reasons_for_write(before.blocked_reasons, worker["worker_id"], before_write=True)
    after_blocking = _registry_blocking_reasons_for_write(after.blocked_reasons, worker["worker_id"], before_write=False)
    ignored_registry_blocks = [
        f"registry before authorization gap ignored for register repair: {reason}"
        for reason in before.blocked_reasons
        if reason not in before_blocking
    ] + [
        f"registry after unrelated authorization gap preserved as warning: {reason}"
        for reason in after.blocked_reasons
        if reason not in after_blocking
    ]

    blocked: list[str] = []
    if not allow_register:
        blocked.append("missing --allow-register")
    if not str(registry_path):
        blocked.append("registry path must be explicit")
    if not allowed_path:
        blocked.append(f"registry path is not allowed: {registry_path}")
    if not record_validation.ok:
        blocked.extend(record_validation.blocked_reasons)
    if before_blocking:
        blocked.extend([f"registry before invalid: {reason}" for reason in before_blocking])
    if duplicate_worker and not replace_existing:
        blocked.append(f"duplicate worker_id requires --replace-existing: {worker['worker_id']}")
    if after_blocking:
        blocked.extend([f"registry after invalid: {reason}" for reason in after_blocking])

    blocked = _dedupe(blocked)
    return RegisteredWorkerWritePlan(
        would_write_registry=False,
        registry_path=str(registry_path),
        worker_id=worker["worker_id"],
        backend_kind=worker["backend_kind"],
        record_ok=record_validation.ok,
        registry_ok_before=before.ok,
        registry_ok_after=after.ok,
        duplicate_worker=duplicate_worker,
        replace_existing=replace_existing,
        allow_register=allow_register,
        allowed_registry_path=allowed_path,
        blocked_reasons=blocked,
        warnings=[
            "registered worker registry write is not live submit",
            "registered worker registry write is not business readiness",
            "registered worker registry write is not mixed-worker readiness",
            "registered worker registry write does not touch panes or live worker commands",
        ]
        + ignored_registry_blocks,
        evidence=[
            {"source": "record_validation", "detail": record_validation.to_dict()},
            {"source": "registry_before", "detail": before.to_dict()},
            {"source": "registry_after", "detail": after.to_dict()},
        ],
        safe_next_actions=_safe_next_actions(not blocked),
        forbidden_next_actions=_forbidden_next_actions(),
    )


def _build_authorization_update_plan(
    *,
    root: Path,
    registry_path: Path,
    registry_payload: dict[str, Any],
    worker_id: str,
    authorization_record_path: Path,
) -> RegisteredWorkerAuthorizationUpdatePlan:
    worker = _find_worker(registry_payload, worker_id)
    auth_result = validate_authorization_record_file(authorization_record_path)
    before = validate_registered_worker_registry_payload(registry_payload, registry_path=str(registry_path), base_path=registry_path.parent)
    after_payload = _registry_after_authorization_update(registry_payload, worker_id, str(authorization_record_path.resolve())) if worker else registry_payload
    after = validate_registered_worker_registry_payload(after_payload, registry_path=str(registry_path), base_path=registry_path.parent)
    allowed_path = _authorization_update_registry_path_allowed(registry_path, root)

    blocked: list[str] = []
    if not worker:
        blocked.append(f"worker not found: {worker_id}")
    if not allowed_path:
        blocked.append(f"registry path is not allowed: {registry_path}")
    if not auth_result.ok:
        blocked.extend(auth_result.blocked_reasons)
    if before.ok:
        registry_ok_before = True
    else:
        registry_ok_before = False
        non_auth_blocks = [
            reason
            for reason in before.blocked_reasons
            if "requires valid authorization record" not in reason and "registered_worker_authorization_gap:" not in reason
        ]
        blocked.extend([f"registry before invalid beyond authorization: {reason}" for reason in non_auth_blocks])
    if worker:
        blocked.extend(_authorization_worker_mismatch_blocks(worker, auth_result, authorization_record_path))
    if not after.ok:
        blocked.extend([f"registry after invalid: {reason}" for reason in after.blocked_reasons])

    changed_fields = ["authorization_record"] if worker else []
    preserved = _preserved_authorization_update_fields(worker or {})
    blocked = _dedupe(blocked)
    return RegisteredWorkerAuthorizationUpdatePlan(
        ok=not blocked,
        would_execute_live=False,
        would_touch_worker=False,
        would_write_registry=False,
        registry_path=str(registry_path),
        authorization_record=str(authorization_record_path.resolve()),
        worker_id=worker_id,
        worker_found=bool(worker),
        authorization_ok=auth_result.ok,
        registry_ok_before=registry_ok_before,
        registry_ok_after=after.ok,
        allowed_registry_path=allowed_path,
        changed_fields=changed_fields,
        preserved_fields=preserved,
        blocked_reasons=blocked,
        warnings=[
            "authorization update plan is no-live and read-only",
            "authorization update must not change mixed-ready or business-ready fields",
            "authorization update does not touch panes, tmux, Claude Code, OpenCode, or business repos",
        ],
        evidence=[
            {"source": "authorization_record_validation", "detail": auth_result.to_dict()},
            {"source": "registry_before", "detail": before.to_dict()},
            {"source": "registry_after", "detail": after.to_dict()},
            {"source": "changed_fields", "detail": {"worker_fields": changed_fields}},
        ],
        safe_next_actions=_authorization_update_safe_next_actions(not blocked),
        forbidden_next_actions=_forbidden_next_actions(),
    )


def _record_payload_to_registry_worker(payload: dict[str, Any], *, record_path: Path) -> dict[str, Any]:
    record = RegisteredWorkerRecord.from_dict(payload)
    smoke = str(payload.get("last_smoke_artifact", "")) or record.smoke_evidence_path
    smoke_for_registry = str(_resolve_relative(smoke, record_path.parent)) if smoke else ""
    return {
        "backend_kind": record.backend_kind,
        "worker_id": record.worker_id,
        "worker_label": str(payload.get("worker_label", record.worker_id)),
        "workspace_root": record.workspace_root,
        "runtime_backend": record.runtime_backend,
        "runtime_binding_kind": record.runtime_binding_kind,
        "pane_id": record.pane_id,
        "pane_pid": record.pane_pid,
        "process_id": record.process_id,
        "command": record.command,
        "user_owned_pane": record.user_owned_pane,
        "authorization_record": record.authorization_record,
        "live_enable_config": record.live_enable_config,
        "last_smoke_artifact": smoke_for_registry,
        "evidence_package": str(payload.get("evidence_package", "")),
        "allowed_write_roots": _string_list(payload.get("allowed_write_roots", [])),
        "forbidden_paths": _string_list(payload.get("forbidden_paths", [])),
        "allow_business_repo_writes": bool(payload.get("allow_business_repo_writes", record.allow_business_repo_writes)),
        "allow_single_worker_smoke": bool(payload.get("allow_single_worker_smoke", False)),
        "allow_mixed_worker": bool(payload.get("allow_mixed_worker", record.allow_mixed_worker)),
        "registration_status": str(payload.get("registration_status", record.registration_status)),
        "live_enabled": bool(payload.get("live_enabled", record.live_enabled)),
        "business_ready": bool(payload.get("business_ready", False)),
        "mixed_ready": bool(payload.get("mixed_ready", False)),
        "registered_at": str(payload.get("registered_at", "")) or _now(),
        "registered_by": str(payload.get("registered_by", "omx_worker_backend")),
        "notes": _string_list(payload.get("notes", record.notes)),
    }


def _registry_worker_to_record_payload(worker: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "backend_kind": worker.get("backend_kind", ""),
        "worker_id": worker.get("worker_id", ""),
        "workspace_root": worker.get("workspace_root", ""),
        "runtime_backend": worker.get("runtime_backend", ""),
        "runtime_binding_kind": worker.get("runtime_binding_kind", ""),
        "pane_id": worker.get("pane_id", ""),
        "pane_pid": worker.get("pane_pid", ""),
        "process_id": worker.get("process_id", ""),
        "command": worker.get("command", ""),
        "user_owned_pane": bool(worker.get("user_owned_pane", False)),
        "registration_status": worker.get("registration_status", "planned"),
        "live_enabled": bool(worker.get("live_enabled", False)),
        "allow_business_repo_writes": bool(worker.get("allow_business_repo_writes", False)),
        "allow_mixed_worker": bool(worker.get("allow_mixed_worker", False)),
        "authorization_record": worker.get("authorization_record", ""),
        "live_enable_config": worker.get("live_enable_config", ""),
        "smoke_evidence_path": worker.get("last_smoke_artifact", ""),
        "notes": _string_list(worker.get("notes", [])),
    }


def _worker_blocks(worker: dict[str, Any], *, base_path: Path | None) -> list[str]:
    blocked: list[str] = []
    required = [
        "backend_kind",
        "worker_id",
        "worker_label",
        "workspace_root",
        "runtime_backend",
        "runtime_binding_kind",
        "pane_id",
        "pane_pid",
        "process_id",
        "command",
        "authorization_record",
        "live_enable_config",
        "last_smoke_artifact",
        "registration_status",
        "registered_at",
        "registered_by",
    ]
    for key in required:
        if not str(worker.get(key, "")):
            blocked.append(f"worker {worker.get('worker_id', '<missing>')} missing {key}")
    mixed_scope_ok = _mixed_scope_ok(worker)
    record_payload = _registry_worker_to_record_payload(worker)
    if mixed_scope_ok:
        record_payload["allow_mixed_worker"] = False
    record_result = validate_registered_worker_record_payload(
        record_payload,
        evidence_source="registry-worker",
        base_path=base_path,
    )
    if not record_result.ok:
        blocked.extend(record_result.blocked_reasons)
    if bool(worker.get("business_ready", False)) and not bool(worker.get("allow_business_repo_writes", False)):
        blocked.append(f"worker {worker.get('worker_id', '<missing>')} business_ready=true requires allow_business_repo_writes=true")
    if bool(worker.get("mixed_ready", False)) and not bool(worker.get("allow_mixed_worker", False)):
        blocked.append(f"worker {worker.get('worker_id', '<missing>')} mixed_ready=true requires allow_mixed_worker=true")
    if bool(worker.get("allow_business_repo_writes", False)) and not bool(worker.get("allow_business_repo_writes_explicit", False)):
        blocked.append(f"worker {worker.get('worker_id', '<missing>')} business repo writes require explicit allow")
    if bool(worker.get("allow_mixed_worker", False)) and not mixed_scope_ok:
        blocked.append(f"worker {worker.get('worker_id', '<missing>')} mixed worker remains gated")
    if bool(worker.get("user_owned_pane", False)):
        auth = _resolve_relative(str(worker.get("authorization_record", "")), base_path)
        auth_result = validate_authorization_record_file(auth)
        if not auth_result.ok:
            blocked.append(f"worker {worker.get('worker_id', '<missing>')} user_owned_pane requires valid authorization record")
            blocked.extend(_authorization_gap_blocks(str(worker.get("worker_id", "<missing>")), auth_result.blocked_reasons))
    return blocked


def _authorization_gap_blocks(worker_id: str, reasons: list[str]) -> list[str]:
    return [
        f"worker {worker_id} registered_worker_authorization_gap:{_authorization_reason_code(reason)}"
        for reason in reasons
    ]


def _authorization_reason_code(reason: str) -> str:
    if reason == "authorization expired":
        return "authorization_expired"
    if reason.startswith("authorization record missing:"):
        return "authorization_missing"
    if reason.startswith("malformed JSON:"):
        return "authorization_malformed_json"
    if reason == "user_owned_pane=true requires allow_user_owned_pane=true":
        return "authorization_user_owned_pane_not_allowed"
    code = "".join(ch.lower() if ch.isalnum() else "_" for ch in reason).strip("_")
    return code or "authorization_invalid"


def _registry_blocking_reasons_for_write(reasons: list[str], worker_id: str, *, before_write: bool) -> list[str]:
    blocking = []
    for reason in reasons:
        if before_write and _is_authorization_gap_reason(reason):
            continue
        if not before_write and _is_authorization_gap_reason(reason) and f"worker {worker_id} " not in reason:
            continue
        blocking.append(reason)
    return blocking


def _is_authorization_gap_reason(reason: str) -> bool:
    return "requires valid authorization record" in reason or "registered_worker_authorization_gap:" in reason


def _mixed_scope_ok(worker: dict[str, Any]) -> bool:
    if not bool(worker.get("allow_mixed_worker", False)):
        return False
    if not bool(worker.get("mixed_ready", False)):
        return False
    if bool(worker.get("business_ready", False)):
        return False
    if bool(worker.get("allow_business_repo_writes", False)):
        return False
    return (
        str(worker.get("mixed_ready_scope", "")) == "sandbox_noop_only"
        and _string_list(worker.get("allowed_mixed_task_kinds", []))
        == ["mixed_noop_sandbox", "mixed_terminal_protocol_smoke"]
    )


def _registry_after_record(payload: dict[str, Any], worker: dict[str, Any], *, replace_existing: bool) -> dict[str, Any]:
    registry = RegisteredWorkerRegistry.from_dict(payload).to_dict()
    workers = [_dict(item) for item in registry.get("workers", []) if isinstance(item, dict)]
    if replace_existing:
        workers = [item for item in workers if str(item.get("worker_id", "")) != worker["worker_id"]]
    workers.append(worker)
    now = _now()
    return {
        "schema_version": 1,
        "registry_id": registry.get("registry_id") or "registered-workers-fixture",
        "created_at": registry.get("created_at") or now,
        "updated_at": now,
        "registry_kind": "registered_worker_registry",
        "workers": workers,
        "defaults": registry.get("defaults") or {
            "allow_business_repo_writes": False,
            "allow_mixed_worker": False,
            "business_ready": False,
            "mixed_ready": False,
        },
        "metadata": {**_dict(registry.get("metadata", {})), "last_write_source": "registered-worker-register"},
    }


def _registry_after_authorization_update(payload: dict[str, Any], worker_id: str, authorization_record: str) -> dict[str, Any]:
    registry = RegisteredWorkerRegistry.from_dict(payload).to_dict()
    workers: list[dict[str, Any]] = []
    for item in registry.get("workers", []):
        worker = _dict(item)
        if str(worker.get("worker_id", "")) == worker_id:
            worker = {**worker, "authorization_record": authorization_record}
        workers.append(worker)
    return {**registry, "workers": workers}


def _find_worker(payload: dict[str, Any], worker_id: str) -> dict[str, Any] | None:
    for item in payload.get("workers", []):
        worker = _dict(item)
        if str(worker.get("worker_id", "")) == worker_id:
            return worker
    return None


def _authorization_worker_mismatch_blocks(
    worker: dict[str, Any],
    auth_result: Any,
    authorization_record_path: Path,
) -> list[str]:
    blocked: list[str] = []
    try:
        auth_payload = _read_json(authorization_record_path)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        return [f"authorization record unreadable: {exc}"]
    checks = {
        "worker_id": str(worker.get("worker_id", "")),
        "backend_kind": str(worker.get("backend_kind", "")),
        "workspace_root": str(worker.get("workspace_root", "")),
        "runtime_backend": str(worker.get("runtime_backend", "")),
        "runtime_binding_kind": str(worker.get("runtime_binding_kind", "")),
        "pane_id": str(worker.get("pane_id", "")),
        "process_id": str(worker.get("process_id", "")),
    }
    for key, expected in checks.items():
        actual = str(auth_payload.get(key, ""))
        if expected and actual and actual != expected:
            blocked.append(f"authorization {key} mismatch: {actual} != {expected}")
        elif expected and not actual:
            blocked.append(f"authorization missing {key}")
    if bool(worker.get("user_owned_pane", False)):
        if not bool(auth_payload.get("user_owned_pane", False)):
            blocked.append("authorization must target user_owned_pane=true")
        if not bool(auth_payload.get("allow_user_owned_pane", False)):
            blocked.append("authorization must allow user_owned_pane")
    if bool(auth_payload.get("allow_business_repo_writes", False)):
        blocked.append("authorization must keep allow_business_repo_writes=false")
    if bool(auth_payload.get("allow_live_submit", False)):
        blocked.append("authorization must keep allow_live_submit=false for no-live refresh")
    if bool(auth_payload.get("allow_registration", False)):
        blocked.append("authorization must keep allow_registration=false for authorization refresh")
    allowed_task_kinds = set(_string_list(auth_payload.get("allowed_task_kinds", [])))
    conservative_task_kinds = {
        "no_op_smoke",
        "mixed_noop_sandbox",
        "mixed_terminal_protocol_smoke",
        "registry_no_live_consistency",
        "fixture_preflight",
    }
    extra_task_kinds = sorted(allowed_task_kinds - conservative_task_kinds)
    if extra_task_kinds:
        blocked.append(f"authorization task kinds are not conservative: {', '.join(extra_task_kinds)}")
    if not auth_result.ok:
        blocked.append("authorization validation failed")
    return blocked


def _preserved_authorization_update_fields(worker: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "backend_kind",
        "worker_id",
        "workspace_root",
        "runtime_backend",
        "runtime_binding_kind",
        "pane_id",
        "pane_pid",
        "process_id",
        "command",
        "user_owned_pane",
        "mixed_ready",
        "allow_mixed_worker",
        "mixed_ready_scope",
        "allowed_mixed_task_kinds",
        "business_ready",
        "allow_business_repo_writes",
        "live_enabled",
    ]
    return {key: worker.get(key) for key in keys if key in worker}


def _load_registry_or_empty(path: Path) -> RegisteredWorkerRegistry:
    if not path.exists():
        return RegisteredWorkerRegistry.empty()
    payload = _read_json(path)
    return RegisteredWorkerRegistry.from_dict(payload)


def _read_record_worker(path: Path) -> dict[str, Any]:
    return _record_payload_to_registry_worker(_read_json(path), record_path=path)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return payload


def _registry_path_allowed(path: Path, roots: list[Path | str]) -> bool:
    resolved = path.resolve()
    text = resolved.as_posix()
    if any(fragment in f"/{text}/" for fragment in FORBIDDEN_REGISTRY_PATH_FRAGMENTS):
        return False
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except ValueError:
            pass
    return False


def _authorization_update_registry_path_allowed(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    text = resolved.as_posix()
    if any(fragment in f"/{text}/" for fragment in FORBIDDEN_REGISTRY_PATH_FRAGMENTS):
        return False
    if text.endswith("/.omx/framework/team/opencode_visible_workers.json"):
        return False
    try:
        resolved.relative_to(root.resolve() / ".omx" / "framework" / "team" / "registered-workers")
        return True
    except ValueError:
        return False


def _create_backup(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    stem = path.name + ".backup"
    backup = path.with_name(stem)
    index = 1
    while backup.exists():
        backup = path.with_name(f"{stem}.{index}")
        index += 1
    if path.exists():
        shutil.copy2(path, backup)
    else:
        backup.write_text(json.dumps({"missing_before_write": True, "registry": payload}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return backup


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        tmp = Path(handle.name)
    tmp.replace(path)
    return tmp


def _resolve_relative(value: str, base_path: Path | None) -> Path:
    path = Path(value)
    if not path.is_absolute() and base_path is not None:
        return base_path / path
    return path


def _registry_invalid(blocked: list[str], *, registry_path: str) -> RegisteredWorkerRegistryValidationResult:
    return RegisteredWorkerRegistryValidationResult(
        ok=False,
        registry_path=registry_path,
        blocked_reasons=_dedupe(blocked),
        warnings=[
            "registered worker registry is not authorization",
            "registered worker registry is not live enable",
            "registered worker registry is not current liveness",
            "registered worker registry does not permit live submit",
        ],
        evidence=[{"source": "registered_worker_registry", "detail": {"registry_path": registry_path}}],
        safe_next_actions=_safe_next_actions(False),
        forbidden_next_actions=_forbidden_next_actions(),
    )


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _safe_next_actions(ok: bool) -> list[str]:
    if ok:
        return [
            "run registered-worker-register-plan",
            "write only with --allow-register to an approved fixture or temp registry path",
            "do not submit live workers",
        ]
    return [
        "fix registered worker registry evidence",
        "rerun validate-registered-worker-registry",
        "do not write registry or submit live workers",
    ]


def _authorization_update_safe_next_actions(ok: bool) -> list[str]:
    if ok:
        return [
            "run registered-worker-authorization-update with explicit allow flags",
            "validate registered worker registry after the update",
            "keep business and mixed live execution forbidden",
        ]
    return [
        "fix authorization update evidence",
        "rerun registered-worker-authorization-update-plan",
        "do not write registry or submit live workers",
    ]


def _forbidden_next_actions() -> list[str]:
    return [
        "live submit",
        "business repo writes",
        "mixed OpenCode plus Claude execution",
        "touch Claude Code pane",
        "tmux send-keys",
        "tmux paste-buffer",
        "invoke claude or claude-code",
        "OpenCode live submit",
        "write the selected runtime .omx/state/team directory",
        "write live registry without explicit --allow-register",
    ]


__all__ = [
    "RegisteredWorkerRegistry",
    "RegisteredWorkerRegistryValidationResult",
    "RegisteredWorkerAuthorizationUpdatePlan",
    "RegisteredWorkerAuthorizationUpdateResult",
    "RegisteredWorkerWritePlan",
    "RegisteredWorkerWriteResult",
    "build_registered_worker_authorization_update_plan",
    "build_registered_worker_register_plan",
    "registry_status",
    "validate_registered_worker_registry_file",
    "validate_registered_worker_registry_payload",
    "write_registered_worker_authorization_update",
    "write_registered_worker_record",
]
