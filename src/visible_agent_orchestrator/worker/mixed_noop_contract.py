"""Source-only mixed no-op contract planning and result validation.

This module describes future OpenCode plus registered Claude no-op fan-out
contracts. It reads local JSON evidence and fixture task directories only; it
does not call tmux, OpenCode, Claude Code, or write runtime registries.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime.backends import default_backend_name
from visible_agent_orchestrator.runtime_paths import workspace_root
from .registered_registry import RegisteredWorkerRegistry, validate_registered_worker_registry_file
from .registered_worker import TERMINAL_MARKERS


SCHEMA_VERSION = 1
CONTRACT_KIND = "mixed_noop_contract"
DEFAULT_ALLOWED_TASK_KINDS = ["mixed_noop_sandbox", "mixed_terminal_protocol_smoke"]
BUSINESS_REPO_ROOT = str(workspace_root())


@dataclass(frozen=True)
class MixedNoopContractRequest:
    root: str
    registered_worker_registry_path: str
    opencode_visible_registry_path: str
    include_workers: list[str] = field(default_factory=list)
    execution_mode: str = "no-live"
    runtime_backend: str = "tmux-reference"
    runtime_telemetry: dict[str, Any] = field(default_factory=dict)
    business_repo_writes_requested: bool = False
    cursor_requested: bool = False
    fixture_only: bool = False
    fixture_path: str = ""
    contract_id: str = "mixed-noop-contract"
    mixed_run_root: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, fixture_path: str = "") -> "MixedNoopContractRequest":
        return cls(
            root=str(payload.get("root", "")),
            registered_worker_registry_path=str(
                payload.get("registered_worker_registry_path", payload.get("registered_worker_registry", "registered-workers.json"))
            ),
            opencode_visible_registry_path=str(
                payload.get("opencode_visible_registry_path", payload.get("opencode_visible_registry", "opencode_visible_workers.json"))
            ),
            include_workers=[str(item) for item in payload.get("include_workers", payload.get("include_worker", []))],
            execution_mode=str(payload.get("execution_mode", "no-live")),
            runtime_backend=str(payload.get("runtime_backend", default_backend_name())),
            runtime_telemetry=_dict(payload.get("runtime_telemetry", {})),
            business_repo_writes_requested=bool(payload.get("business_repo_writes_requested", False)),
            cursor_requested=bool(payload.get("cursor_requested", False)),
            fixture_only=bool(payload.get("fixture_only", bool(fixture_path))),
            fixture_path=fixture_path or str(payload.get("fixture_path", "")),
            contract_id=str(payload.get("contract_id", "mixed-noop-contract")),
            mixed_run_root=str(payload.get("mixed_run_root", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MixedNoopWorkerSpec:
    worker_id: str
    backend_kind: str
    registry_source: str
    runtime_backend: str
    role: str
    mixed_ready: bool
    business_ready: bool
    allow_mixed_worker: bool
    allow_business_repo_writes: bool
    mixed_ready_scope: str
    allowed_mixed_task_kinds: list[str]
    allowed_write_roots: list[str]
    forbidden_paths: list[str]
    required_terminal_markers: list[str]
    required_outputs: list[str]
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MixedNoopArtifactLayout:
    mixed_run_root: str
    per_worker_task_dirs: dict[str, str]
    shared_readonly_contract_dir: str
    aggregate_report_path: str
    aggregate_summary_path: str
    event_log_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MixedNoopContractPlan:
    ok: bool
    schema_version: int
    contract_id: str
    created_at: str
    contract_kind: str
    execution_mode: str
    root: str
    runtime_backend: str
    registered_worker_registry: str
    opencode_visible_registry: str
    workers: list[MixedNoopWorkerSpec]
    roles: dict[str, str]
    task_matrix: list[dict[str, Any]]
    fanout: dict[str, Any]
    fanin: dict[str, Any]
    artifact_layout: MixedNoopArtifactLayout
    allowed_write_roots: list[str]
    forbidden_paths: list[str]
    business_repo_policy: dict[str, Any]
    terminal_protocol: dict[str, Any]
    lock_policy: dict[str, Any]
    settle_policy: dict[str, Any]
    health_policy: dict[str, Any]
    watcher_policy: dict[str, Any]
    expected_results: dict[str, Any]
    would_execute_live: bool
    would_write_registry: bool
    write_scope_disjoint: bool
    business_writes_forbidden: bool
    worker_count: int
    opencode_worker_count: int
    claude_worker_count: int
    cursor_worker_count: int
    per_worker_dirs: dict[str, str]
    aggregate_paths: dict[str, str]
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["workers"] = [worker.to_dict() for worker in self.workers]
        payload["artifact_layout"] = self.artifact_layout.to_dict()
        return payload


@dataclass(frozen=True)
class MixedNoopContractValidation:
    ok: bool
    contract_id: str
    worker_count: int
    would_execute_live: bool
    would_write_registry: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MixedNoopResultValidation:
    ok: bool
    aggregate_state: str
    worker_results: list[dict[str, Any]]
    all_reports_nonempty: bool
    all_terminal_markers_valid: bool
    terminal_marker_conflicts: list[str]
    out_of_scope_writes: list[dict[str, Any]]
    business_repo_modified: bool
    registry_modified: bool
    state_team_modified: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_mixed_noop_contract_plan_from_files(
    *,
    root: Path | str,
    registered_worker_registry_path: Path | str,
    opencode_visible_registry_path: Path | str,
    include_workers: list[str] | None = None,
    execution_mode: str = "no-live",
    runtime_telemetry: dict[str, Any] | None = None,
    business_repo_writes_requested: bool = False,
    fixture_only: bool = False,
    fixture_path: Path | str | None = None,
) -> MixedNoopContractPlan:
    request = MixedNoopContractRequest(
        root=str(Path(root).resolve()),
        registered_worker_registry_path=str(Path(registered_worker_registry_path).resolve()),
        opencode_visible_registry_path=str(Path(opencode_visible_registry_path).resolve()),
        include_workers=list(include_workers or []),
        execution_mode=execution_mode,
        runtime_telemetry=_dict(runtime_telemetry or {}),
        business_repo_writes_requested=business_repo_writes_requested,
        fixture_only=fixture_only,
        fixture_path=str(Path(fixture_path).resolve()) if fixture_path else "",
    )
    return build_mixed_noop_contract_plan(request)


def build_mixed_noop_fixture_plan(fixture_dir: Path | str) -> MixedNoopContractPlan:
    fixture = Path(fixture_dir).resolve()
    payload = _read_json(fixture / "request.json")
    request = MixedNoopContractRequest.from_dict(payload, fixture_path=str(fixture))
    root = fixture
    registered = _fixture_path(request.registered_worker_registry_path, fixture)
    opencode = _fixture_path(request.opencode_visible_registry_path, fixture)
    telemetry_path = fixture / "runtime-telemetry.json"
    request = MixedNoopContractRequest(
        **{
            **request.to_dict(),
            "root": str(root),
            "registered_worker_registry_path": str(registered),
            "opencode_visible_registry_path": str(opencode),
            "runtime_telemetry": _read_json(telemetry_path) if telemetry_path.is_file() else request.runtime_telemetry,
            "fixture_only": True,
            "fixture_path": str(fixture),
        }
    )
    return build_mixed_noop_contract_plan(request)


def build_mixed_noop_contract_plan(request: MixedNoopContractRequest) -> MixedNoopContractPlan:
    root = Path(request.root).resolve() if request.root else Path(".").resolve()
    registered_path = Path(request.registered_worker_registry_path).resolve()
    opencode_path = Path(request.opencode_visible_registry_path).resolve()
    blocked: list[str] = []
    warnings = [
        "mixed no-op contract is source-only and no-live",
        "mixed no-op contract is not business readiness",
        "mixed no-op contract is not a registry write",
        "mixed live remains forbidden",
    ]
    evidence: list[dict[str, Any]] = []
    if request.execution_mode != "no-live":
        blocked.append("mixed no-op contract requires execution_mode=no-live")
    if request.cursor_requested:
        blocked.append("Cursor workers are disabled/out of scope for mixed no-op contract")

    registered_payload, registered_present = _read_optional_json(registered_path)
    opencode_payload, opencode_present = _read_optional_json(opencode_path)
    if not registered_present:
        blocked.append("registered worker registry missing")
    if not opencode_present:
        blocked.append("OpenCode visible registry missing")

    if registered_present and not request.fixture_only:
        validation = validate_registered_worker_registry_file(registered_path)
        if request.include_workers:
            warnings.extend(
                f"registry-wide validation warning outside selected contract: {reason}"
                for reason in validation.blocked_reasons
            )
        else:
            blocked.extend(validation.blocked_reasons)
        evidence.extend(validation.evidence)
    evidence.extend(
        [
            {"source": "registered_worker_registry", "detail": {"present": registered_present, "registry_path": str(registered_path)}},
            {"source": "opencode_visible_registry", "detail": {"present": opencode_present, "registry_path": str(opencode_path)}},
        ]
    )

    include = set(request.include_workers)
    workers = []
    if opencode_present:
        workers.extend(_opencode_workers(opencode_payload, request=request, include=include))
    if registered_present:
        workers.extend(_claude_workers(registered_payload, request=request, include=include))

    if include:
        present_ids = {worker.worker_id for worker in workers}
        for worker_id in sorted(include - present_ids):
            blocked.append(f"included worker missing: {worker_id}")
    if not any(worker.backend_kind == "opencode_tui" for worker in workers):
        blocked.append("OpenCode worker missing")
    if not any(worker.backend_kind == "claude_code" for worker in workers):
        blocked.append("Claude worker missing")

    blocked.extend(_worker_blocks(workers))
    blocked.extend(_business_blocks(request, workers))
    overlap_blocks = _write_scope_blocks(workers)
    blocked.extend(overlap_blocks)
    runtime = _runtime_telemetry(root, request)
    blocked.extend(_runtime_blocks(runtime))
    blocked = _dedupe(blocked)
    write_scope_disjoint = not overlap_blocks

    layout = _artifact_layout(root, request, workers)
    ok = not blocked
    allowed_write_roots = _dedupe(root for worker in workers for root in worker.allowed_write_roots)
    forbidden_paths = _dedupe([BUSINESS_REPO_ROOT, str(root / ".omx" / "state" / "team")] + [path for worker in workers for path in worker.forbidden_paths])
    task_matrix = [
        {
            "worker_id": worker.worker_id,
            "backend_kind": worker.backend_kind,
            "role": worker.role,
            "task_kind": "mixed_noop_sandbox",
            "allowed_write_roots": worker.allowed_write_roots,
            "forbidden_paths": worker.forbidden_paths,
            "business_repo_writes_allowed": False,
            "mixed_live_allowed": False,
        }
        for worker in workers
    ]
    aggregate_paths = {
        "aggregate_report_path": layout.aggregate_report_path,
        "aggregate_summary_path": layout.aggregate_summary_path,
        "event_log_path": layout.event_log_path,
    }

    return MixedNoopContractPlan(
        ok=ok,
        schema_version=SCHEMA_VERSION,
        contract_id=request.contract_id,
        created_at=_utc_now(),
        contract_kind=CONTRACT_KIND,
        execution_mode=request.execution_mode,
        root=str(root),
        runtime_backend=request.runtime_backend,
        registered_worker_registry=str(registered_path),
        opencode_visible_registry=str(opencode_path),
        workers=workers,
        roles={worker.worker_id: worker.role for worker in workers},
        task_matrix=task_matrix,
        fanout={
            "mode": "per_worker_task_dir",
            "workers_may_only_write_own_task_dir": True,
            "shared_contract_is_readonly": True,
        },
        fanin={
            "aggregate_writer": "future_orchestrator_collector",
            "workers_must_not_modify_peer_outputs": True,
            "aggregate_terminal_rule": {
                "all_done": ".done",
                "any_fail": ".fail",
                "any_blocked_no_fail": ".blocked",
                "missing_or_invalid": "invalid",
            },
        },
        artifact_layout=layout,
        allowed_write_roots=allowed_write_roots,
        forbidden_paths=forbidden_paths,
        business_repo_policy={
            "business_ready_allowed": False,
            "business_repo_writes_allowed": False,
            "business_repo_writes_requested": request.business_repo_writes_requested,
            "reason": "business_ready=false remains required for mixed no-op; business tasks stay forbidden",
        },
        terminal_protocol={
            "required_report": "report.md",
            "report_must_be_nonempty": True,
            "terminal_marker_count": "exactly_one",
            "allowed_terminal_markers": list(TERMINAL_MARKERS),
            "aggregate_terminal_rule": "all .done => .done; any .fail => .fail; any .blocked and no .fail => .blocked; missing/invalid => invalid",
        },
        lock_policy={"active_locks_allowed": 0, "worker_scoped_lock_required": True},
        settle_policy={"worker_scoped_settle_required": True},
        health_policy={"must_not_touch_workers": True, "read_only_health_only": True},
        watcher_policy={"active_alerts_allowed": 0, "watcher_telemetry_only": True},
        expected_results={
            "worker_report": "non-empty report.md",
            "worker_terminal_marker": "exactly one of .done/.blocked/.fail",
            "aggregate_states": ["completed", "blocked", "failed", "invalid"],
        },
        would_execute_live=False,
        would_write_registry=False,
        write_scope_disjoint=write_scope_disjoint,
        business_writes_forbidden=True,
        worker_count=len(workers),
        opencode_worker_count=sum(1 for worker in workers if worker.backend_kind == "opencode_tui"),
        claude_worker_count=sum(1 for worker in workers if worker.backend_kind == "claude_code"),
        cursor_worker_count=sum(1 for worker in workers if worker.backend_kind == "cursor_agent"),
        per_worker_dirs=layout.per_worker_task_dirs,
        aggregate_paths=aggregate_paths,
        blocked_reasons=blocked,
        warnings=_dedupe(warnings + [warning for worker in workers for warning in worker.warnings]),
        safe_next_actions=[
            "fixture-only mixed no-op result validation",
            "source-only OpenCode mixed-readiness gate design",
            "install only after separate install prompt",
        ],
        forbidden_next_actions=[
            "mixed live execution",
            "business repo writes",
            "registered worker registry writes",
            "OpenCode visible registry writes",
            "tmux input",
            "Claude Code invocation",
            "OpenCode live submit",
            "Cursor execution",
        ],
        evidence=_dedupe_evidence(evidence + [{"source": "runtime_telemetry", "detail": runtime}]),
    )


def validate_mixed_noop_contract_file(path: Path | str) -> MixedNoopContractValidation:
    payload = _read_json(Path(path).resolve())
    blocked: list[str] = []
    workers = payload.get("workers", [])
    if payload.get("contract_kind") != CONTRACT_KIND:
        blocked.append("contract_kind must be mixed_noop_contract")
    if payload.get("execution_mode") != "no-live":
        blocked.append("execution_mode must be no-live")
    if bool(payload.get("would_execute_live", True)):
        blocked.append("would_execute_live must be false")
    if bool(payload.get("would_write_registry", True)):
        blocked.append("would_write_registry must be false")
    if not isinstance(workers, list) or not workers:
        blocked.append("contract must contain workers")
    for worker in workers if isinstance(workers, list) else []:
        if worker.get("business_ready") is True:
            blocked.append(f"worker {worker.get('worker_id', '<missing>')} business_ready=true is not allowed")
        if worker.get("allow_business_repo_writes") is True:
            blocked.append(f"worker {worker.get('worker_id', '<missing>')} allow_business_repo_writes=true is not allowed")
    return MixedNoopContractValidation(
        ok=not blocked,
        contract_id=str(payload.get("contract_id", "")),
        worker_count=len(workers) if isinstance(workers, list) else 0,
        would_execute_live=bool(payload.get("would_execute_live", False)),
        would_write_registry=bool(payload.get("would_write_registry", False)),
        blocked_reasons=_dedupe(blocked),
        warnings=[],
        evidence=[{"source": "contract_file", "detail": {"path": str(Path(path).resolve())}}],
    )


def validate_mixed_noop_result_files(contract_path: Path | str, result_path: Path | str) -> MixedNoopResultValidation:
    contract_file = Path(contract_path).resolve()
    result_file = Path(result_path).resolve()
    contract = _read_json(contract_file)
    result = _read_json(result_file)
    base = result_file.parent
    workers = {str(worker.get("worker_id", "")): worker for worker in contract.get("workers", []) if isinstance(worker, dict)}
    blocked: list[str] = []
    worker_results: list[dict[str, Any]] = []
    marker_states: list[str] = []
    conflicts: list[str] = []
    out_of_scope: list[dict[str, Any]] = []
    business_repo_modified = False
    registry_modified = False
    state_team_modified = False

    for item in result.get("worker_results", []):
        worker_id = str(item.get("worker_id", ""))
        worker = workers.get(worker_id, {})
        task_dir = _resolve_path(str(item.get("task_dir", "")), base)
        report = task_dir / "report.md" if task_dir else None
        markers = [marker for marker in TERMINAL_MARKERS if task_dir and (task_dir / marker).is_file()]
        report_nonempty = bool(report and report.is_file() and report.read_text(encoding="utf-8", errors="replace").strip())
        if not report_nonempty:
            blocked.append(f"worker {worker_id} missing non-empty report.md")
        if len(markers) != 1:
            blocked.append(f"worker {worker_id} terminal marker count must be exactly one")
            conflicts.append(worker_id)
        marker = markers[0] if len(markers) == 1 else ""
        marker_states.append(marker)
        allowed_roots = [str(path) for path in worker.get("allowed_write_roots", []) if path]
        for write in item.get("observed_writes", []):
            path = str(write)
            if path.startswith(BUSINESS_REPO_ROOT):
                business_repo_modified = True
            if "/.omx/framework/team/registered-workers/" in path or path.endswith("/registered-workers.json"):
                registry_modified = True
            if "/.omx/state/team" in path:
                state_team_modified = True
            if not _path_under_any(path, allowed_roots):
                out_of_scope.append({"worker_id": worker_id, "path": path, "allowed_write_roots": allowed_roots})
        worker_results.append(
            {
                "worker_id": worker_id,
                "task_dir": str(task_dir),
                "report_nonempty": report_nonempty,
                "terminal_marker": marker,
                "terminal_marker_count": len(markers),
                "observed_writes": list(item.get("observed_writes", [])),
            }
        )

    if business_repo_modified:
        blocked.append("business repo modified")
    if registry_modified:
        blocked.append("registry modified")
    if state_team_modified:
        blocked.append(".omx/state/team modified")
    if out_of_scope:
        blocked.append("out-of-scope write detected")
    if not worker_results:
        blocked.append("result must contain worker_results")

    aggregate_state = _aggregate_state(marker_states, invalid=bool(blocked or conflicts))
    return MixedNoopResultValidation(
        ok=not blocked,
        aggregate_state=aggregate_state,
        worker_results=worker_results,
        all_reports_nonempty=all(item["report_nonempty"] for item in worker_results),
        all_terminal_markers_valid=bool(worker_results) and not conflicts,
        terminal_marker_conflicts=conflicts,
        out_of_scope_writes=out_of_scope,
        business_repo_modified=business_repo_modified,
        registry_modified=registry_modified,
        state_team_modified=state_team_modified,
        blocked_reasons=_dedupe(blocked),
        warnings=["mixed no-op result validation is fixture/source-only"],
        evidence=[
            {"source": "contract_file", "detail": {"path": str(contract_file)}},
            {"source": "result_file", "detail": {"path": str(result_file)}},
        ],
    )


def _opencode_workers(payload: dict[str, Any], *, request: MixedNoopContractRequest, include: set[str]) -> list[MixedNoopWorkerSpec]:
    specs: list[MixedNoopWorkerSpec] = []
    for worker in payload.get("workers", []):
        if not isinstance(worker, dict) or str(worker.get("backend", worker.get("backend_kind", ""))) not in {"opencode_tui", "opencode-tui"}:
            continue
        worker_id = str(worker.get("worker_id", ""))
        if include and worker_id not in include:
            continue
        mixed_ready = bool(worker.get("mixed_ready", False) and worker.get("allow_mixed_worker", False))
        allow_business = bool(worker.get("allow_business_repo_writes", False))
        business_ready = bool(worker.get("business_ready", False))
        blocked: list[str] = []
        warnings: list[str] = []
        if not mixed_ready:
            blocked.append(f"OpenCode worker {worker_id} mixed_ready=false")
        if business_ready:
            blocked.append(f"OpenCode worker {worker_id} business_ready=true is not allowed for no-op contract")
        if allow_business:
            blocked.append(f"OpenCode worker {worker_id} allow_business_repo_writes=true is forbidden")
        if not bool(worker.get("worker_scoped_lock_evidence_ok", False)):
            blocked.append(f"OpenCode worker {worker_id} lacks worker-scoped lock evidence")
        if not bool(worker.get("settle_evidence_ok", False)):
            blocked.append(f"OpenCode worker {worker_id} lacks worker-scoped settle evidence")
        scopes = [str(item) for item in worker.get("allowed_write_roots", [])]
        if not scopes:
            scopes = [str(Path(request.root).resolve() / ".omx" / "framework" / "team" / "mixed-noop" / "runs" / request.contract_id / worker_id)]
        specs.append(
            MixedNoopWorkerSpec(
                worker_id=worker_id,
                backend_kind="opencode_tui",
                registry_source="opencode_visible_registry",
                runtime_backend=str(worker.get("runtime_backend", request.runtime_backend)),
                role=str(worker.get("mixed_noop_role", "noop_executor")),
                mixed_ready=mixed_ready,
                business_ready=business_ready,
                allow_mixed_worker=bool(worker.get("allow_mixed_worker", False)),
                allow_business_repo_writes=allow_business,
                mixed_ready_scope=str(worker.get("mixed_ready_scope", "")),
                allowed_mixed_task_kinds=[str(item) for item in worker.get("allowed_mixed_task_kinds", [])],
                allowed_write_roots=scopes,
                forbidden_paths=[str(item) for item in worker.get("forbidden_paths", [BUSINESS_REPO_ROOT])],
                required_terminal_markers=list(TERMINAL_MARKERS),
                required_outputs=["report.md"],
                blocked_reasons=_dedupe(blocked),
                warnings=_dedupe(warnings),
                evidence=[{"source": "opencode_visible_registry", "detail": {"worker_id": worker_id}}],
            )
        )
    return specs


def _claude_workers(payload: dict[str, Any], *, request: MixedNoopContractRequest, include: set[str]) -> list[MixedNoopWorkerSpec]:
    specs: list[MixedNoopWorkerSpec] = []
    registry = RegisteredWorkerRegistry.from_dict(payload)
    for worker in registry.workers:
        if str(worker.get("backend_kind", "")) != "claude_code":
            continue
        worker_id = str(worker.get("worker_id", ""))
        if include and worker_id not in include:
            continue
        mixed_ready = bool(worker.get("mixed_ready", False) and worker.get("allow_mixed_worker", False))
        business_ready = bool(worker.get("business_ready", False))
        allow_business = bool(worker.get("allow_business_repo_writes", False))
        blocked: list[str] = []
        if not mixed_ready:
            blocked.append(f"Claude worker {worker_id} mixed_ready=false")
        if business_ready:
            blocked.append(f"Claude worker {worker_id} business_ready=true is not allowed for no-op contract")
        if allow_business:
            blocked.append(f"Claude worker {worker_id} allow_business_repo_writes=true is forbidden")
        scopes = [str(item) for item in worker.get("allowed_write_roots", [])]
        if not scopes:
            scopes = [str(Path(request.root).resolve() / ".omx" / "framework" / "team" / "mixed-noop" / "runs" / request.contract_id / worker_id)]
        specs.append(
            MixedNoopWorkerSpec(
                worker_id=worker_id,
                backend_kind="claude_code",
                registry_source="registered_worker_registry",
                runtime_backend=str(worker.get("runtime_backend", request.runtime_backend)),
                role=str(worker.get("mixed_noop_role", "noop_reviewer")),
                mixed_ready=mixed_ready,
                business_ready=business_ready,
                allow_mixed_worker=bool(worker.get("allow_mixed_worker", False)),
                allow_business_repo_writes=allow_business,
                mixed_ready_scope=str(worker.get("mixed_ready_scope", "")),
                allowed_mixed_task_kinds=[str(item) for item in worker.get("allowed_mixed_task_kinds", [])],
                allowed_write_roots=scopes,
                forbidden_paths=[str(item) for item in worker.get("forbidden_paths", [BUSINESS_REPO_ROOT])],
                required_terminal_markers=list(TERMINAL_MARKERS),
                required_outputs=["report.md"],
                blocked_reasons=_dedupe(blocked),
                warnings=[],
                evidence=[{"source": "registered_worker_registry", "detail": {"worker_id": worker_id}}],
            )
        )
    return specs


def _worker_blocks(workers: list[MixedNoopWorkerSpec]) -> list[str]:
    blocked: list[str] = []
    for worker in workers:
        blocked.extend(worker.blocked_reasons)
        if worker.mixed_ready_scope and worker.mixed_ready_scope != "sandbox_noop_only":
            blocked.append(f"worker {worker.worker_id} mixed_ready_scope is not sandbox_noop_only")
        kinds = set(worker.allowed_mixed_task_kinds)
        if not kinds.issubset(set(DEFAULT_ALLOWED_TASK_KINDS)):
            blocked.append(f"worker {worker.worker_id} allowed_mixed_task_kinds exceed no-op contract scope")
        if worker.mixed_ready and not kinds:
            blocked.append(f"worker {worker.worker_id} missing allowed_mixed_task_kinds")
    return _dedupe(blocked)


def _business_blocks(request: MixedNoopContractRequest, workers: list[MixedNoopWorkerSpec]) -> list[str]:
    blocked: list[str] = []
    if request.business_repo_writes_requested:
        blocked.append("business repo writes requested but forbidden for mixed no-op contract")
    for worker in workers:
        if worker.business_ready:
            blocked.append(f"worker {worker.worker_id} business_ready=true is not allowed")
        if worker.allow_business_repo_writes:
            blocked.append(f"worker {worker.worker_id} allow_business_repo_writes=true is forbidden")
    return _dedupe(blocked)


def _write_scope_blocks(workers: list[MixedNoopWorkerSpec]) -> list[str]:
    blocked: list[str] = []
    for index, left in enumerate(workers):
        for right in workers[index + 1 :]:
            for left_root in left.allowed_write_roots:
                for right_root in right.allowed_write_roots:
                    if _paths_overlap(left_root, right_root):
                        blocked.append(f"write scope overlap between {left.worker_id} and {right.worker_id}: {left_root} overlaps {right_root}")
    return _dedupe(blocked)


def _artifact_layout(root: Path, request: MixedNoopContractRequest, workers: list[MixedNoopWorkerSpec]) -> MixedNoopArtifactLayout:
    mixed_root = Path(request.mixed_run_root).resolve() if request.mixed_run_root else root / ".omx" / "framework" / "team" / "mixed-noop" / "runs" / request.contract_id
    per_worker = {worker.worker_id: str(mixed_root / "tasks" / worker.worker_id) for worker in workers}
    return MixedNoopArtifactLayout(
        mixed_run_root=str(mixed_root),
        per_worker_task_dirs=per_worker,
        shared_readonly_contract_dir=str(mixed_root / "contract"),
        aggregate_report_path=str(mixed_root / "aggregate" / "report.md"),
        aggregate_summary_path=str(mixed_root / "aggregate" / "summary.json"),
        event_log_path=str(mixed_root / "events.jsonl"),
    )


def _runtime_telemetry(root: Path, request: MixedNoopContractRequest) -> dict[str, Any]:
    if request.runtime_telemetry:
        include = set() if request.fixture_only else set(request.include_workers)
        return _scope_runtime_telemetry(request.runtime_telemetry, include)
    watcher = _read_json_or_empty(root / ".omx" / "framework" / "team" / "visible-runs" / "_watcher" / "current_alerts.json")
    locks_root = root / ".omx" / "framework" / "team" / "visible-runs" / "_locks"
    locks = []
    if locks_root.is_dir():
        for path in sorted(locks_root.glob("*.lock.json")):
            data = _read_json_or_empty(path)
            if data and data.get("status") != "released":
                locks.append(data)
    include = set() if request.fixture_only else set(request.include_workers)
    return _scope_runtime_telemetry(
        {"active_alerts": int(watcher.get("active_alerts", 0) or 0), "alerts": watcher.get("alerts", []), "locks": locks},
        include,
    )


def _scope_runtime_telemetry(runtime: dict[str, Any], include: set[str]) -> dict[str, Any]:
    if not include:
        return dict(runtime)
    locks = runtime.get("locks", []) if isinstance(runtime.get("locks"), list) else []
    alerts = runtime.get("alerts", []) if isinstance(runtime.get("alerts"), list) else []
    scoped_locks = [lock for lock in locks if _runtime_item_worker_id(lock) in include]
    scoped_alerts = [alert for alert in alerts if _runtime_item_worker_id(alert) in include]
    payload = dict(runtime)
    payload["locks"] = scoped_locks
    payload["active_locks"] = len(scoped_locks)
    payload["active_alerts"] = len(scoped_alerts)
    payload["alerts"] = scoped_alerts
    payload["unrelated_lock_count"] = max(0, len(locks) - len(scoped_locks))
    payload["unrelated_alert_count"] = max(0, len(alerts) - len(scoped_alerts))
    payload["lock_scope"] = "selected_workers"
    payload["selected_workers"] = sorted(include)
    return payload


def _runtime_item_worker_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("worker_id", "worker", "owner"):
        value = str(item.get(key, ""))
        if value:
            return value
    path = str(item.get("lock_path") or item.get("path") or "")
    name = Path(path).name if path else ""
    if name.endswith(".lock.json"):
        return name.removesuffix(".lock.json")
    return ""


def _runtime_blocks(runtime: dict[str, Any]) -> list[str]:
    locks = runtime.get("locks", []) if isinstance(runtime.get("locks"), list) else []
    active_locks = int(runtime.get("active_locks", runtime.get("active_nonterminal_locks", len(locks))) or 0)
    active_alerts = int(runtime.get("active_alerts", 0) or 0)
    blocked: list[str] = []
    if active_locks > 0:
        blocked.append(f"active locks > 0: {active_locks}")
    if active_alerts > 0:
        blocked.append(f"watcher alerts > 0: {active_alerts}")
    return blocked


def _aggregate_state(markers: list[str], *, invalid: bool) -> str:
    if invalid or not markers or any(marker not in TERMINAL_MARKERS for marker in markers):
        return "invalid"
    if ".fail" in markers:
        return "failed"
    if ".blocked" in markers:
        return "blocked"
    if all(marker == ".done" for marker in markers):
        return "completed"
    return "invalid"


def _path_under_any(path: str, roots: list[str]) -> bool:
    if not path or not roots:
        return False
    candidate = Path(path).resolve()
    for root in roots:
        root_path = Path(root).resolve()
        if candidate == root_path or root_path in candidate.parents:
            return True
    return False


def _paths_overlap(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_path = Path(left).resolve()
    right_path = Path(right).resolve()
    return left_path == right_path or left_path in right_path.parents or right_path in left_path.parents


def _resolve_path(value: str, base_path: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base_path / path
    return path.resolve()


def _fixture_path(value: str, fixture: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = fixture / path
    return path.resolve()


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
