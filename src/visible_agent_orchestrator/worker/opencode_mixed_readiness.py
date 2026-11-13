"""Source-only OpenCode mixed-readiness candidate gate.

This module evaluates whether an OpenCode visible worker has enough local
evidence to become a mixed-ready candidate for a future sandbox/no-op mixed
contract. It never writes the OpenCode visible registry, enables business
readiness, executes live work, calls tmux, calls OpenCode, or invokes Claude
Code.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime_paths import state_team_root, workspace_root

from .registered_registry import RegisteredWorkerRegistry
from .registered_worker import TERMINAL_MARKERS


GATE_SCHEMA_VERSION = 1
BUSINESS_REPO_ROOT = str(workspace_root())


@dataclass(frozen=True)
class OpenCodeMixedReadinessEvidence:
    source: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeMixedReadinessRequest:
    root: str
    worker_id: str
    opencode_visible_registry_path: str
    registered_worker_registry_path: str
    done_smoke_path: str = ""
    blocked_smoke_path: str = ""
    fail_smoke_path: str = ""
    lock_evidence_path: str = ""
    settle_evidence_path: str = ""
    execution_mode: str = "no-live"
    runtime_telemetry: dict[str, Any] = field(default_factory=dict)
    opencode_allowed_write_roots: list[str] = field(default_factory=list)
    claude_allowed_write_roots: list[str] = field(default_factory=list)
    business_repo_writes_requested: bool = False
    cursor_requested: bool = False
    fixture_only: bool = False
    fixture_path: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, fixture_path: str = "") -> "OpenCodeMixedReadinessRequest":
        return cls(
            root=str(payload.get("root", "")),
            worker_id=str(payload.get("worker_id", "")),
            opencode_visible_registry_path=str(payload.get("opencode_visible_registry_path", "")),
            registered_worker_registry_path=str(payload.get("registered_worker_registry_path", "")),
            done_smoke_path=str(payload.get("done_smoke_path", payload.get("done_smoke", ""))),
            blocked_smoke_path=str(payload.get("blocked_smoke_path", payload.get("blocked_smoke", ""))),
            fail_smoke_path=str(payload.get("fail_smoke_path", payload.get("fail_smoke", ""))),
            lock_evidence_path=str(payload.get("lock_evidence_path", payload.get("lock_evidence", ""))),
            settle_evidence_path=str(payload.get("settle_evidence_path", payload.get("settle_evidence", ""))),
            execution_mode=str(payload.get("execution_mode", "no-live")),
            runtime_telemetry=_dict(payload.get("runtime_telemetry", {})),
            opencode_allowed_write_roots=_string_list(payload.get("opencode_allowed_write_roots", [])),
            claude_allowed_write_roots=_string_list(payload.get("claude_allowed_write_roots", [])),
            business_repo_writes_requested=bool(payload.get("business_repo_writes_requested", False)),
            cursor_requested=bool(payload.get("cursor_requested", False)),
            fixture_only=bool(payload.get("fixture_only", bool(fixture_path))),
            fixture_path=fixture_path or str(payload.get("fixture_path", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeMixedReadinessResult:
    ok: bool
    worker_id: str
    backend_kind: str
    opencode_worker_present: bool
    opencode_registry_valid: bool
    done_smoke_ok: bool
    blocked_smoke_ok: bool
    fail_smoke_ok: bool
    terminal_protocol_ok: bool
    worker_scoped_lock_ok: bool
    worker_scoped_settle_ok: bool
    no_active_locks: bool
    no_watcher_alerts: bool
    write_scope_disjoint: bool
    business_writes_forbidden: bool
    state_team_forbidden: bool
    obsidian_forbidden: bool
    mixed_ready_candidate: bool
    business_ready_candidate: bool
    would_update_registry: bool
    would_execute_live: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeMixedReadinessGateResult:
    schema_version: int
    gate_id: str
    created_at: str
    backend_kind: str
    worker_id: str
    opencode_visible_registry: str
    opencode_worker_record: dict[str, Any]
    required_terminal_evidence: dict[str, Any]
    required_worker_scoped_lock_evidence: dict[str, Any]
    required_worker_scoped_settle_evidence: dict[str, Any]
    required_health_evidence: dict[str, Any]
    required_write_scope_evidence: dict[str, Any]
    required_business_policy: dict[str, Any]
    required_mixed_policy: dict[str, Any]
    current_readiness: dict[str, Any]
    mixed_ready_candidate: bool
    business_ready_candidate: bool
    would_update_registry: bool
    would_execute_live: bool
    result: OpenCodeMixedReadinessResult
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "gate_id": self.gate_id,
            "created_at": self.created_at,
            "backend_kind": self.backend_kind,
            "worker_id": self.worker_id,
            "opencode_visible_registry": self.opencode_visible_registry,
            "opencode_worker_record": self.opencode_worker_record,
            "required_terminal_evidence": self.required_terminal_evidence,
            "required_worker_scoped_lock_evidence": self.required_worker_scoped_lock_evidence,
            "required_worker_scoped_settle_evidence": self.required_worker_scoped_settle_evidence,
            "required_health_evidence": self.required_health_evidence,
            "required_write_scope_evidence": self.required_write_scope_evidence,
            "required_business_policy": self.required_business_policy,
            "required_mixed_policy": self.required_mixed_policy,
            "current_readiness": self.current_readiness,
            "mixed_ready_candidate": self.mixed_ready_candidate,
            "business_ready_candidate": self.business_ready_candidate,
            "would_update_registry": self.would_update_registry,
            "would_execute_live": self.would_execute_live,
            "result": self.result.to_dict(),
            "blocked_reasons": list(self.blocked_reasons),
            "warnings": list(self.warnings),
            "safe_next_actions": list(self.safe_next_actions),
            "forbidden_next_actions": list(self.forbidden_next_actions),
            "evidence": list(self.evidence),
        }


def build_opencode_mixed_readiness_check_from_files(
    *,
    root: Path | str,
    worker_id: str,
    opencode_visible_registry_path: Path | str,
    registered_worker_registry_path: Path | str,
    done_smoke_path: Path | str,
    blocked_smoke_path: Path | str,
    fail_smoke_path: Path | str,
    lock_evidence_path: Path | str,
    settle_evidence_path: Path | str,
    execution_mode: str = "no-live",
    runtime_telemetry: dict[str, Any] | None = None,
    opencode_allowed_write_roots: list[str] | None = None,
    claude_allowed_write_roots: list[str] | None = None,
    business_repo_writes_requested: bool = False,
    cursor_requested: bool = False,
    fixture_only: bool = False,
    fixture_path: Path | str | None = None,
) -> OpenCodeMixedReadinessGateResult:
    request = OpenCodeMixedReadinessRequest(
        root=str(Path(root).resolve()),
        worker_id=worker_id,
        opencode_visible_registry_path=str(Path(opencode_visible_registry_path).resolve()),
        registered_worker_registry_path=str(Path(registered_worker_registry_path).resolve()),
        done_smoke_path=str(Path(done_smoke_path).resolve()),
        blocked_smoke_path=str(Path(blocked_smoke_path).resolve()),
        fail_smoke_path=str(Path(fail_smoke_path).resolve()),
        lock_evidence_path=str(Path(lock_evidence_path).resolve()),
        settle_evidence_path=str(Path(settle_evidence_path).resolve()),
        execution_mode=execution_mode,
        runtime_telemetry=_dict(runtime_telemetry or {}),
        opencode_allowed_write_roots=list(opencode_allowed_write_roots or []),
        claude_allowed_write_roots=list(claude_allowed_write_roots or []),
        business_repo_writes_requested=business_repo_writes_requested,
        cursor_requested=cursor_requested,
        fixture_only=fixture_only,
        fixture_path=str(Path(fixture_path).resolve()) if fixture_path else "",
    )
    return build_opencode_mixed_readiness_check(request)


def build_opencode_mixed_readiness_fixture_check(fixture_dir: Path | str) -> OpenCodeMixedReadinessGateResult:
    fixture = Path(fixture_dir).resolve()
    request_payload = _read_json(fixture / "request.json")
    request = OpenCodeMixedReadinessRequest.from_dict(request_payload, fixture_path=str(fixture))
    request = OpenCodeMixedReadinessRequest(
        **{
            **request.to_dict(),
            "root": str(fixture),
            "opencode_visible_registry_path": str(fixture / "opencode_visible_workers.json"),
            "registered_worker_registry_path": str(fixture / "registered-workers.json"),
            "done_smoke_path": str(_resolve_path(request.done_smoke_path or "smoke/done", fixture)),
            "blocked_smoke_path": str(_resolve_path(request.blocked_smoke_path or "smoke/blocked", fixture)),
            "fail_smoke_path": str(_resolve_path(request.fail_smoke_path or "smoke/fail", fixture)),
            "lock_evidence_path": str(_resolve_path(request.lock_evidence_path or "lock-evidence.json", fixture)),
            "settle_evidence_path": str(_resolve_path(request.settle_evidence_path or "settle-evidence.json", fixture)),
            "runtime_telemetry": _read_json_or_empty(fixture / "runtime-telemetry.json"),
            "fixture_only": True,
            "fixture_path": str(fixture),
        }
    )
    return build_opencode_mixed_readiness_check(request)


def build_opencode_mixed_readiness_check(
    request: OpenCodeMixedReadinessRequest,
) -> OpenCodeMixedReadinessGateResult:
    root = Path(request.root).resolve() if request.root else Path(".").resolve()
    opencode_path = Path(request.opencode_visible_registry_path).resolve()
    registered_path = Path(request.registered_worker_registry_path).resolve()
    blocked: list[str] = []
    warnings: list[str] = [
        "OpenCode mixed-readiness gate is source-only and no-live",
        "mixed_ready_candidate is not OpenCode registry mixed_ready",
        "business_ready_candidate remains false",
    ]
    evidence: list[dict[str, Any]] = []

    if request.execution_mode != "no-live":
        blocked.append("mixed live execution is disabled; execution_mode must be no-live")
    if request.cursor_requested:
        blocked.append("Cursor requested but disabled / out of scope")

    opencode_payload, opencode_present = _read_optional_json(opencode_path)
    worker = _find_opencode_worker(opencode_payload, request.worker_id)
    opencode_worker_present = bool(worker)
    opencode_registry_valid = opencode_present and _has_workers_list(opencode_payload)
    evidence.append(
        {
            "source": "opencode_visible_registry",
            "detail": {
                "registry_path": str(opencode_path),
                "present": opencode_present,
                "registry_valid": opencode_registry_valid,
                "worker_present": opencode_worker_present,
            },
        }
    )
    if not opencode_present:
        blocked.append(f"OpenCode visible registry missing: {opencode_path}")
    elif not opencode_registry_valid:
        blocked.append("OpenCode visible registry invalid or missing workers list")
    if not opencode_worker_present:
        blocked.append(f"OpenCode worker missing: {request.worker_id}")
        worker = {}

    backend_kind = str(worker.get("backend_kind", worker.get("backend", "")))
    if opencode_worker_present and backend_kind not in {"opencode_tui", "opencode-tui"}:
        blocked.append(f"backend_kind must be opencode_tui for {request.worker_id}: {backend_kind}")
    if opencode_worker_present and not bool(worker.get("allow_auto_submit", False)):
        blocked.append(f"OpenCode worker {request.worker_id} allow_auto_submit=false blocks mixed-readiness candidate")
    registry_evidence_bundle_ok = _registry_evidence_bundle_ok(worker)

    registered_payload, registered_present = _read_optional_json(registered_path)
    claude_roots = request.claude_allowed_write_roots or _registered_claude_allowed_write_roots(registered_payload)
    evidence.append(
        {
            "source": "registered_worker_registry",
            "detail": {"registry_path": str(registered_path), "present": registered_present, "claude_write_roots": claude_roots},
        }
    )

    smoke_statuses = {
        "done": _smoke_status(Path(request.done_smoke_path), ".done"),
        "blocked": _smoke_status(Path(request.blocked_smoke_path), ".blocked"),
        "fail": _smoke_status(Path(request.fail_smoke_path), ".fail"),
    }
    done_smoke_ok = smoke_statuses["done"]["ok"]
    blocked_smoke_ok = smoke_statuses["blocked"]["ok"]
    fail_smoke_ok = smoke_statuses["fail"]["ok"]
    terminal_protocol_ok = done_smoke_ok and blocked_smoke_ok and fail_smoke_ok
    for label, status in smoke_statuses.items():
        evidence.append({"source": f"{label}_smoke", "detail": status})
        if not status["path_present"]:
            if not registry_evidence_bundle_ok:
                blocked.append(f"{label} smoke evidence missing: {status['path']}")
        elif not status["report_present"]:
            blocked.append(f"{label} smoke report.md missing or empty: {status['path']}")
        elif status["terminal_marker_count"] != 1:
            blocked.append(f"{label} smoke terminal marker count must be 1: {status['terminal_marker_count']}")
        elif not status["expected_marker_present"]:
            blocked.append(f"{label} smoke wrong marker: expected {status['expected_marker']} got {status['terminal_marker']}")

    lock_evidence = _read_json_or_empty(Path(request.lock_evidence_path))
    settle_evidence = _read_json_or_empty(Path(request.settle_evidence_path))
    worker_scoped_lock_ok = _lock_evidence_ok(lock_evidence, request.worker_id)
    worker_scoped_settle_ok = _settle_evidence_ok(settle_evidence, request.worker_id)
    if registry_evidence_bundle_ok:
        worker_scoped_lock_ok = bool(worker_scoped_lock_ok or worker.get("worker_scoped_lock_evidence_ok", False))
        worker_scoped_settle_ok = bool(worker_scoped_settle_ok or worker.get("settle_evidence_ok", False))
    evidence.append({"source": "worker_scoped_lock_evidence", "detail": lock_evidence})
    evidence.append({"source": "worker_scoped_settle_evidence", "detail": settle_evidence})
    if not worker_scoped_lock_ok:
        blocked.append(f"OpenCode worker {request.worker_id} lacks worker-scoped lock evidence")
    if not worker_scoped_settle_ok:
        blocked.append(f"OpenCode worker {request.worker_id} lacks worker-scoped settle evidence")

    runtime = _runtime_telemetry(root, request)
    no_active_locks = _active_lock_count(runtime) == 0
    no_watcher_alerts = _active_alert_count(runtime) == 0
    evidence.append({"source": "runtime_telemetry", "detail": runtime})
    if not no_active_locks:
        blocked.append(f"active locks > 0: {_active_lock_count(runtime)}")
    if not no_watcher_alerts:
        blocked.append(f"watcher alerts > 0: {_active_alert_count(runtime)}")

    opencode_roots = request.opencode_allowed_write_roots or _string_list(worker.get("allowed_write_roots", []))
    write_scope_disjoint = not _write_scope_overlap(opencode_roots, claude_roots)
    if not write_scope_disjoint:
        blocked.append("write scope overlap between OpenCode and Claude workers")

    combined_roots = opencode_roots + claude_roots
    business_writes_forbidden = not request.business_repo_writes_requested and not _roots_overlap_path(combined_roots, BUSINESS_REPO_ROOT)
    if request.business_repo_writes_requested:
        blocked.append("business repo writes requested but business tasks remain forbidden")
    if _roots_overlap_path(combined_roots, BUSINESS_REPO_ROOT):
        blocked.append("business repo write root is forbidden")

    state_team_forbidden = not _roots_overlap_path(combined_roots, str(state_team_root(request.root)))
    obsidian_forbidden = not any("obsidian" in root.lower() for root in combined_roots)
    if not state_team_forbidden:
        blocked.append(".omx/state/team write root is forbidden")
    if not obsidian_forbidden:
        blocked.append("Obsidian write root is forbidden")

    mixed_ready_candidate = (
        not blocked
        and opencode_worker_present
        and backend_kind in {"opencode_tui", "opencode-tui"}
        and terminal_protocol_ok
        and worker_scoped_lock_ok
        and worker_scoped_settle_ok
    )
    business_ready_candidate = False
    would_update_registry = False
    would_execute_live = False

    current_readiness = {
        "business_ready": bool(worker.get("business_ready", False)),
        "mixed_ready": bool(worker.get("mixed_ready", False)),
        "allow_business_repo_writes": bool(worker.get("allow_business_repo_writes", False)),
        "allow_mixed_worker": bool(worker.get("allow_mixed_worker", False)),
        "allow_auto_submit": bool(worker.get("allow_auto_submit", False)),
        "binding_confidence": str(worker.get("binding_confidence", "")),
        "server_live": bool(worker.get("server_live", False)),
    }
    result = OpenCodeMixedReadinessResult(
        ok=mixed_ready_candidate,
        worker_id=request.worker_id,
        backend_kind="opencode_tui" if backend_kind in {"opencode_tui", "opencode-tui"} else backend_kind,
        opencode_worker_present=opencode_worker_present,
        opencode_registry_valid=opencode_registry_valid,
        done_smoke_ok=done_smoke_ok,
        blocked_smoke_ok=blocked_smoke_ok,
        fail_smoke_ok=fail_smoke_ok,
        terminal_protocol_ok=terminal_protocol_ok,
        worker_scoped_lock_ok=worker_scoped_lock_ok,
        worker_scoped_settle_ok=worker_scoped_settle_ok,
        no_active_locks=no_active_locks,
        no_watcher_alerts=no_watcher_alerts,
        write_scope_disjoint=write_scope_disjoint,
        business_writes_forbidden=business_writes_forbidden,
        state_team_forbidden=state_team_forbidden,
        obsidian_forbidden=obsidian_forbidden,
        mixed_ready_candidate=mixed_ready_candidate,
        business_ready_candidate=business_ready_candidate,
        would_update_registry=would_update_registry,
        would_execute_live=would_execute_live,
        blocked_reasons=_dedupe(blocked),
        warnings=_dedupe(warnings),
        safe_next_actions=_safe_next_actions(mixed_ready_candidate),
        forbidden_next_actions=_forbidden_next_actions(),
        evidence=_dedupe_evidence(evidence),
    )

    return OpenCodeMixedReadinessGateResult(
        schema_version=GATE_SCHEMA_VERSION,
        gate_id="opencode-mixed-readiness-gate",
        created_at=_utc_now(),
        backend_kind=result.backend_kind,
        worker_id=request.worker_id,
        opencode_visible_registry=str(opencode_path),
        opencode_worker_record=worker,
        required_terminal_evidence={
            "done_smoke_required": True,
            "blocked_smoke_required": True,
            "fail_smoke_required": True,
            "report_md_nonempty_required": True,
            "exactly_one_terminal_marker_required": True,
        },
        required_worker_scoped_lock_evidence={
            "one_active_task_per_worker_required": True,
            "active_nonterminal_lock_blocks": True,
            "terminal_stale_lock_must_be_settleable": True,
            "lock_archive_terminal_only_required": True,
            "lock_evidence_path": request.lock_evidence_path,
            "worker_scoped_lock_ok": worker_scoped_lock_ok,
        },
        required_worker_scoped_settle_evidence={
            "settle_latest_supported": True,
            "post_timeout_terminal_settle_supported": True,
            "pending_after_timeout_supported": True,
            "no_redispatch_on_pending_required": True,
            "settle_evidence_path": request.settle_evidence_path,
            "worker_scoped_settle_ok": worker_scoped_settle_ok,
        },
        required_health_evidence={
            "require_no_active_locks": True,
            "require_no_watcher_alerts": True,
        },
        required_write_scope_evidence={
            "opencode_allowed_write_roots": opencode_roots,
            "claude_allowed_write_roots": claude_roots,
            "disjoint_write_roots_required": True,
            "business_repo_forbidden": True,
            "state_team_forbidden": True,
            "obsidian_forbidden": True,
        },
        required_business_policy={
            "allow_business_tasks": False,
            "business_ready_candidate_must_remain_false": True,
            "business_repo_writes_requested": request.business_repo_writes_requested,
        },
        required_mixed_policy={
            "allow_business_tasks": False,
            "allow_mixed_live": False,
            "allow_mixed_noop_sandbox_candidate": mixed_ready_candidate,
            "require_no_active_locks": True,
            "require_no_watcher_alerts": True,
            "require_terminal_smokes": True,
            "require_worker_scoped_lock_evidence": True,
            "require_worker_scoped_settle_evidence": True,
        },
        current_readiness=current_readiness,
        mixed_ready_candidate=mixed_ready_candidate,
        business_ready_candidate=business_ready_candidate,
        would_update_registry=would_update_registry,
        would_execute_live=would_execute_live,
        result=result,
        blocked_reasons=result.blocked_reasons,
        warnings=result.warnings,
        safe_next_actions=result.safe_next_actions,
        forbidden_next_actions=result.forbidden_next_actions,
        evidence=result.evidence,
    )


def _find_opencode_worker(payload: dict[str, Any], worker_id: str) -> dict[str, Any]:
    workers = payload.get("workers", []) if isinstance(payload.get("workers", []), list) else []
    for worker in workers:
        if str(worker.get("worker_id", "")) == worker_id:
            return dict(worker)
    return {}


def _registry_evidence_bundle_ok(worker: dict[str, Any]) -> bool:
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
        "mixed_ready",
    )
    return all(bool(worker.get(flag, False)) for flag in required_flags)


def _has_workers_list(payload: dict[str, Any]) -> bool:
    return isinstance(payload.get("workers", []), list)


def _registered_claude_allowed_write_roots(payload: dict[str, Any]) -> list[str]:
    if not payload:
        return []
    registry = RegisteredWorkerRegistry.from_dict(payload)
    roots: list[str] = []
    for worker in registry.workers:
        if str(worker.get("backend_kind", "")) == "claude_code":
            roots.extend(_string_list(worker.get("allowed_write_roots", [])))
    return _dedupe(roots)


def _smoke_status(path: Path, expected_marker: str) -> dict[str, Any]:
    markers = [marker for marker in TERMINAL_MARKERS if (path / marker).is_file()]
    report = path / "report.md"
    report_present = report.is_file() and bool(report.read_text(encoding="utf-8", errors="replace").strip())
    terminal_marker = markers[0] if len(markers) == 1 else ""
    return {
        "path": str(path),
        "path_present": path.is_dir(),
        "report_present": report_present,
        "expected_marker": expected_marker,
        "expected_marker_present": terminal_marker == expected_marker,
        "terminal_marker": terminal_marker,
        "terminal_marker_count": len(markers),
        "ok": path.is_dir() and report_present and len(markers) == 1 and terminal_marker == expected_marker,
    }


def _lock_evidence_ok(payload: dict[str, Any], worker_id: str) -> bool:
    return (
        bool(payload.get("worker_scoped_lock_evidence_ok", False))
        and str(payload.get("worker_id", "")) == worker_id
        and bool(payload.get("one_active_task_per_worker", False))
        and int(payload.get("active_nonterminal_locks", 1) or 0) == 0
        and bool(payload.get("terminal_stale_lock_settleable", False))
        and bool(payload.get("lock_archive_terminal_only", False))
    )


def _settle_evidence_ok(payload: dict[str, Any], worker_id: str) -> bool:
    return (
        bool(payload.get("worker_scoped_settle_evidence_ok", False))
        and str(payload.get("worker_id", "")) == worker_id
        and bool(payload.get("settle_latest_supported", False))
        and bool(payload.get("post_timeout_terminal_settle_supported", False))
        and bool(payload.get("pending_after_timeout_supported", False))
        and bool(payload.get("no_redispatch_on_pending", False))
    )


def _runtime_telemetry(root: Path, request: OpenCodeMixedReadinessRequest) -> dict[str, Any]:
    if request.runtime_telemetry:
        return request.runtime_telemetry
    alerts = _read_json_or_empty(root / ".omx" / "framework" / "team" / "visible-runs" / "_watcher" / "current_alerts.json")
    locks_root = root / ".omx" / "framework" / "team" / "visible-runs" / "_locks"
    locks = []
    if locks_root.is_dir():
        for path in sorted(locks_root.glob("*.lock.json")):
            data = _read_json_or_empty(path)
            if data and data.get("status") != "released" and str(data.get("worker_id") or "") == request.worker_id:
                locks.append(data)
    alert_items = alerts.get("alerts", []) if isinstance(alerts.get("alerts"), list) else []
    worker_alerts = [
        item
        for item in alert_items
        if isinstance(item, dict) and str(item.get("worker_id") or item.get("worker") or "") == request.worker_id
    ]
    return {
        "active_alerts": len(worker_alerts),
        "alerts": worker_alerts,
        "locks": locks,
    }


def _active_lock_count(runtime: dict[str, Any]) -> int:
    locks = runtime.get("locks", []) if isinstance(runtime.get("locks"), list) else []
    return int(runtime.get("active_locks", runtime.get("active_nonterminal_locks", len(locks))) or 0)


def _active_alert_count(runtime: dict[str, Any]) -> int:
    return int(runtime.get("active_alerts", 0) or 0)


def _write_scope_overlap(left: list[str], right: list[str]) -> bool:
    for left_root in left:
        for right_root in right:
            if _paths_overlap(left_root, right_root):
                return True
    return False


def _roots_overlap_path(roots: list[str], target: str) -> bool:
    return any(_paths_overlap(root, target) for root in roots)


def _paths_overlap(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_path = Path(left).resolve()
    right_path = Path(right).resolve()
    return left_path == right_path or left_path in right_path.parents or right_path in left_path.parents


def _safe_next_actions(candidate_ok: bool) -> list[str]:
    if candidate_ok:
        return [
            "source-only install smoke for opencode-mixed-readiness-check",
            "separate explicit OpenCode mixed_ready registry update path review",
            "fixture/sandbox mixed no-op contract planning",
        ]
    return ["fix missing source-only OpenCode evidence and rerun fixture check"]


def _forbidden_next_actions() -> list[str]:
    return [
        "write OpenCode visible registry",
        "write registered worker registry",
        "mixed live execution",
        "business repo writes",
        "OpenCode live submit",
        "Claude Code live submit",
        "tmux input",
        "cursor-agent execution",
        "Obsidian writes",
    ]


def _resolve_path(value: str, base_path: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base_path / path
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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


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
