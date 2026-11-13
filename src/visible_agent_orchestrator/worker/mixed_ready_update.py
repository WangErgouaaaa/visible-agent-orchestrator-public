"""Explicit source-only Claude mixed-ready registry update path.

This module plans and, with explicit flags, writes the narrow registry change
that marks one registered Claude worker as eligible for future sandbox/no-op
mixed planning. It never executes live work, touches tmux, calls OpenCode, or
invokes Claude Code.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.visible_registry_lock import visible_registry_path_lock
from visible_agent_orchestrator.runtime_paths import workspace_root

from .claude_mixed_readiness import (
    ClaudeMixedReadinessGateResult,
    build_claude_mixed_readiness_check_from_files,
)
from .registered_registry import RegisteredWorkerRegistry


SCHEMA_VERSION = 1
UPDATE_KIND = "claude-mixed-ready-registry-update"
MIXED_READY_SCOPE = "sandbox_noop_only"
ALLOWED_MIXED_TASK_KINDS = ["mixed_noop_sandbox", "mixed_terminal_protocol_smoke"]
BUSINESS_REPO_ROOT = str(workspace_root())


@dataclass(frozen=True)
class MixedReadyUpdateRequest:
    root: str
    worker_id: str
    registered_worker_registry_path: str
    opencode_visible_registry_path: str
    done_smoke_path: str
    blocked_smoke_path: str
    fail_smoke_path: str
    execution_mode: str = "no-live"
    runtime_telemetry: dict[str, Any] = field(default_factory=dict)
    claude_allowed_write_roots: list[str] = field(default_factory=list)
    opencode_allowed_write_roots: list[str] = field(default_factory=list)
    business_repo_writes_requested: bool = False
    cursor_requested: bool = False
    fixture_only: bool = False
    allow_framework_registry: bool = False
    allow_mixed_ready_update: bool = False
    propose_business_ready: bool = False
    propose_allow_business_repo_writes: bool = False
    readiness_gate_overrides: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MixedReadyUpdatePlan:
    ok: bool
    schema_version: int
    update_id: str
    created_at: str
    update_kind: str
    backend_kind: str
    worker_id: str
    registry_path: str
    readiness_gate_result: dict[str, Any]
    readiness_gate_evidence: list[dict[str, Any]]
    current_registry_record: dict[str, Any]
    proposed_registry_record: dict[str, Any]
    changed_fields: list[str]
    business_ready_before: bool
    business_ready_after: bool
    mixed_ready_before: bool
    mixed_ready_after: bool
    allow_business_repo_writes_before: bool
    allow_business_repo_writes_after: bool
    allow_mixed_worker_before: bool
    allow_mixed_worker_after: bool
    mixed_ready_scope: str
    allowed_mixed_task_kinds: list[str]
    would_write_registry: bool
    would_execute_live: bool
    requires_allow_mixed_ready_update: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def readiness_ok(self) -> bool:
        return bool(self.readiness_gate_result.get("ok", False))

    @property
    def mixed_ready_candidate(self) -> bool:
        return bool(self.readiness_gate_result.get("mixed_ready_candidate", False))

    @property
    def business_ready_candidate(self) -> bool:
        return bool(self.readiness_gate_result.get("business_ready_candidate", False))

    @property
    def current_mixed_ready(self) -> bool:
        return self.mixed_ready_before

    @property
    def proposed_mixed_ready(self) -> bool:
        return self.mixed_ready_after

    @property
    def current_business_ready(self) -> bool:
        return self.business_ready_before

    @property
    def proposed_business_ready(self) -> bool:
        return self.business_ready_after

    @property
    def current_allow_business_repo_writes(self) -> bool:
        return self.allow_business_repo_writes_before

    @property
    def proposed_allow_business_repo_writes(self) -> bool:
        return self.allow_business_repo_writes_after

    @property
    def current_allow_mixed_worker(self) -> bool:
        return self.allow_mixed_worker_before

    @property
    def proposed_allow_mixed_worker(self) -> bool:
        return self.allow_mixed_worker_after

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "readiness_ok": self.readiness_ok,
                "mixed_ready_candidate": self.mixed_ready_candidate,
                "business_ready_candidate": self.business_ready_candidate,
                "current_mixed_ready": self.current_mixed_ready,
                "proposed_mixed_ready": self.proposed_mixed_ready,
                "current_business_ready": self.current_business_ready,
                "proposed_business_ready": self.proposed_business_ready,
                "current_allow_business_repo_writes": self.current_allow_business_repo_writes,
                "proposed_allow_business_repo_writes": self.proposed_allow_business_repo_writes,
                "current_allow_mixed_worker": self.current_allow_mixed_worker,
                "proposed_allow_mixed_worker": self.proposed_allow_mixed_worker,
            }
        )
        return payload


@dataclass(frozen=True)
class MixedReadyUpdateResult:
    attempted: bool
    wrote_registry: bool
    registry_path: str
    backup_path: str
    worker_id: str
    changed_fields: list[str]
    business_ready_after: bool
    mixed_ready_after: bool
    allow_business_repo_writes_after: bool
    allow_mixed_worker_after: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.wrote_registry and not self.blocked_reasons

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, **asdict(self)}


def build_mixed_ready_update_plan(request: MixedReadyUpdateRequest) -> MixedReadyUpdatePlan:
    gate = _build_gate(request)
    return _plan_from_gate(request, gate)


def _write_mixed_ready_update(
    request: MixedReadyUpdateRequest,
    *,
    plan: MixedReadyUpdatePlan | None = None,
) -> MixedReadyUpdateResult:
    plan = plan or build_mixed_ready_update_plan(request)
    blocked = list(plan.blocked_reasons)
    if not request.allow_mixed_ready_update:
        blocked.append("missing --allow-mixed-ready-update")
    if not request.allow_framework_registry:
        blocked.append("missing --allow-framework-registry")
    blocked = _dedupe(blocked)
    if blocked:
        return MixedReadyUpdateResult(
            attempted=bool(request.allow_mixed_ready_update),
            wrote_registry=False,
            registry_path=plan.registry_path,
            backup_path="",
            worker_id=plan.worker_id,
            changed_fields=plan.changed_fields,
            business_ready_after=plan.business_ready_after,
            mixed_ready_after=plan.mixed_ready_after,
            allow_business_repo_writes_after=plan.allow_business_repo_writes_after,
            allow_mixed_worker_after=plan.allow_mixed_worker_after,
            blocked_reasons=blocked,
            warnings=plan.warnings,
            evidence=plan.evidence,
        )

    registry_path = Path(request.registered_worker_registry_path).resolve()
    registry_payload = _read_json(registry_path)
    registry = RegisteredWorkerRegistry.from_dict(registry_payload)
    workers = []
    replaced = False
    for worker in registry.workers:
        if str(worker.get("worker_id", "")) == request.worker_id:
            workers.append(plan.proposed_registry_record)
            replaced = True
        else:
            workers.append(worker)
    if not replaced:
        return MixedReadyUpdateResult(
            attempted=True,
            wrote_registry=False,
            registry_path=str(registry_path),
            backup_path="",
            worker_id=request.worker_id,
            changed_fields=[],
            business_ready_after=False,
            mixed_ready_after=False,
            allow_business_repo_writes_after=False,
            allow_mixed_worker_after=False,
            blocked_reasons=[f"worker missing at write time: {request.worker_id}"],
            warnings=plan.warnings,
            evidence=plan.evidence,
        )

    next_payload = {
        **registry.to_dict(),
        "updated_at": _utc_now(),
        "workers": workers,
        "metadata": {
            **_dict(registry_payload.get("metadata", {})),
            "last_write_source": UPDATE_KIND,
            "last_mixed_ready_update_id": plan.update_id,
        },
    }
    backup = _create_backup(registry_path, registry_payload)
    _atomic_write_json(registry_path, next_payload)
    return MixedReadyUpdateResult(
        attempted=True,
        wrote_registry=True,
        registry_path=str(registry_path),
        backup_path=str(backup),
        worker_id=request.worker_id,
        changed_fields=plan.changed_fields,
        business_ready_after=plan.business_ready_after,
        mixed_ready_after=plan.mixed_ready_after,
        allow_business_repo_writes_after=plan.allow_business_repo_writes_after,
        allow_mixed_worker_after=plan.allow_mixed_worker_after,
        blocked_reasons=[],
        warnings=plan.warnings,
        evidence=plan.evidence + [{"source": "atomic_registry_write", "detail": {"backup_path": str(backup)}}],
    )


def write_mixed_ready_update(
    request: MixedReadyUpdateRequest,
) -> MixedReadyUpdateResult:
    if (
        not request.allow_mixed_ready_update
        or not request.allow_framework_registry
    ):
        return _write_mixed_ready_update(request)
    registry_path = Path(request.registered_worker_registry_path).resolve()
    with visible_registry_path_lock(registry_path):
        locked_plan = build_mixed_ready_update_plan(request)
        return _write_mixed_ready_update(request, plan=locked_plan)


def validate_mixed_ready_update_record(payload: dict[str, Any]) -> dict[str, Any]:
    blocked: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        blocked.append("schema_version must be 1")
    if payload.get("update_kind") != UPDATE_KIND:
        blocked.append(f"update_kind must be {UPDATE_KIND}")
    if payload.get("backend_kind") != "claude_code":
        blocked.append("backend_kind must be claude_code")
    if payload.get("business_ready_after") is not False:
        blocked.append("business_ready_after must remain false")
    if payload.get("allow_business_repo_writes_after") is not False:
        blocked.append("allow_business_repo_writes_after must remain false")
    if payload.get("would_execute_live") is not False:
        blocked.append("would_execute_live must be false")
    if payload.get("mixed_ready_scope") != MIXED_READY_SCOPE:
        blocked.append(f"mixed_ready_scope must be {MIXED_READY_SCOPE}")
    allowed = payload.get("allowed_mixed_task_kinds", [])
    if allowed != ALLOWED_MIXED_TASK_KINDS:
        blocked.append("allowed_mixed_task_kinds must stay sandbox/no-op only")
    if payload.get("proposed_business_ready") is True:
        blocked.append("proposed business_ready=true is forbidden")
    if payload.get("proposed_allow_business_repo_writes") is True:
        blocked.append("proposed allow_business_repo_writes=true is forbidden")
    blocked = _dedupe(blocked)
    return {
        "schema_version": 1,
        "ok": not blocked,
        "blocked_reasons": blocked,
        "warnings": [
            "validation is source-only",
            "mixed_ready does not authorize business tasks or live execution",
        ],
        "evidence": [{"source": "mixed_ready_update_record", "detail": {"worker_id": payload.get("worker_id", "")}}],
    }


def _build_gate(request: MixedReadyUpdateRequest) -> ClaudeMixedReadinessGateResult:
    gate = build_claude_mixed_readiness_check_from_files(
        root=request.root,
        worker_id=request.worker_id,
        registered_worker_registry_path=request.registered_worker_registry_path,
        opencode_visible_registry_path=request.opencode_visible_registry_path,
        done_smoke_path=request.done_smoke_path,
        blocked_smoke_path=request.blocked_smoke_path,
        fail_smoke_path=request.fail_smoke_path,
        execution_mode=request.execution_mode,
        runtime_telemetry=request.runtime_telemetry,
        claude_allowed_write_roots=request.claude_allowed_write_roots,
        opencode_allowed_write_roots=request.opencode_allowed_write_roots,
        business_repo_writes_requested=request.business_repo_writes_requested,
        cursor_requested=request.cursor_requested,
        fixture_only=request.fixture_only,
        fixture_path=Path(request.registered_worker_registry_path).resolve().parent if request.fixture_only else None,
    )
    if not request.readiness_gate_overrides:
        return gate
    data = gate.to_dict()
    result = dict(data.get("result", {}))
    for key, value in request.readiness_gate_overrides.items():
        data[key] = value
        result[key] = value
    data["result"] = result
    # Re-plan against the dict view when tests need a synthetic bad upstream gate.
    return _GateView(data)  # type: ignore[return-value]


def _plan_from_gate(request: MixedReadyUpdateRequest, gate: ClaudeMixedReadinessGateResult) -> MixedReadyUpdatePlan:
    gate_data = gate.to_dict()
    gate_result = _dict(gate_data.get("result", {}))
    current = dict(gate_data.get("registered_worker_record", {}) or {})
    registry_path = Path(request.registered_worker_registry_path).resolve()
    root = Path(request.root).resolve()
    blocked: list[str] = list(gate_data.get("blocked_reasons", []))

    if not registry_path.is_file():
        blocked.append(f"registry missing: {registry_path}")
    if not current:
        blocked.append(f"worker missing: {request.worker_id}")
    if str(current.get("backend_kind", "")) != "claude_code":
        blocked.append("backend_kind != claude_code")
    if not gate_result:
        blocked.append("readiness gate missing")
    if gate_result.get("ok") is not True:
        blocked.append("readiness gate ok=false")
    if gate_result.get("mixed_ready_candidate") is not True:
        blocked.append("mixed_ready_candidate=false")
    if gate_result.get("business_ready_candidate") is True:
        blocked.append("business_ready_candidate=true is forbidden for mixed-ready update")
    if _active_lock_count(gate_result) > 0:
        blocked.append("active locks > 0")
    if _watcher_alert_count(gate_result) > 0:
        blocked.append("watcher alerts > 0")
    if not bool(gate_result.get("terminal_protocol_ok", False)):
        blocked.append("missing done/blocked/fail evidence")
    if not bool(gate_result.get("write_scope_disjoint", False)):
        blocked.append("write scope overlap")
    if request.execution_mode != "no-live":
        blocked.append("execution_mode must be no-live")
    if request.cursor_requested:
        blocked.append("cursor requested but disabled / out of scope")
    if not _is_framework_registered_worker_path(root, registry_path):
        blocked.append(f"registry path outside allowed framework registered-workers root: {registry_path}")
    if ".omx/state/team" in registry_path.as_posix():
        blocked.append(".omx/state/team registry path refused")
    if registry_path.name == "opencode_visible_workers.json" or registry_path == Path(request.opencode_visible_registry_path).resolve():
        blocked.append("OpenCode visible registry path refused")
    if _paths_overlap(str(registry_path), BUSINESS_REPO_ROOT):
        blocked.append("business repo path refused")

    business_after = bool(request.propose_business_ready)
    allow_business_after = bool(request.propose_allow_business_repo_writes)
    if business_after:
        blocked.append("proposed business_ready=true is forbidden")
    if allow_business_after:
        blocked.append("proposed allow_business_repo_writes=true is forbidden")

    proposed = {
        **current,
        "business_ready": False,
        "allow_business_repo_writes": False,
        "mixed_ready": True,
        "allow_mixed_worker": True,
        "mixed_ready_scope": MIXED_READY_SCOPE,
        "allowed_mixed_task_kinds": list(ALLOWED_MIXED_TASK_KINDS),
        "mixed_ready_updated_at": _utc_now(),
        "mixed_ready_update_source": UPDATE_KIND,
    }
    if request.propose_business_ready:
        proposed["business_ready"] = True
    if request.propose_allow_business_repo_writes:
        proposed["allow_business_repo_writes"] = True

    changed = [
        key
        for key in (
            "mixed_ready",
            "allow_mixed_worker",
            "mixed_ready_scope",
            "allowed_mixed_task_kinds",
            "business_ready",
            "allow_business_repo_writes",
        )
        if current.get(key) != proposed.get(key)
    ]
    blocked = _dedupe(blocked)
    ok = not blocked
    return MixedReadyUpdatePlan(
        ok=ok,
        schema_version=SCHEMA_VERSION,
        update_id=f"mixed-ready-update-{request.worker_id}-{_utc_now()}",
        created_at=_utc_now(),
        update_kind=UPDATE_KIND,
        backend_kind=str(current.get("backend_kind", "claude_code")),
        worker_id=request.worker_id,
        registry_path=str(registry_path),
        readiness_gate_result=gate_result,
        readiness_gate_evidence=list(gate_data.get("evidence", [])),
        current_registry_record=current,
        proposed_registry_record=proposed,
        changed_fields=changed,
        business_ready_before=bool(current.get("business_ready", False)),
        business_ready_after=bool(proposed.get("business_ready", False)),
        mixed_ready_before=bool(current.get("mixed_ready", False)),
        mixed_ready_after=bool(proposed.get("mixed_ready", False)),
        allow_business_repo_writes_before=bool(current.get("allow_business_repo_writes", False)),
        allow_business_repo_writes_after=bool(proposed.get("allow_business_repo_writes", False)),
        allow_mixed_worker_before=bool(current.get("allow_mixed_worker", False)),
        allow_mixed_worker_after=bool(proposed.get("allow_mixed_worker", False)),
        mixed_ready_scope=MIXED_READY_SCOPE,
        allowed_mixed_task_kinds=list(ALLOWED_MIXED_TASK_KINDS),
        would_write_registry=False,
        would_execute_live=False,
        requires_allow_mixed_ready_update=True,
        blocked_reasons=blocked,
        warnings=_dedupe(
            list(gate_data.get("warnings", []))
            + [
                "mixed_ready is sandbox/no-op only",
                "business_ready remains false",
                "allow_business_repo_writes remains false",
                "would_execute_live=false",
            ]
        ),
        safe_next_actions=_safe_next_actions(ok),
        forbidden_next_actions=_forbidden_next_actions(),
        evidence=[
            {"source": "claude_mixed_readiness_gate", "detail": gate_result},
            {"source": "registry_path_policy", "detail": {"registry_path": str(registry_path), "root": str(root)}},
        ],
    )


def _is_framework_registered_worker_path(root: Path, registry_path: Path) -> bool:
    allowed = root / ".omx" / "framework" / "team" / "registered-workers"
    try:
        registry_path.relative_to(allowed.resolve())
        return registry_path.name == "registered-workers.json"
    except ValueError:
        return False


def _active_lock_count(payload: dict[str, Any]) -> int:
    if "no_active_locks" in payload and payload["no_active_locks"] is False:
        return 1
    return 0


def _watcher_alert_count(payload: dict[str, Any]) -> int:
    if "no_watcher_alerts" in payload and payload["no_watcher_alerts"] is False:
        return 1
    return 0


def _paths_overlap(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_path = Path(left).resolve()
    right_path = Path(right).resolve()
    return left_path == right_path or left_path in right_path.parents or right_path in left_path.parents


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return data


def _create_backup(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".backup")
    index = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup.{index}")
        index += 1
    if path.exists():
        shutil.copy2(path, backup)
    else:
        backup.write_text(json.dumps({"missing_before_write": True, "registry": payload}, indent=2) + "\n", encoding="utf-8")
    return backup


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        tmp = Path(handle.name)
    tmp.replace(path)


def _safe_next_actions(ok: bool) -> list[str]:
    if ok:
        return [
            "run claude-mixed-ready-update with explicit --allow-framework-registry and --allow-mixed-ready-update in a temp/sandbox root",
            "run sandbox/no-op mixed planning only",
        ]
    return ["fix source-only readiness evidence and rerun claude-mixed-ready-update-plan"]


def _forbidden_next_actions() -> list[str]:
    return [
        "business task dispatch",
        "business repo writes",
        "mixed live execution",
        "Claude Code live submit",
        "OpenCode live submit",
        "tmux input",
        "Cursor execution",
        "Obsidian writes",
    ]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _GateView:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, Any]:
        return self._payload


__all__ = [
    "MixedReadyUpdatePlan",
    "MixedReadyUpdateRequest",
    "MixedReadyUpdateResult",
    "build_mixed_ready_update_plan",
    "validate_mixed_ready_update_record",
    "write_mixed_ready_update",
]
