"""No-live OpenCode mixed-readiness status aggregation.

This module surfaces the current OpenCode mixed-readiness evidence state for
operator status commands. It only reads local JSON/files and composes existing
readiness/evidence-planning checks; it never submits work, writes registries,
calls tmux, calls OpenCode, or promotes mixed/business readiness.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .opencode_mixed_readiness import (
    build_opencode_mixed_readiness_check_from_files,
    build_opencode_mixed_readiness_fixture_check,
)
from .opencode_mixed_readiness_evidence_execution import (
    EVIDENCE_ROOT_REL,
    SMOKE_KINDS,
    build_opencode_mixed_readiness_evidence_execution_plan_from_files,
)


STATUS_SCHEMA_VERSION = 1
BACKEND_KIND = "opencode_tui"


@dataclass(frozen=True)
class OpenCodeMixedWorkerStatus:
    worker_id: str
    backend_kind: str
    registry_present: bool
    registry_valid: bool
    worker_present: bool
    evidence_plan_available: bool
    evidence_execution_plan_available: bool
    evidence_collected: bool
    done_smoke_ok: bool
    blocked_smoke_ok: bool
    fail_smoke_ok: bool
    worker_scoped_lock_ok: bool
    worker_scoped_settle_ok: bool
    mixed_ready_candidate: bool
    mixed_ready: bool
    allow_mixed_worker: bool
    business_ready: bool
    allow_business_repo_writes: bool
    would_execute_live: bool
    would_write_registry: bool
    would_touch_worker: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)
    readiness: dict[str, Any] = field(default_factory=dict)
    evidence_execution_plan: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeMixedStatusSummary:
    schema_version: int
    status_kind: str
    generated_at: str
    root: str
    worker_scope: str
    ok: bool
    worker_count: int
    mixed_ready_workers: list[str]
    mixed_blocked_workers: list[str]
    evidence_missing_workers: list[str]
    evidence_collected_candidates: list[str]
    would_execute_live: bool
    would_write_registry: bool
    workers: list[OpenCodeMixedWorkerStatus]
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        first = self.workers[0] if self.workers else None
        payload = {
            **asdict(self),
            "workers": [worker.to_dict() for worker in self.workers],
            "diagnostic_scope": "opencode_mixed_readiness",
            "diagnostic_scope_note": (
                "mixed OpenCode plus Claude execution readiness only; "
                "not ordinary OpenCode visible dispatch admission"
            ),
        }
        payload.update(_top_level_worker_aliases(first))
        if not first:
            payload["current_blockers"] = list(self.blockers)
        return payload


def source_opencode_mixed_status_summary(
    root: Path | str,
    *,
    worker_id: str | None = None,
    include_all: bool = False,
    execution_mode: str = "no-live",
    opencode_visible_registry: Path | str | None = None,
    registered_worker_registry: Path | str | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    opencode_registry = (
        Path(opencode_visible_registry).resolve()
        if opencode_visible_registry
        else root_path / ".omx" / "framework" / "team" / "opencode_visible_workers.json"
    )
    registered_registry = (
        Path(registered_worker_registry).resolve()
        if registered_worker_registry
        else root_path / ".omx" / "framework" / "team" / "registered-workers" / "registered-workers.json"
    )
    visible_payload, visible_present = _read_optional_json(opencode_registry)
    workers = _opencode_workers(visible_payload)
    selected = _select_workers(workers, worker_id=worker_id, include_all=include_all)

    worker_statuses: list[OpenCodeMixedWorkerStatus] = []
    if not visible_present:
        summary = _summary(
            root_path,
            worker_scope=_scope(worker_id, include_all),
            workers=[],
            blockers=[f"OpenCode visible registry missing: {opencode_registry}"],
            warnings=[],
        )
        return summary.to_dict()
    if not selected and worker_id:
        worker_statuses.append(
            _missing_worker_status(
                root_path=root_path,
                worker_id=worker_id,
                opencode_registry=opencode_registry,
                registry_present=visible_present,
            )
        )
    elif not selected:
        summary = _summary(
            root_path,
            worker_scope=_scope(worker_id, include_all),
            workers=[],
            blockers=["OpenCode visible registry has no selectable OpenCode workers"],
            warnings=[],
        )
        return summary.to_dict()
    else:
        for worker in selected:
            worker_statuses.append(
                build_opencode_mixed_worker_status_from_files(
                    root=root_path,
                    worker_id=str(worker.get("worker_id") or ""),
                    opencode_visible_registry=opencode_registry,
                    registered_worker_registry=registered_registry,
                    execution_mode=execution_mode,
                )
            )
    return _summary(root_path, worker_scope=_scope(worker_id, include_all), workers=worker_statuses).to_dict()


def build_opencode_mixed_worker_status_from_files(
    *,
    root: Path | str,
    worker_id: str,
    opencode_visible_registry: Path | str,
    registered_worker_registry: Path | str,
    execution_mode: str = "no-live",
) -> OpenCodeMixedWorkerStatus:
    root_path = Path(root).resolve()
    evidence_root = root_path / EVIDENCE_ROOT_REL / worker_id
    worker_record = _find_worker_record(Path(opencode_visible_registry).resolve(), worker_id)
    readiness = build_opencode_mixed_readiness_check_from_files(
        root=root_path,
        worker_id=worker_id,
        opencode_visible_registry_path=opencode_visible_registry,
        registered_worker_registry_path=registered_worker_registry,
        done_smoke_path=evidence_root / "done" / "task-001",
        blocked_smoke_path=evidence_root / "blocked" / "task-001",
        fail_smoke_path=evidence_root / "fail" / "task-001",
        lock_evidence_path=evidence_root / "lock-evidence.json",
        settle_evidence_path=evidence_root / "settle-evidence.json",
        execution_mode=execution_mode,
    ).to_dict()
    plan = build_opencode_mixed_readiness_evidence_execution_plan_from_files(
        root=root_path,
        worker_id=worker_id,
        opencode_visible_registry=opencode_visible_registry,
        registered_worker_registry=registered_worker_registry,
        execution_mode=execution_mode,
    )
    return _worker_status_from_readiness_and_plan(worker_id, readiness, plan, worker_record)


def build_opencode_mixed_status_fixture(fixture: Path | str) -> dict[str, Any]:
    fixture_path = _resolve_fixture(fixture)
    if fixture_path.is_dir():
        request = _read_json(fixture_path / "request.json")
        builtin_fixture = str(request.get("builtin_fixture") or "")
        if builtin_fixture:
            return _build_builtin_fixture_status(builtin_fixture)
        source_fixture = str(request.get("source_opencode_mixed_readiness_fixture") or "")
        if source_fixture:
            source_path = _resolve_source_readiness_fixture(fixture_path, source_fixture)
            readiness = build_opencode_mixed_readiness_fixture_check(source_path).to_dict()
            plan = build_opencode_mixed_readiness_evidence_execution_plan_from_files(
                root=source_path,
                worker_id=str(readiness.get("worker_id") or request.get("worker_id") or ""),
                opencode_visible_registry=source_path / "opencode_visible_workers.json",
                registered_worker_registry=source_path / "registered-workers.json",
                execution_mode="no-live",
            )
            worker = _worker_status_from_readiness_and_plan(str(readiness.get("worker_id") or ""), readiness, plan)
            return _summary(source_path, worker_scope="fixture", workers=[worker]).to_dict()
        return source_opencode_mixed_status_summary(
            fixture_path,
            worker_id=str(request.get("worker_id") or "") or None,
            include_all=bool(request.get("include_all", False)),
            execution_mode=str(request.get("execution_mode", "no-live")),
        )
    return _build_builtin_fixture_status(str(fixture))


def format_opencode_mixed_status_human(summary: dict[str, Any], *, indent: str = "") -> list[str]:
    lines = [
        f"{indent}opencode_mixed_status: ok={str(bool(summary.get('ok'))).lower()} "
        f"scope={summary.get('worker_scope') or '-'} "
        f"diagnostic_scope={summary.get('diagnostic_scope') or '-'} "
        f"workers={summary.get('worker_count', 0)}",
        f"{indent}  mixed_ready_workers: {', '.join(summary.get('mixed_ready_workers', [])) or '-'}",
        f"{indent}  mixed_blocked_workers: {', '.join(summary.get('mixed_blocked_workers', [])) or '-'}",
        f"{indent}  evidence_missing_workers: {', '.join(summary.get('evidence_missing_workers', [])) or '-'}",
        f"{indent}  evidence_collected_candidates: {', '.join(summary.get('evidence_collected_candidates', [])) or '-'}",
    ]
    for worker in summary.get("workers", []):
        blockers = worker.get("blockers", [])
        blocker_text = blockers[0] if blockers else "-"
        lines.append(
            f"{indent}  - {worker.get('worker_id') or '-'} "
            f"mixed_ready={str(bool(worker.get('mixed_ready'))).lower()} "
            f"candidate={str(bool(worker.get('mixed_ready_candidate'))).lower()} "
            f"evidence_collected={str(bool(worker.get('evidence_collected'))).lower()} "
            f"blocker={blocker_text}"
        )
    return lines


def _worker_status_from_readiness_and_plan(
    worker_id: str,
    readiness: dict[str, Any],
    plan: dict[str, Any],
    worker_record: dict[str, Any] | None = None,
) -> OpenCodeMixedWorkerStatus:
    worker_record = worker_record or {}
    result = readiness.get("result", {}) if isinstance(readiness.get("result"), dict) else {}
    current = readiness.get("current_readiness", {}) if isinstance(readiness.get("current_readiness"), dict) else {}
    plan_result = plan.get("result", {}) if isinstance(plan.get("result"), dict) else {}
    done_smoke_ok = bool(result.get("done_smoke_ok", False) or worker_record.get("done_smoke_evidence_ok", False))
    blocked_smoke_ok = bool(result.get("blocked_smoke_ok", False) or worker_record.get("blocked_smoke_evidence_ok", False))
    fail_smoke_ok = bool(result.get("fail_smoke_ok", False) or worker_record.get("fail_smoke_evidence_ok", False))
    worker_scoped_lock_ok = bool(
        result.get("worker_scoped_lock_ok", False) or worker_record.get("worker_scoped_lock_evidence_ok", False)
    )
    worker_scoped_settle_ok = bool(
        result.get("worker_scoped_settle_ok", False) or worker_record.get("settle_evidence_ok", False)
    )
    filesystem_evidence_collected = bool(
        done_smoke_ok
        and blocked_smoke_ok
        and fail_smoke_ok
        and worker_scoped_lock_ok
        and worker_scoped_settle_ok
    )
    registry_evidence_collected = _registry_evidence_collected(worker_record)
    evidence_collected = bool(filesystem_evidence_collected or registry_evidence_collected)
    blockers = _dedupe(list(readiness.get("blocked_reasons", [])) + list(plan.get("blocked_reasons", [])))
    if result.get("opencode_worker_present") and not bool(current.get("mixed_ready", False)):
        blockers.append(f"OpenCode worker {worker_id} registry mixed_ready=false")
    warnings = _dedupe(
        list(readiness.get("warnings", []))
        + list(plan.get("warnings", []))
        + _status_warnings(worker_id, evidence_collected, bool(current.get("mixed_ready", False)))
    )
    mixed_ready_candidate = bool(
        readiness.get("mixed_ready_candidate", False) or _registry_mixed_ready_candidate(worker_record)
    )
    if bool(current.get("mixed_ready", False)) and mixed_ready_candidate:
        warnings = [item for item in warnings if item != "mixed_ready_candidate is not OpenCode registry mixed_ready"]
    return OpenCodeMixedWorkerStatus(
        worker_id=worker_id,
        backend_kind=str(readiness.get("backend_kind") or result.get("backend_kind") or BACKEND_KIND),
        registry_present=bool(_registry_present(readiness)),
        registry_valid=bool(result.get("opencode_registry_valid", False)),
        worker_present=bool(result.get("opencode_worker_present", False)),
        evidence_plan_available=True,
        evidence_execution_plan_available=bool(plan_result.get("ok", False) and plan_result.get("smoke_task_count") == len(SMOKE_KINDS)),
        evidence_collected=evidence_collected,
        done_smoke_ok=done_smoke_ok,
        blocked_smoke_ok=blocked_smoke_ok,
        fail_smoke_ok=fail_smoke_ok,
        worker_scoped_lock_ok=worker_scoped_lock_ok,
        worker_scoped_settle_ok=worker_scoped_settle_ok,
        mixed_ready_candidate=mixed_ready_candidate,
        mixed_ready=bool(current.get("mixed_ready", False)),
        allow_mixed_worker=bool(current.get("allow_mixed_worker", False)),
        business_ready=bool(current.get("business_ready", False)),
        allow_business_repo_writes=bool(current.get("allow_business_repo_writes", False)),
        would_execute_live=bool(readiness.get("would_execute_live", False) or plan_result.get("would_execute_live", False)),
        would_write_registry=bool(readiness.get("would_update_registry", False) or plan_result.get("would_write_registry", False)),
        would_touch_worker=bool(plan_result.get("would_touch_worker", False)),
        blockers=_dedupe(blockers),
        warnings=warnings,
        safe_next_actions=_dedupe(
            list(readiness.get("safe_next_actions", []))
            + list(plan_result.get("safe_next_actions", []))
            + ["use separate explicit registry update path only after evidence is collected"]
        ),
        forbidden_next_actions=_dedupe(
            list(readiness.get("forbidden_next_actions", []))
            + list(plan_result.get("forbidden_next_actions", []))
        ),
        readiness=readiness,
        evidence_execution_plan=plan,
    )


def _find_worker_record(registry_path: Path, worker_id: str) -> dict[str, Any]:
    payload, _present = _read_optional_json(registry_path)
    workers = payload.get("workers", []) if isinstance(payload.get("workers"), list) else []
    for item in workers:
        if isinstance(item, dict) and str(item.get("worker_id") or "") == worker_id:
            return item
    return {}


def _registry_evidence_collected(worker: dict[str, Any]) -> bool:
    if not isinstance(worker, dict) or not worker:
        return False
    evidence_path = str(worker.get("mixed_ready_evidence_path") or "").strip()
    if not evidence_path:
        return False
    try:
        if not Path(evidence_path).is_file():
            return False
    except OSError:
        return False
    required_flags = (
        "done_smoke_evidence_ok",
        "blocked_smoke_evidence_ok",
        "fail_smoke_evidence_ok",
        "worker_scoped_lock_evidence_ok",
        "settle_evidence_ok",
    )
    return all(bool(worker.get(flag, False)) for flag in required_flags)


def _registry_mixed_ready_candidate(worker: dict[str, Any]) -> bool:
    if not isinstance(worker, dict) or not worker:
        return False
    check = worker.get("last_mixed_readiness_check", {})
    if not isinstance(check, dict):
        return False
    return bool(check.get("mixed_ready_candidate", False))


def _summary(
    root: Path,
    *,
    worker_scope: str,
    workers: list[OpenCodeMixedWorkerStatus],
    blockers: list[str] | None = None,
    warnings: list[str] | None = None,
) -> OpenCodeMixedStatusSummary:
    summary_blockers = _dedupe(list(blockers or []) + [reason for worker in workers for reason in worker.blockers])
    summary_warnings = _dedupe(list(warnings or []) + [reason for worker in workers for reason in worker.warnings])
    mixed_ready_workers = [worker.worker_id for worker in workers if worker.mixed_ready and not worker.blockers]
    mixed_blocked_workers = [worker.worker_id for worker in workers if worker.blockers or not worker.mixed_ready]
    evidence_missing_workers = [worker.worker_id for worker in workers if not worker.evidence_collected]
    evidence_collected_candidates = [worker.worker_id for worker in workers if worker.evidence_collected and worker.mixed_ready_candidate]
    return OpenCodeMixedStatusSummary(
        schema_version=STATUS_SCHEMA_VERSION,
        status_kind="opencode-mixed-status",
        generated_at=_utc_now(),
        root=str(root),
        worker_scope=worker_scope,
        ok=bool(workers) and not any((not worker.registry_present or not worker.worker_present) for worker in workers),
        worker_count=len(workers),
        mixed_ready_workers=mixed_ready_workers,
        mixed_blocked_workers=mixed_blocked_workers,
        evidence_missing_workers=evidence_missing_workers,
        evidence_collected_candidates=evidence_collected_candidates,
        would_execute_live=any(worker.would_execute_live for worker in workers),
        would_write_registry=any(worker.would_write_registry for worker in workers),
        workers=workers,
        blockers=summary_blockers,
        warnings=summary_warnings,
        safe_next_actions=_dedupe([action for worker in workers for action in worker.safe_next_actions]),
        forbidden_next_actions=_dedupe([action for worker in workers for action in worker.forbidden_next_actions]),
    )


def _top_level_worker_aliases(worker: OpenCodeMixedWorkerStatus | None) -> dict[str, Any]:
    if not worker:
        return {
            "worker_id": "",
            "backend_kind": "",
            "registry_present": False,
            "worker_present": False,
            "mixed_ready": False,
            "business_ready": False,
            "allow_mixed_worker": False,
            "allow_business_repo_writes": False,
            "evidence_plan_available": False,
            "evidence_execution_plan_available": False,
            "evidence_collected": False,
            "done_smoke_evidence_present": False,
            "blocked_smoke_evidence_present": False,
            "fail_smoke_evidence_present": False,
            "worker_scoped_lock_evidence_present": False,
            "worker_scoped_settle_evidence_present": False,
            "mixed_ready_candidate": False,
            "current_blockers": [],
            "evidence": [],
        }
    return {
        "worker_id": worker.worker_id,
        "backend_kind": worker.backend_kind,
        "registry_present": worker.registry_present,
        "worker_present": worker.worker_present,
        "mixed_ready": worker.mixed_ready,
        "business_ready": worker.business_ready,
        "allow_mixed_worker": worker.allow_mixed_worker,
        "allow_business_repo_writes": worker.allow_business_repo_writes,
        "evidence_plan_available": worker.evidence_plan_available,
        "evidence_execution_plan_available": worker.evidence_execution_plan_available,
        "evidence_collected": worker.evidence_collected,
        "done_smoke_evidence_present": worker.done_smoke_ok,
        "blocked_smoke_evidence_present": worker.blocked_smoke_ok,
        "fail_smoke_evidence_present": worker.fail_smoke_ok,
        "worker_scoped_lock_evidence_present": worker.worker_scoped_lock_ok,
        "worker_scoped_settle_evidence_present": worker.worker_scoped_settle_ok,
        "mixed_ready_candidate": worker.mixed_ready_candidate,
        "current_blockers": list(worker.blockers),
        "evidence": list(worker.readiness.get("evidence", [])) if isinstance(worker.readiness.get("evidence", []), list) else [],
    }


def _missing_worker_status(
    *,
    root_path: Path,
    worker_id: str,
    opencode_registry: Path,
    registry_present: bool,
) -> OpenCodeMixedWorkerStatus:
    return OpenCodeMixedWorkerStatus(
        worker_id=worker_id,
        backend_kind=BACKEND_KIND,
        registry_present=registry_present,
        registry_valid=registry_present,
        worker_present=False,
        evidence_plan_available=False,
        evidence_execution_plan_available=False,
        evidence_collected=False,
        done_smoke_ok=False,
        blocked_smoke_ok=False,
        fail_smoke_ok=False,
        worker_scoped_lock_ok=False,
        worker_scoped_settle_ok=False,
        mixed_ready_candidate=False,
        mixed_ready=False,
        allow_mixed_worker=False,
        business_ready=False,
        allow_business_repo_writes=False,
        would_execute_live=False,
        would_write_registry=False,
        would_touch_worker=False,
        blockers=[f"OpenCode worker missing: {worker_id}"],
        warnings=[f"OpenCode mixed status read registry only: {opencode_registry}"],
        forbidden_next_actions=["OpenCode live submit", "tmux input", "registry write"],
        readiness={"root": str(root_path), "worker_id": worker_id},
        evidence_execution_plan={},
    )


def _select_workers(workers: list[dict[str, Any]], *, worker_id: str | None, include_all: bool) -> list[dict[str, Any]]:
    if worker_id:
        return [worker for worker in workers if str(worker.get("worker_id") or "") == worker_id]
    if include_all:
        return workers
    defaults = [worker for worker in workers if bool(worker.get("default_for_new_tasks"))]
    if defaults:
        return defaults[:1]
    candidates = [worker for worker in workers if bool(worker.get("allow_auto_submit", False))]
    return candidates[:1] if candidates else workers[:1]


def _opencode_workers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    workers = payload.get("workers", []) if isinstance(payload.get("workers"), list) else []
    return [
        dict(worker)
        for worker in workers
        if str(worker.get("backend_kind", worker.get("backend", ""))) in {"opencode_tui", "opencode-tui"}
    ]


def _scope(worker_id: str | None, include_all: bool) -> str:
    if worker_id:
        return "worker"
    if include_all:
        return "all_workers"
    return "default_worker"


def _registry_present(readiness: dict[str, Any]) -> bool:
    for item in readiness.get("evidence", []):
        if isinstance(item, dict) and item.get("source") == "opencode_visible_registry":
            detail = item.get("detail", {})
            return bool(isinstance(detail, dict) and detail.get("present", False))
    return False


def _status_warnings(worker_id: str, evidence_collected: bool, mixed_ready: bool) -> list[str]:
    warnings = [
        "OpenCode mixed status is read-only/no-live",
        "OpenCode mixed status does not update registries or submit workers",
    ]
    if not evidence_collected:
        warnings.append(f"OpenCode worker {worker_id} mixed-readiness evidence is missing or incomplete")
    if evidence_collected and not mixed_ready:
        warnings.append(f"OpenCode worker {worker_id} has collected evidence but registry mixed_ready remains false")
    return warnings


def _resolve_fixture(value: Path | str) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_dir():
        return path.resolve()
    root = Path(__file__).resolve().parents[3]
    candidate = root / "tests" / "fixtures" / "worker-backends" / "opencode-mixed-status" / str(value)
    if candidate.is_dir():
        return candidate.resolve()
    return path


def _resolve_source_readiness_fixture(fixture_path: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        direct = (fixture_path / path).resolve()
        if direct.is_dir():
            return direct
        root = Path(__file__).resolve().parents[3]
        candidate = root / "tests" / "fixtures" / "worker-backends" / "opencode-mixed-readiness" / value
        if candidate.is_dir():
            return candidate.resolve()
    return path.resolve()


def _build_builtin_fixture_status(name: str) -> dict[str, Any]:
    scenario = name.strip()
    if scenario in {"current-blocked", "evidence-collected-candidate", "mixed-ready-true", "registry-missing", "worker-missing"}:
        with tempfile.TemporaryDirectory(prefix=f"opencode-mixed-status-{scenario}-") as tmp:
            fixture = Path(tmp)
            _write_builtin_fixture(fixture, scenario)
            return source_opencode_mixed_status_summary(
                fixture,
                worker_id="vw-fixture4107",
                include_all=False,
                execution_mode="no-live",
            )
    raise FileNotFoundError(f"OpenCode mixed status fixture not found: {name}")


def _write_builtin_fixture(fixture: Path, scenario: str) -> None:
    worker = {
        "worker_id": "vw-fixture4107",
        "backend": "opencode_tui",
        "allow_auto_submit": True,
        "default_for_new_tasks": True,
        "allow_mixed_worker": scenario in {"mixed-ready-true", "evidence-collected-candidate"},
        "mixed_ready": scenario == "mixed-ready-true",
        "business_ready": False,
        "allow_business_repo_writes": False,
        "allowed_write_roots": [str(fixture / "opencode")],
    }
    if scenario != "registry-missing":
        workers = [] if scenario == "worker-missing" else [worker]
        _write_json(fixture / ".omx/framework/team/opencode_visible_workers.json", {"workers": workers})
    _write_json(
        fixture / ".omx/framework/team/registered-workers/registered-workers.json",
        {
            "schema_version": 1,
            "registry_kind": "registered_worker_registry",
            "registry_id": "opencode-mixed-status-builtin",
            "created_at": "2026-06-05T00:00:00Z",
            "updated_at": "2026-06-05T00:00:00Z",
            "workers": [],
        },
    )
    if scenario in {"evidence-collected-candidate", "mixed-ready-true"}:
        evidence_root = fixture / EVIDENCE_ROOT_REL / "vw-fixture4107"
        for kind, marker in (("done", ".done"), ("blocked", ".blocked"), ("fail", ".fail")):
            task_dir = evidence_root / kind / "task-001"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "report.md").write_text(f"{kind} smoke report\n", encoding="utf-8")
            (task_dir / marker).write_text("sentinel\n", encoding="utf-8")
        _write_json(
            evidence_root / "lock-evidence.json",
            {
                "worker_id": "vw-fixture4107",
                "worker_scoped_lock_evidence_ok": True,
                "one_active_task_per_worker": True,
                "active_nonterminal_locks": 0,
                "terminal_stale_lock_settleable": True,
                "lock_archive_terminal_only": True,
            },
        )
        _write_json(
            evidence_root / "settle-evidence.json",
            {
                "worker_id": "vw-fixture4107",
                "worker_scoped_settle_evidence_ok": True,
                "settle_latest_supported": True,
                "post_timeout_terminal_settle_supported": True,
                "pending_after_timeout_supported": True,
                "no_redispatch_on_pending": True,
            },
        )


def _read_optional_json(path: Path) -> tuple[dict[str, Any], bool]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, False
    return (payload if isinstance(payload, dict) else {}), True


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result
