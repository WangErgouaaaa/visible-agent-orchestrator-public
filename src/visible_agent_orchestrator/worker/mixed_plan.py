"""Source-only mixed worker planning for OpenCode and registered Claude.

The planner reads local JSON evidence and fixture data only. It does not submit
work, call tmux, call OpenCode, call Claude Code, write registries, or infer
business/mixed readiness from inventory alone.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime_paths import workspace_root

from visible_agent_orchestrator.runtime.backends import default_backend_name
from .registered_registry import RegisteredWorkerRegistry, validate_registered_worker_registry_file
from .registered_worker import TERMINAL_MARKERS


PLAN_SCHEMA_VERSION = 1
DEFAULT_FORBIDDEN_PATHS = [str(workspace_root())]


@dataclass(frozen=True)
class MixedWorkerGateResult:
    gate: str
    ok: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MixedWorkerCandidate:
    worker_id: str
    backend_kind: str
    source: dict[str, bool]
    workspace_root: str
    runtime_backend: str
    live_capable: bool
    registered: bool
    last_smoke_ok: bool
    done_smoke_ok: bool
    blocked_smoke_ok: bool
    fail_smoke_ok: bool
    business_ready: bool
    mixed_ready: bool
    allow_business_repo_writes: bool
    allow_mixed_worker: bool
    allowed_write_roots: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    required_gates: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MixedWorkerPlanRequest:
    root: str
    plan_id: str = "mixed-worker-plan"
    plan_kind: str = "opencode_claude_mixed_worker_planning"
    runtime_backend: str = default_backend_name()
    include_backends: list[str] = field(default_factory=lambda: ["opencode_tui", "claude_code"])
    execution_mode: str = "no-live"
    registered_worker_registry_path: str = ""
    opencode_visible_registry_path: str = ""
    runtime_telemetry: dict[str, Any] = field(default_factory=dict)
    allowed_write_roots: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=lambda: list(DEFAULT_FORBIDDEN_PATHS))
    business_repo_writes_requested: bool = False
    cursor_requested: bool = False
    fixture_only: bool = False
    fixture_path: str = ""
    smoke_search_roots: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, fixture_path: str = "") -> "MixedWorkerPlanRequest":
        return cls(
            root=str(payload.get("root", "")),
            plan_id=str(payload.get("plan_id", "mixed-worker-plan")),
            plan_kind=str(payload.get("plan_kind", "opencode_claude_mixed_worker_planning")),
            runtime_backend=str(payload.get("runtime_backend", default_backend_name())),
            include_backends=[str(item) for item in payload.get("include_backends", ["opencode_tui", "claude_code"])],
            execution_mode=str(payload.get("execution_mode", "no-live")),
            registered_worker_registry_path=str(payload.get("registered_worker_registry_path", "")),
            opencode_visible_registry_path=str(payload.get("opencode_visible_registry_path", "")),
            runtime_telemetry=_dict(payload.get("runtime_telemetry", {})),
            allowed_write_roots=[str(item) for item in payload.get("allowed_write_roots", [])],
            forbidden_paths=[str(item) for item in payload.get("forbidden_paths", DEFAULT_FORBIDDEN_PATHS)],
            business_repo_writes_requested=bool(payload.get("business_repo_writes_requested", False)),
            cursor_requested=bool(payload.get("cursor_requested", False)),
            fixture_only=bool(payload.get("fixture_only", bool(fixture_path))),
            fixture_path=fixture_path or str(payload.get("fixture_path", "")),
            smoke_search_roots=[str(item) for item in payload.get("smoke_search_roots", [])],
            evidence=[_dict(item) for item in payload.get("evidence", []) if isinstance(item, dict)],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MixedWorkerPlanResult:
    ok: bool
    plan_id: str
    created_at: str
    plan_kind: str
    root: str
    runtime_backend: str
    would_execute_live: bool
    mixed_ready: bool
    business_ready: bool
    worker_count: int
    opencode_worker_count: int
    claude_worker_count: int
    cursor_worker_count: int
    workers: list[MixedWorkerCandidate]
    worker_roles: dict[str, str]
    task_matrix: list[dict[str, Any]]
    allowed_write_roots: list[str]
    forbidden_paths: list[str]
    business_repo_policy: dict[str, Any]
    readiness_gates: list[MixedWorkerGateResult]
    terminal_protocol_gates: list[MixedWorkerGateResult]
    lock_gates: list[MixedWorkerGateResult]
    health_gates: list[MixedWorkerGateResult]
    smoke_requirements: dict[str, Any]
    execution_mode: str
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "plan_kind": self.plan_kind,
            "root": self.root,
            "runtime_backend": self.runtime_backend,
            "workers": [worker.to_dict() for worker in self.workers],
            "worker_roles": self.worker_roles,
            "task_matrix": self.task_matrix,
            "allowed_write_roots": list(self.allowed_write_roots),
            "forbidden_paths": list(self.forbidden_paths),
            "business_repo_policy": self.business_repo_policy,
            "readiness_gates": [gate.to_dict() for gate in self.readiness_gates],
            "terminal_protocol_gates": [gate.to_dict() for gate in self.terminal_protocol_gates],
            "lock_gates": [gate.to_dict() for gate in self.lock_gates],
            "health_gates": [gate.to_dict() for gate in self.health_gates],
            "smoke_requirements": self.smoke_requirements,
            "execution_mode": self.execution_mode,
            "would_execute_live": self.would_execute_live,
            "mixed_ready": self.mixed_ready,
            "business_ready": self.business_ready,
            "worker_count": self.worker_count,
            "opencode_worker_count": self.opencode_worker_count,
            "claude_worker_count": self.claude_worker_count,
            "cursor_worker_count": self.cursor_worker_count,
            "blocked_reasons": list(self.blocked_reasons),
            "warnings": list(self.warnings),
            "safe_next_actions": list(self.safe_next_actions),
            "forbidden_next_actions": list(self.forbidden_next_actions),
            "evidence": list(self.evidence),
        }


def build_mixed_worker_plan_from_files(
    *,
    root: Path | str,
    registered_worker_registry_path: Path | str,
    opencode_visible_registry_path: Path | str,
    include_backends: list[str] | None = None,
    execution_mode: str = "no-live",
    business_repo_writes_requested: bool = False,
    runtime_telemetry: dict[str, Any] | None = None,
    fixture_only: bool = False,
    fixture_path: Path | str | None = None,
) -> MixedWorkerPlanResult:
    request = MixedWorkerPlanRequest(
        root=str(Path(root).resolve()),
        include_backends=list(include_backends or ["opencode_tui", "claude_code"]),
        execution_mode=execution_mode,
        registered_worker_registry_path=str(Path(registered_worker_registry_path).resolve()),
        opencode_visible_registry_path=str(Path(opencode_visible_registry_path).resolve()),
        runtime_telemetry=_dict(runtime_telemetry or {}),
        business_repo_writes_requested=business_repo_writes_requested,
        fixture_only=fixture_only,
        fixture_path=str(Path(fixture_path).resolve()) if fixture_path else "",
    )
    return build_mixed_worker_plan(request)


def build_mixed_worker_fixture_plan(fixture_dir: Path | str) -> MixedWorkerPlanResult:
    fixture = Path(fixture_dir).resolve()
    request_payload = _read_json(fixture / "request.json")
    request = MixedWorkerPlanRequest.from_dict(request_payload, fixture_path=str(fixture))
    root = fixture
    registered_path = fixture / "registered-workers.json"
    opencode_path = fixture / "opencode_visible_workers.json"
    telemetry_path = fixture / "runtime-telemetry.json"
    request = MixedWorkerPlanRequest(
        **{
            **request.to_dict(),
            "root": str(root),
            "registered_worker_registry_path": str(registered_path),
            "opencode_visible_registry_path": str(opencode_path),
            "runtime_telemetry": _read_json(telemetry_path),
            "fixture_only": True,
            "fixture_path": str(fixture),
            "smoke_search_roots": [str(fixture / "smoke")],
        }
    )
    return build_mixed_worker_plan(request)


def build_mixed_worker_plan(request: MixedWorkerPlanRequest) -> MixedWorkerPlanResult:
    root = Path(request.root).resolve() if request.root else Path(".").resolve()
    include = {normalize_backend_name(item) for item in request.include_backends}
    warnings = [
        "mixed worker planning is source-only and fixture-only in this phase",
        "mixed worker planning is not mixed live execution",
        "mixed worker planning is not business readiness",
        "mixed worker planning is not a registry write",
    ]
    blocked: list[str] = []
    evidence = list(request.evidence)

    would_execute_live = False
    if request.execution_mode != "no-live":
        blocked.append("mixed live execution is disabled; execution_mode must be no-live")
    if "cursor_agent" in include or request.cursor_requested:
        blocked.append("cursor requested but disabled unless a future fixture-only gate explicitly enables it")

    registered_payload = _read_json_or_empty(Path(request.registered_worker_registry_path))
    opencode_payload, opencode_present = _read_optional_json(Path(request.opencode_visible_registry_path))

    claude_workers, registry_evidence, registry_blocks = _registered_claude_candidates(
        registered_payload=registered_payload,
        registry_path=Path(request.registered_worker_registry_path),
        request=request,
    )
    evidence.extend(registry_evidence)
    blocked.extend(registry_blocks)

    opencode_workers, opencode_evidence, opencode_blocks = _opencode_candidates(
        opencode_payload=opencode_payload,
        registry_path=Path(request.opencode_visible_registry_path),
        registry_present=opencode_present,
        include_opencode="opencode_tui" in include,
        request=request,
    )
    evidence.extend(opencode_evidence)
    blocked.extend(opencode_blocks)

    workers = [*opencode_workers, *claude_workers]
    if "claude_code" in include and not claude_workers:
        blocked.append("no registered Claude worker")
    if "opencode_tui" in include and not opencode_workers:
        blocked.append("OpenCode registry missing or has no usable OpenCode worker")

    blocked.extend(_business_blocks(request, workers))
    blocked.extend(_mixed_readiness_blocks(workers, include))
    blocked.extend(_write_scope_blocks(workers))
    blocked.extend(reason for worker in workers for reason in worker.blocked_reasons)
    runtime = _runtime_telemetry(root, request)
    blocked.extend(_runtime_blocks(runtime))
    blocked.extend(_terminal_protocol_blocks(workers))

    readiness_gates = [
        _gate("claude_registered", bool(claude_workers), "no registered Claude worker"),
        _gate("opencode_registry_present", bool(opencode_workers) if "opencode_tui" in include else True, "OpenCode registry missing or has no usable OpenCode worker"),
        _gate("no_cursor", not ("cursor_agent" in include or request.cursor_requested), "cursor requested but disabled unless a future fixture-only gate explicitly enables it"),
        _gate("mixed_ready_claims", not _mixed_readiness_blocks(workers, include), "; ".join(_mixed_readiness_blocks(workers, include))),
        _gate("business_repo_policy", not _business_blocks(request, workers), "; ".join(_business_blocks(request, workers))),
    ]
    terminal_gates = [
        _gate("done_blocked_fail_terminal_evidence", not _terminal_protocol_blocks(workers), "; ".join(_terminal_protocol_blocks(workers)))
    ]
    lock_gates = [_gate("no_active_locks", not _runtime_lock_blocks(runtime), "; ".join(_runtime_lock_blocks(runtime)))]
    health_gates = [_gate("no_watcher_alerts", not _runtime_alert_blocks(runtime), "; ".join(_runtime_alert_blocks(runtime)))]

    blocked = _dedupe(blocked)
    warnings = _dedupe(warnings + _worker_warnings(workers))
    mixed_ready = bool(workers) and not blocked and all(worker.mixed_ready for worker in workers)
    business_ready = bool(workers) and all(worker.business_ready for worker in workers)
    ok = not blocked

    return MixedWorkerPlanResult(
        ok=ok,
        plan_id=request.plan_id,
        created_at=_utc_now(),
        plan_kind=request.plan_kind,
        root=str(root),
        runtime_backend=request.runtime_backend,
        would_execute_live=would_execute_live,
        mixed_ready=mixed_ready,
        business_ready=business_ready,
        worker_count=len(workers),
        opencode_worker_count=sum(1 for worker in workers if worker.backend_kind == "opencode_tui"),
        claude_worker_count=sum(1 for worker in workers if worker.backend_kind == "claude_code"),
        cursor_worker_count=sum(1 for worker in workers if worker.backend_kind == "cursor_agent"),
        workers=workers,
        worker_roles=_worker_roles(workers),
        task_matrix=_task_matrix(workers),
        allowed_write_roots=_dedupe([root for worker in workers for root in worker.allowed_write_roots] + list(request.allowed_write_roots)),
        forbidden_paths=_dedupe([path for worker in workers for path in worker.forbidden_paths] + list(request.forbidden_paths)),
        business_repo_policy={
            "business_repo_writes_requested": request.business_repo_writes_requested,
            "business_repo_writes_allowed": False,
            "business_ready": business_ready,
            "reason": "business repo writes remain forbidden unless a future explicit business gate is added",
        },
        readiness_gates=readiness_gates,
        terminal_protocol_gates=terminal_gates,
        lock_gates=lock_gates,
        health_gates=health_gates,
        smoke_requirements={
            "required_markers": [".done", ".blocked", ".fail"],
            "require_nonempty_report": True,
            "all_workers_must_have_done_blocked_fail_evidence": True,
        },
        execution_mode=request.execution_mode,
        blocked_reasons=blocked,
        warnings=warnings,
        safe_next_actions=[
            "source-only mixed contract",
            "registered Claude fail/blocked evidence already present when recorded by fixture or smoke artifacts",
            "next maybe fixture mixed smoke",
            "no live mixed yet",
        ],
        forbidden_next_actions=[
            "mixed live execution",
            "business repo writes",
            "registry writes",
            "OpenCode live submit",
            "Claude Code live submit",
            "tmux input",
            "cursor-agent execution",
        ],
        evidence=_dedupe_evidence(
            evidence
            + [
                {"source": "mixed_worker_plan", "detail": {"execution_mode": request.execution_mode, "fixture_only": request.fixture_only}},
                {"source": "runtime_telemetry", "detail": runtime},
            ]
        ),
    )


def normalize_backend_name(value: str) -> str:
    aliases = {
        "opencode-tui": "opencode_tui",
        "opencode_tui": "opencode_tui",
        "claude-code-fixture": "claude_code",
        "claude_code": "claude_code",
        "cursor-agent-fixture": "cursor_agent",
        "cursor_agent": "cursor_agent",
    }
    return aliases.get(str(value), str(value))


def _registered_claude_candidates(
    *,
    registered_payload: dict[str, Any],
    registry_path: Path,
    request: MixedWorkerPlanRequest,
) -> tuple[list[MixedWorkerCandidate], list[dict[str, Any]], list[str]]:
    evidence: list[dict[str, Any]] = []
    blocked: list[str] = []
    if not registered_payload:
        return [], [{"source": "registered_worker_registry", "detail": {"present": False, "registry_path": str(registry_path)}}], []
    if request.fixture_only:
        evidence.append(
            {
                "source": "registered_worker_registry",
                "detail": {"registry_path": str(registry_path), "fixture_only": True},
            }
        )
    else:
        validation = validate_registered_worker_registry_file(registry_path)
        evidence.extend(validation.evidence)
        blocked.extend(validation.blocked_reasons)
    registry = RegisteredWorkerRegistry.from_dict(registered_payload)
    base_path = registry_path.parent
    candidates = [
        _registered_worker_candidate(worker, base_path=base_path, request=request)
        for worker in registry.workers
        if str(worker.get("backend_kind", "")) == "claude_code"
    ]
    return candidates, evidence, blocked


def _registered_worker_candidate(
    worker: dict[str, Any],
    *,
    base_path: Path,
    request: MixedWorkerPlanRequest,
) -> MixedWorkerCandidate:
    smoke = _smoke_evidence(worker, base_path=base_path, request=request, backend_kind="claude_code")
    allow_business = bool(worker.get("allow_business_repo_writes", False))
    allow_mixed = bool(worker.get("allow_mixed_worker", False))
    business_ready = bool(worker.get("business_ready", False) and allow_business)
    mixed_ready = bool(worker.get("mixed_ready", False) and allow_mixed)
    blocked: list[str] = []
    warnings: list[str] = []
    if not business_ready:
        warnings.append("business task suggestion is forbidden because business_ready=false")
    if not mixed_ready:
        blocked.append(f"worker {worker.get('worker_id', '<missing>')} mixed_ready=false blocks mixed live")
    if not all(smoke[key] for key in ("done_smoke_ok", "blocked_smoke_ok", "fail_smoke_ok")):
        blocked.append(f"worker {worker.get('worker_id', '<missing>')} lacks done/blocked/fail terminal evidence")
    return MixedWorkerCandidate(
        worker_id=str(worker.get("worker_id", "")),
        backend_kind="claude_code",
        source={"opencode_visible_registry": False, "registered_worker_registry": True, "fixture": request.fixture_only},
        workspace_root=str(worker.get("workspace_root", "")),
        runtime_backend=str(worker.get("runtime_backend", request.runtime_backend)),
        live_capable=bool(worker.get("live_enabled", False)),
        registered=True,
        last_smoke_ok=smoke["last_smoke_ok"],
        done_smoke_ok=smoke["done_smoke_ok"],
        blocked_smoke_ok=smoke["blocked_smoke_ok"],
        fail_smoke_ok=smoke["fail_smoke_ok"],
        business_ready=business_ready,
        mixed_ready=mixed_ready,
        allow_business_repo_writes=allow_business,
        allow_mixed_worker=allow_mixed,
        allowed_write_roots=[str(item) for item in worker.get("allowed_write_roots", [])],
        forbidden_paths=[str(item) for item in worker.get("forbidden_paths", [])],
        required_gates=["business_ready", "mixed_ready", "done_smoke", "blocked_smoke", "fail_smoke"],
        blocked_reasons=_dedupe(blocked),
        warnings=_dedupe(warnings),
        evidence=smoke["evidence"],
    )


def _opencode_candidates(
    *,
    opencode_payload: dict[str, Any],
    registry_path: Path,
    registry_present: bool,
    include_opencode: bool,
    request: MixedWorkerPlanRequest,
) -> tuple[list[MixedWorkerCandidate], list[dict[str, Any]], list[str]]:
    evidence = [{"source": "opencode_visible_registry", "detail": {"present": registry_present, "registry_path": str(registry_path)}}]
    if not include_opencode:
        return [], evidence, []
    if not registry_present:
        return [], evidence, ["OpenCode registry missing"]
    workers = opencode_payload.get("workers", [])
    if not isinstance(workers, list):
        return [], evidence, ["OpenCode registry workers must be a list"]
    candidates = [_opencode_worker_candidate(worker, request=request) for worker in workers if _is_opencode_worker(worker)]
    return candidates, evidence, []


def _opencode_worker_candidate(worker: dict[str, Any], *, request: MixedWorkerPlanRequest) -> MixedWorkerCandidate:
    smoke = _smoke_evidence(worker, base_path=Path(request.fixture_path or request.root), request=request, backend_kind="opencode_tui")
    blocked: list[str] = []
    warnings: list[str] = []
    worker_id = str(worker.get("worker_id", ""))
    binding = str(worker.get("binding_confidence", ""))
    if binding != "strong":
        blocked.append(f"OpenCode worker {worker_id} binding_confidence is not strong")
    if not bool(worker.get("allow_auto_submit", False)):
        blocked.append(f"OpenCode worker {worker_id} allow_auto_submit=false")
    if not bool(worker.get("worker_scoped_lock_evidence_ok", False)):
        blocked.append(f"OpenCode worker {worker_id} lacks worker-scoped lock evidence")
    if not bool(worker.get("settle_evidence_ok", False)):
        blocked.append(f"OpenCode worker {worker_id} lacks worker-scoped settle evidence")
    if not all(smoke[key] for key in ("done_smoke_ok", "blocked_smoke_ok", "fail_smoke_ok")):
        blocked.append(f"worker {worker_id} lacks done/blocked/fail terminal evidence")
    mixed_ready = bool(worker.get("mixed_ready", False) and worker.get("allow_mixed_worker", False))
    if not mixed_ready:
        warnings.append(f"OpenCode worker {worker_id} mixed_ready=false")
    return MixedWorkerCandidate(
        worker_id=worker_id,
        backend_kind="opencode_tui",
        source={"opencode_visible_registry": True, "registered_worker_registry": False, "fixture": request.fixture_only},
        workspace_root=str(worker.get("project", worker.get("workspace_root", request.root))),
        runtime_backend=str(worker.get("runtime_backend", request.runtime_backend)),
        live_capable=bool(worker.get("server_live", False) or worker.get("live_capable", False)),
        registered=False,
        last_smoke_ok=smoke["last_smoke_ok"],
        done_smoke_ok=smoke["done_smoke_ok"],
        blocked_smoke_ok=smoke["blocked_smoke_ok"],
        fail_smoke_ok=smoke["fail_smoke_ok"],
        business_ready=bool(worker.get("business_ready", False) and worker.get("allow_business_repo_writes", False)),
        mixed_ready=mixed_ready,
        allow_business_repo_writes=bool(worker.get("allow_business_repo_writes", False)),
        allow_mixed_worker=bool(worker.get("allow_mixed_worker", False)),
        allowed_write_roots=[str(item) for item in worker.get("allowed_write_roots", [])],
        forbidden_paths=[str(item) for item in worker.get("forbidden_paths", [])],
        required_gates=["strong_binding", "worker_scoped_lock", "worker_scoped_settle", "done_smoke", "blocked_smoke", "fail_smoke"],
        blocked_reasons=_dedupe(blocked),
        warnings=_dedupe(warnings),
        evidence=smoke["evidence"],
    )


def _smoke_evidence(
    worker: dict[str, Any],
    *,
    base_path: Path,
    request: MixedWorkerPlanRequest,
    backend_kind: str,
) -> dict[str, Any]:
    explicit = {
        "done_smoke_ok": bool(worker.get("done_smoke_ok", False)),
        "blocked_smoke_ok": bool(worker.get("blocked_smoke_ok", False)),
        "fail_smoke_ok": bool(worker.get("fail_smoke_ok", False)),
    }
    evidence = [{"source": "worker_smoke_fields", "detail": {"worker_id": worker.get("worker_id", ""), **explicit}}]
    last = _smoke_status(_resolve_path(str(worker.get("last_smoke_artifact", "")), base_path))
    if last["ok"] and last["terminal_marker"] == ".done":
        explicit["done_smoke_ok"] = True
    if last["ok"] and last["terminal_marker"] == ".blocked":
        explicit["blocked_smoke_ok"] = True
    if last["ok"] and last["terminal_marker"] == ".fail":
        explicit["fail_smoke_ok"] = True
    evidence.append({"source": "last_smoke_artifact", "detail": last})

    for root in _smoke_search_roots(request, backend_kind):
        scanned = _scan_smoke_root(root, str(worker.get("worker_id", "")))
        if scanned["done_smoke_ok"]:
            explicit["done_smoke_ok"] = True
        if scanned["blocked_smoke_ok"]:
            explicit["blocked_smoke_ok"] = True
        if scanned["fail_smoke_ok"]:
            explicit["fail_smoke_ok"] = True
        evidence.append({"source": "smoke_search_root", "detail": scanned})

    return {
        "last_smoke_ok": bool(last["ok"] or any(explicit.values())),
        **explicit,
        "evidence": evidence,
    }


def _scan_smoke_root(root: Path, worker_id: str) -> dict[str, Any]:
    result = {
        "root": str(root),
        "worker_id": worker_id,
        "done_smoke_ok": False,
        "blocked_smoke_ok": False,
        "fail_smoke_ok": False,
        "paths": [],
    }
    if not root.is_dir():
        return result
    for report in sorted(root.glob("**/report.md")):
        task_dir = report.parent
        if worker_id and worker_id not in str(task_dir):
            text = report.read_text(encoding="utf-8", errors="replace")
            if worker_id not in text:
                continue
        status = _smoke_status(task_dir)
        if status["ok"] and status["terminal_marker"] == ".done":
            result["done_smoke_ok"] = True
        if status["ok"] and status["terminal_marker"] == ".blocked":
            result["blocked_smoke_ok"] = True
        if status["ok"] and status["terminal_marker"] == ".fail":
            result["fail_smoke_ok"] = True
        if status["ok"]:
            result["paths"].append(status["path"])
    return result


def _smoke_search_roots(request: MixedWorkerPlanRequest, backend_kind: str) -> list[Path]:
    roots = [Path(item).resolve() for item in request.smoke_search_roots if item]
    root = Path(request.root).resolve() if request.root else Path(".").resolve()
    if backend_kind == "claude_code":
        roots.append(root / ".omx" / "framework" / "team" / "claude-live-smoke")
    return _dedupe_paths(roots)


def _business_blocks(request: MixedWorkerPlanRequest, workers: list[MixedWorkerCandidate]) -> list[str]:
    if not request.business_repo_writes_requested:
        return []
    blocked = ["business repo writes requested but business repo writes remain forbidden"]
    blocked.extend(f"worker {worker.worker_id} business_ready=false blocks business tasks" for worker in workers if not worker.business_ready)
    return _dedupe(blocked)


def _mixed_readiness_blocks(workers: list[MixedWorkerCandidate], include: set[str]) -> list[str]:
    if not {"opencode_tui", "claude_code"}.issubset(include):
        return []
    return _dedupe(f"worker {worker.worker_id} mixed_ready=false blocks mixed live" for worker in workers if not worker.mixed_ready)


def _write_scope_blocks(workers: list[MixedWorkerCandidate]) -> list[str]:
    blocked: list[str] = []
    for index, left in enumerate(workers):
        for right in workers[index + 1 :]:
            for left_root in left.allowed_write_roots:
                for right_root in right.allowed_write_roots:
                    if _paths_overlap(left_root, right_root):
                        blocked.append(f"write scope overlap between {left.worker_id} and {right.worker_id}: {left_root} overlaps {right_root}")
    return _dedupe(blocked)


def _terminal_protocol_blocks(workers: list[MixedWorkerCandidate]) -> list[str]:
    blocked: list[str] = []
    for worker in workers:
        if not (worker.done_smoke_ok and worker.blocked_smoke_ok and worker.fail_smoke_ok):
            blocked.append(f"worker {worker.worker_id} lacks done/blocked/fail terminal evidence")
    return _dedupe(blocked)


def _runtime_telemetry(root: Path, request: MixedWorkerPlanRequest) -> dict[str, Any]:
    if request.runtime_telemetry:
        return request.runtime_telemetry
    watcher = _read_json_or_empty(root / ".omx" / "framework" / "team" / "visible-runs" / "_watcher" / "current_alerts.json")
    locks_root = root / ".omx" / "framework" / "team" / "visible-runs" / "_locks"
    locks = []
    if locks_root.is_dir():
        for path in sorted(locks_root.glob("*.lock.json")):
            data = _read_json_or_empty(path)
            if data and data.get("status") != "released":
                locks.append(data)
    return {
        "active_alerts": int(watcher.get("active_alerts", 0) or 0),
        "alerts": watcher.get("alerts", []) if isinstance(watcher.get("alerts"), list) else [],
        "locks": locks,
    }


def _runtime_blocks(runtime: dict[str, Any]) -> list[str]:
    return _dedupe(_runtime_lock_blocks(runtime) + _runtime_alert_blocks(runtime))


def _runtime_lock_blocks(runtime: dict[str, Any]) -> list[str]:
    locks = runtime.get("locks", []) if isinstance(runtime.get("locks"), list) else []
    active_locks = int(runtime.get("active_locks", runtime.get("active_nonterminal_locks", len(locks))) or 0)
    if active_locks > 0:
        return [f"active locks > 0: {active_locks}"]
    return []


def _runtime_alert_blocks(runtime: dict[str, Any]) -> list[str]:
    alerts = int(runtime.get("active_alerts", 0) or 0)
    if alerts > 0:
        return [f"watcher alerts > 0: {alerts}"]
    return []


def _gate(name: str, ok: bool, blocked_reason: str) -> MixedWorkerGateResult:
    reasons = [blocked_reason] if blocked_reason and not ok else []
    return MixedWorkerGateResult(gate=name, ok=ok, blocked_reasons=reasons)


def _worker_roles(workers: list[MixedWorkerCandidate]) -> dict[str, str]:
    roles: dict[str, str] = {}
    for worker in workers:
        if worker.backend_kind == "opencode_tui":
            roles[worker.worker_id] = "visible task executor candidate"
        elif worker.backend_kind == "claude_code":
            roles[worker.worker_id] = "registered Claude reviewer/planner candidate"
        else:
            roles[worker.worker_id] = "disabled candidate"
    return roles


def _task_matrix(workers: list[MixedWorkerCandidate]) -> list[dict[str, Any]]:
    return [
        {
            "worker_id": worker.worker_id,
            "backend_kind": worker.backend_kind,
            "allowed_write_roots": worker.allowed_write_roots,
            "forbidden_paths": worker.forbidden_paths,
            "business_repo_writes_allowed": False,
            "mixed_live_allowed": False,
        }
        for worker in workers
    ]


def _worker_warnings(workers: list[MixedWorkerCandidate]) -> list[str]:
    return _dedupe(warning for worker in workers for warning in worker.warnings)


def _is_opencode_worker(worker: dict[str, Any]) -> bool:
    return str(worker.get("backend", worker.get("backend_kind", ""))) in {"opencode_tui", "opencode-tui"}


def _paths_overlap(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_path = Path(left).resolve()
    right_path = Path(right).resolve()
    return left_path == right_path or left_path in right_path.parents or right_path in left_path.parents


def _resolve_path(value: str, base_path: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = base_path / path
    return path.resolve()


def _smoke_status(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"path": "", "report_present": False, "terminal_marker": "", "terminal_marker_count": 0, "ok": False}
    markers = [marker for marker in TERMINAL_MARKERS if (path / marker).is_file()]
    report = path / "report.md"
    report_present = report.is_file() and bool(report.read_text(encoding="utf-8", errors="replace").strip())
    return {
        "path": str(path),
        "report_present": report_present,
        "terminal_marker": markers[0] if len(markers) == 1 else "",
        "terminal_marker_count": len(markers),
        "ok": report_present and len(markers) == 1,
    }


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return data


def _read_json_or_empty(path: Path) -> dict[str, Any]:
    try:
        return _read_json(path)
    except Exception:
        return {}


def _read_optional_json(path: Path) -> tuple[dict[str, Any], bool]:
    try:
        return _read_json(path), True
    except Exception:
        return {}, False


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dedupe(values: Any) -> list[Any]:
    seen = set()
    out = []
    for value in values:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _dedupe_evidence(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in _dedupe(values) if isinstance(item, dict)]


def _dedupe_paths(values: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out
