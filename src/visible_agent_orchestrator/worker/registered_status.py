"""Source-only registered worker status and health snapshots.

Registered worker status is inventory observation. It validates local JSON and
optional smoke artifacts, but it does not touch panes, call tmux, call Claude,
submit live work, or imply business/mixed readiness.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .registered_registry import RegisteredWorkerRegistry, validate_registered_worker_registry_file
from .registered_worker import TERMINAL_MARKERS


@dataclass(frozen=True)
class RegisteredWorkerStatus:
    worker_id: str
    backend_kind: str
    workspace_root: str
    runtime_backend: str
    runtime_binding_kind: str
    pane_id: str
    process_id: str
    user_owned_pane: bool
    registration_status: str
    live_enabled: bool
    business_ready: bool
    mixed_ready: bool
    allow_business_repo_writes: bool
    allow_mixed_worker: bool
    last_smoke_artifact: str
    last_smoke_report_present: bool
    last_smoke_terminal_marker: str
    last_smoke_terminal_marker_count: int
    last_smoke_ok: bool
    current_liveness_checked: bool
    current_liveness_ok: bool
    readiness_summary: str
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegisteredWorkerStatusSnapshot:
    registry_path: str
    registry_valid: bool
    worker_count: int
    workers: list[RegisteredWorkerStatus]
    collected_at: str
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.registry_valid and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "diagnostic_scope": "registered_worker_registry",
            "diagnostic_scope_note": (
                "non-visible inventory; separate from dispatchable visible workers; "
                "its safe/forbidden actions apply to this registry inventory only"
            ),
            "registry_path": self.registry_path,
            "registry_valid": self.registry_valid,
            "worker_count": self.worker_count,
            "workers": [worker.to_dict() for worker in self.workers],
            "collected_at": self.collected_at,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "evidence": list(self.evidence),
            "safe_next_actions": list(self.safe_next_actions),
            "forbidden_next_actions": list(self.forbidden_next_actions),
        }


def build_registered_worker_status_snapshot(
    registry_path: Path | str,
    *,
    allow_read_live: bool = False,
) -> RegisteredWorkerStatusSnapshot:
    path = Path(registry_path).resolve()
    validation = validate_registered_worker_registry_file(path)
    registry_payload = _read_registry_payload(path)
    registry = RegisteredWorkerRegistry.from_dict(registry_payload) if registry_payload else RegisteredWorkerRegistry.empty()
    base_path = path.parent
    workers = [_worker_status(worker, base_path=base_path, allow_read_live=allow_read_live) for worker in registry.workers]
    warnings = _dedupe(
        list(validation.warnings)
        + [
            "registered worker status is observation only",
            "registered worker status is not live submit",
            "registered worker status is not business readiness",
            "registered worker status is not mixed readiness",
            "default status collection does not touch panes, tmux, or Claude",
        ]
    )
    if allow_read_live:
        warnings.append("allow_read_live is reserved for read-only liveness checks; no live check is implemented here")
    errors = list(validation.blocked_reasons)
    return RegisteredWorkerStatusSnapshot(
        registry_path=str(path),
        registry_valid=validation.ok,
        worker_count=len(workers),
        workers=workers,
        collected_at=_utc_now(),
        warnings=_dedupe(warnings),
        errors=_dedupe(errors),
        evidence=list(validation.evidence)
        + [
            {
                "source": "registered_worker_status",
                "detail": {
                    "allow_read_live": allow_read_live,
                    "live_liveness_checked": False,
                    "registry_path": str(path),
                },
            }
        ],
        safe_next_actions=_snapshot_safe_next_actions(workers),
        forbidden_next_actions=_forbidden_next_actions(),
    )


def build_registered_worker_health_snapshot(
    registry_path: Path | str,
    *,
    allow_read_live: bool = False,
) -> dict[str, Any]:
    snapshot = build_registered_worker_status_snapshot(registry_path, allow_read_live=allow_read_live)
    workers = [worker.to_dict() for worker in snapshot.workers]
    return {
        "schema_version": 1,
        **snapshot.to_dict(),
        "health_scope": "registered_workers",
        "current_liveness_checked": any(worker["current_liveness_checked"] for worker in workers),
        "current_liveness_ok": all(worker["current_liveness_ok"] for worker in workers) if workers else False,
        "business_ready_workers": [worker["worker_id"] for worker in workers if worker["business_ready"]],
        "mixed_ready_workers": [worker["worker_id"] for worker in workers if worker["mixed_ready"]],
    }


def registered_worker_registry_path(root: Path | str) -> Path:
    return Path(root).resolve() / ".omx" / "framework" / "team" / "registered-workers" / "registered-workers.json"


def source_registered_worker_summary(root: Path | str) -> dict[str, Any]:
    path = registered_worker_registry_path(root)
    if not path.is_file():
        return {
            "present": False,
            "registry_path": str(path),
            "registry_valid": False,
            "worker_count": 0,
            "workers": [],
            "warnings": ["registered worker registry is absent"],
            "errors": [],
        }
    snapshot = build_registered_worker_status_snapshot(path)
    return {"present": True, **snapshot.to_dict()}


def _worker_status(worker: dict[str, Any], *, base_path: Path, allow_read_live: bool) -> RegisteredWorkerStatus:
    worker_id = str(worker.get("worker_id", ""))
    smoke_path = _resolve_path(str(worker.get("last_smoke_artifact", "")), base_path)
    smoke = _smoke_status(smoke_path)
    allow_business = bool(worker.get("allow_business_repo_writes", False))
    allow_mixed = bool(worker.get("allow_mixed_worker", False))
    claimed_business_ready = bool(worker.get("business_ready", False))
    claimed_mixed_ready = bool(worker.get("mixed_ready", False))
    business_ready = bool(claimed_business_ready and allow_business)
    mixed_ready = bool(claimed_mixed_ready and allow_mixed)

    blocked: list[str] = []
    warnings: list[str] = []
    if claimed_business_ready and not allow_business:
        blocked.append("business_ready=true requires allow_business_repo_writes=true")
    if claimed_mixed_ready and not allow_mixed:
        blocked.append("mixed_ready=true requires allow_mixed_worker=true")
    if not smoke["ok"]:
        warnings.append("last smoke artifact missing or invalid")
    if not business_ready:
        warnings.append("business task suggestion is forbidden because business_ready=false")
    if not mixed_ready:
        warnings.append("mixed worker suggestion is forbidden because mixed_ready=false")
    if str(worker.get("backend_kind", "")) == "cursor_agent":
        warnings.append("cursor registered worker path remains disabled")

    readiness = "source-only"
    if smoke["ok"]:
        readiness += "; last_smoke=ok"
    elif smoke["path"]:
        readiness += "; last_smoke=invalid"
    else:
        readiness += "; last_smoke=missing"
    readiness += "; live_liveness=not_checked"

    return RegisteredWorkerStatus(
        worker_id=worker_id,
        backend_kind=str(worker.get("backend_kind", "")),
        workspace_root=str(worker.get("workspace_root", "")),
        runtime_backend=str(worker.get("runtime_backend", "")),
        runtime_binding_kind=str(worker.get("runtime_binding_kind", "")),
        pane_id=str(worker.get("pane_id", "")),
        process_id=str(worker.get("process_id", "")),
        user_owned_pane=bool(worker.get("user_owned_pane", False)),
        registration_status=str(worker.get("registration_status", "")),
        live_enabled=bool(worker.get("live_enabled", False)),
        business_ready=business_ready,
        mixed_ready=mixed_ready,
        allow_business_repo_writes=allow_business,
        allow_mixed_worker=allow_mixed,
        last_smoke_artifact=smoke["path"],
        last_smoke_report_present=smoke["report_present"],
        last_smoke_terminal_marker=smoke["terminal_marker"],
        last_smoke_terminal_marker_count=smoke["terminal_marker_count"],
        last_smoke_ok=smoke["ok"],
        current_liveness_checked=bool(allow_read_live and False),
        current_liveness_ok=False,
        readiness_summary=readiness,
        blocked_reasons=_dedupe(blocked),
        warnings=_dedupe(warnings),
        safe_next_actions=_worker_safe_next_actions(business_ready=business_ready, mixed_ready=mixed_ready),
        forbidden_next_actions=_forbidden_next_actions(),
    )


def _smoke_status(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "path": "",
            "report_present": False,
            "terminal_marker": "",
            "terminal_marker_count": 0,
            "ok": False,
        }
    markers = [marker for marker in TERMINAL_MARKERS if (path / marker).is_file()]
    report_present = (path / "report.md").is_file() and bool((path / "report.md").read_text(encoding="utf-8", errors="replace").strip())
    return {
        "path": str(path),
        "report_present": report_present,
        "terminal_marker": markers[0] if len(markers) == 1 else "",
        "terminal_marker_count": len(markers),
        "ok": report_present and len(markers) == 1,
    }


def _read_registry_payload(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_path(value: str, base_path: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = base_path / path
    return path.resolve()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _snapshot_safe_next_actions(workers: list[RegisteredWorkerStatus]) -> list[str]:
    if not workers:
        return ["create a registered worker record in source fixtures before any live work"]
    return [
        "review registered-worker-health before any business task",
        "keep business tasks forbidden unless business_ready=true and explicitly authorized",
        "keep mixed execution forbidden unless mixed_ready=true and separately reviewed",
    ]


def _worker_safe_next_actions(*, business_ready: bool, mixed_ready: bool) -> list[str]:
    actions = ["rerun source-only registered-worker-status after registry changes"]
    if not business_ready:
        actions.append("do not suggest business task dispatch for this worker")
    if not mixed_ready:
        actions.append("do not suggest mixed OpenCode plus Claude execution")
    return actions


def _forbidden_next_actions() -> list[str]:
    return [
        "live submit",
        "business repo writes",
        "mixed OpenCode plus Claude execution",
        "touch Claude Code pane",
        "tmux send-keys",
        "tmux paste-buffer",
        "invoke claude or claude-code",
        "write the selected runtime .omx/state/team directory",
        "restore or rebind live workers",
    ]


__all__ = [
    "RegisteredWorkerStatus",
    "RegisteredWorkerStatusSnapshot",
    "build_registered_worker_health_snapshot",
    "build_registered_worker_status_snapshot",
    "registered_worker_registry_path",
    "source_registered_worker_summary",
]
