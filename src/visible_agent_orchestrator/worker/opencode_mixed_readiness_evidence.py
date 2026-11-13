"""Source-only OpenCode mixed-readiness evidence smoke planning.

This module plans and validates evidence for the OpenCode mixed-readiness
gate. It never runs OpenCode, calls tmux, invokes Claude, writes registries,
or promotes a worker to mixed-ready.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime_paths import framework_root, state_team_root, workspace_root


SCHEMA_VERSION = 1
PLAN_KIND = "opencode-mixed-readiness-evidence-smoke-plan"
BACKEND_KIND = "opencode_tui"
EVIDENCE_ROOT_REL = ".omx/framework/team/opencode-mixed-readiness-smokes"
BUSINESS_REPO_ROOT = str(workspace_root())
TERMINAL_MARKERS = [".done", ".blocked", ".fail"]


@dataclass(frozen=True)
class OpenCodeEvidenceSmokeMatrix:
    done_smoke: dict[str, Any]
    blocked_smoke: dict[str, Any]
    fail_smoke: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceSmokeResultValidation:
    done_smoke_ok: bool
    blocked_smoke_ok: bool
    fail_smoke_ok: bool
    terminal_protocol_ok: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeLockSettleEvidenceValidation:
    worker_scoped_lock_ok: bool
    worker_scoped_settle_ok: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceSmokePlanRequest:
    root: str
    worker_id: str
    opencode_visible_registry: str
    registered_worker_registry: str
    execution_mode: str = "no-live"
    evidence_root: str = ""
    cursor_requested: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, fixture_dir: Path | None = None) -> "OpenCodeEvidenceSmokePlanRequest":
        base = fixture_dir.resolve() if fixture_dir else None
        root = str(_resolve_optional(payload.get("root", "."), base))
        evidence_root = str(_resolve_optional(payload.get("evidence_root", ""), base)) if payload.get("evidence_root") else ""
        return cls(
            root=root,
            worker_id=str(payload.get("worker_id", "")),
            opencode_visible_registry=str(_resolve_optional(payload.get("opencode_visible_registry", payload.get("opencode_visible_registry_path", "")), base)),
            registered_worker_registry=str(_resolve_optional(payload.get("registered_worker_registry", payload.get("registered_worker_registry_path", "")), base)),
            execution_mode=str(payload.get("execution_mode", "no-live")),
            evidence_root=evidence_root,
            cursor_requested=bool(payload.get("cursor_requested", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceSmokePlanResult:
    ok: bool
    plan_id: str
    worker_id: str
    backend_kind: str
    execution_mode: str
    would_execute_live: bool
    would_write_registry: bool
    evidence_root: str
    smoke_matrix: dict[str, Any]
    expected_task_dirs: dict[str, str]
    expected_reports: dict[str, str]
    expected_markers: dict[str, str]
    lock_evidence_requirements: dict[str, Any]
    settle_evidence_requirements: dict[str, Any]
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeMixedReadinessEvidenceValidationResult:
    ok: bool
    done_smoke_ok: bool
    blocked_smoke_ok: bool
    fail_smoke_ok: bool
    terminal_protocol_ok: bool
    worker_scoped_lock_ok: bool
    worker_scoped_settle_ok: bool
    mixed_ready_candidate: bool
    business_ready_candidate: bool
    would_execute_live: bool
    would_update_registry: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_opencode_mixed_readiness_evidence_plan_from_files(
    *,
    root: Path | str,
    worker_id: str,
    opencode_visible_registry: Path | str,
    registered_worker_registry: Path | str,
    execution_mode: str = "no-live",
) -> dict[str, Any]:
    request = OpenCodeEvidenceSmokePlanRequest(
        root=str(Path(root).resolve()),
        worker_id=worker_id,
        opencode_visible_registry=str(Path(opencode_visible_registry).resolve()),
        registered_worker_registry=str(Path(registered_worker_registry).resolve()),
        execution_mode=execution_mode,
    )
    return build_opencode_mixed_readiness_evidence_plan(request)


def build_opencode_mixed_readiness_evidence_fixture_plan(fixture_dir: Path | str) -> dict[str, Any]:
    fixture = Path(fixture_dir).resolve()
    request = OpenCodeEvidenceSmokePlanRequest.from_dict(_read_json(fixture / "request.json"), fixture_dir=fixture)
    return build_opencode_mixed_readiness_evidence_plan(request)


def build_opencode_mixed_readiness_evidence_plan(request: OpenCodeEvidenceSmokePlanRequest) -> dict[str, Any]:
    root = Path(request.root).resolve() if request.root else Path(".").resolve()
    evidence_root = Path(request.evidence_root).resolve() if request.evidence_root else root / EVIDENCE_ROOT_REL / request.worker_id
    blocked: list[str] = []
    warnings = [
        "OpenCode mixed-readiness evidence smoke plan is source-only and no-live",
        "evidence validation may produce mixed_ready_candidate=true but never registry mixed_ready=true",
        "business_ready_candidate remains false and business tasks remain forbidden",
    ]
    if request.execution_mode != "no-live":
        blocked.append("evidence smoke live execution is disabled; execution_mode must be no-live")
    if request.cursor_requested:
        blocked.append("Cursor requested but disabled / out of scope")

    expected_task_dirs = {
        "done_smoke": str(evidence_root / "done" / "task-001"),
        "blocked_smoke": str(evidence_root / "blocked" / "task-001"),
        "fail_smoke": str(evidence_root / "fail" / "task-001"),
    }
    expected_reports = {name: f"{path}/report.md" for name, path in expected_task_dirs.items()}
    expected_markers = {
        "done_smoke": ".done",
        "blocked_smoke": ".blocked",
        "fail_smoke": ".fail",
    }
    smoke_matrix = {
        name: {
            "expected_marker": marker,
            "required_report_md_nonempty": True,
            "terminal_marker_count": "exactly_one",
            "task_dir": expected_task_dirs[name],
            "report_path": expected_reports[name],
        }
        for name, marker in expected_markers.items()
    }
    allowed_write_roots = [str(evidence_root)]
    selected_framework_root = framework_root(request.root)
    forbidden_paths = [
        BUSINESS_REPO_ROOT,
        str(state_team_root(request.root)),
        str(selected_framework_root / "registered-workers" / "registered-workers.json"),
        str(selected_framework_root / "opencode_visible_workers.json"),
    ]
    result = OpenCodeEvidenceSmokePlanResult(
        ok=not blocked,
        plan_id=f"opencode-evidence-smoke-{request.worker_id}",
        worker_id=request.worker_id,
        backend_kind=BACKEND_KIND,
        execution_mode="no-live",
        would_execute_live=False,
        would_write_registry=False,
        evidence_root=str(evidence_root),
        smoke_matrix=smoke_matrix,
        expected_task_dirs=expected_task_dirs,
        expected_reports=expected_reports,
        expected_markers=expected_markers,
        lock_evidence_requirements=_lock_requirements(request.worker_id, evidence_root),
        settle_evidence_requirements=_settle_requirements(request.worker_id, evidence_root),
        blocked_reasons=_dedupe(blocked),
        warnings=warnings,
        safe_next_actions=[
            "future evidence smoke execution must use controlled visible dispatch or multiworker wrapper",
            "validate produced evidence before any separate OpenCode mixed_ready registry update",
            "keep mixed live and business tasks forbidden",
        ],
        forbidden_next_actions=_forbidden_next_actions(),
        evidence=[
            {"source": "plan_request", "detail": request.to_dict()},
            {"source": "allowed_write_roots", "detail": {"allowed_write_roots": allowed_write_roots}},
            {"source": "forbidden_paths", "detail": {"forbidden_paths": forbidden_paths}},
        ],
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "plan_id": result.plan_id,
        "created_at": _utc_now(),
        "plan_kind": PLAN_KIND,
        "backend_kind": BACKEND_KIND,
        "worker_id": request.worker_id,
        "execution_mode": "no-live",
        "root": str(root),
        "opencode_visible_registry": request.opencode_visible_registry,
        "registered_worker_registry": request.registered_worker_registry,
        "evidence_root": str(evidence_root),
        "smoke_matrix": smoke_matrix,
        "worker_scoped_lock_contract": _lock_contract(request.worker_id, evidence_root),
        "worker_scoped_settle_contract": _settle_contract(request.worker_id, evidence_root),
        "allowed_write_roots": allowed_write_roots,
        "forbidden_paths": forbidden_paths,
        "terminal_protocol": _terminal_protocol(),
        "expected_evidence": {
            "task_dirs": expected_task_dirs,
            "reports": expected_reports,
            "markers": expected_markers,
            "lock_evidence": str(evidence_root / "lock-evidence.json"),
            "settle_evidence": str(evidence_root / "settle-evidence.json"),
        },
        "result_validation": {
            "validator": "validate-opencode-mixed-readiness-evidence",
            "mixed_ready_candidate_can_be_true": True,
            "business_ready_candidate_must_remain_false": True,
            "would_execute_live_must_remain_false": True,
            "would_update_registry_must_remain_false": True,
        },
        "would_execute_live": False,
        "would_write_registry": False,
        "blocked_reasons": result.blocked_reasons,
        "warnings": result.warnings,
        "safe_next_actions": result.safe_next_actions,
        "forbidden_next_actions": result.forbidden_next_actions,
        "evidence": result.evidence,
        "result": result.to_dict(),
    }


def validate_opencode_mixed_readiness_evidence_file(evidence_path: Path | str) -> OpenCodeMixedReadinessEvidenceValidationResult:
    evidence_path = Path(evidence_path).resolve()
    payload = _read_json(evidence_path)
    return validate_opencode_mixed_readiness_evidence(payload, evidence_path=evidence_path)


def validate_opencode_mixed_readiness_evidence(
    payload: dict[str, Any],
    *,
    evidence_path: Path | None = None,
) -> OpenCodeMixedReadinessEvidenceValidationResult:
    worker_id = str(payload.get("worker_id", ""))
    blocked: list[str] = []
    warnings = [
        "OpenCode mixed-readiness evidence validation is source-only and no-live",
        "mixed_ready_candidate is not registry mixed_ready",
        "business_ready_candidate remains false",
    ]
    if payload.get("execution_mode", "no-live") != "no-live":
        blocked.append("evidence live execution is disabled; execution_mode must be no-live")
    if bool(payload.get("cursor_requested", False)):
        blocked.append("Cursor requested but disabled / out of scope")

    smokes = _dict(payload.get("smoke_matrix", payload.get("smokes", {})))
    done = _validate_smoke("done_smoke", smokes.get("done_smoke", smokes.get("done", {})), ".done", blocked)
    blocked_smoke = _validate_smoke("blocked_smoke", smokes.get("blocked_smoke", smokes.get("blocked", {})), ".blocked", blocked)
    fail = _validate_smoke("fail_smoke", smokes.get("fail_smoke", smokes.get("fail", {})), ".fail", blocked)
    terminal_protocol_ok = done and blocked_smoke and fail

    lock_payload = _dict(payload.get("worker_scoped_lock_evidence", payload.get("lock_evidence", {})))
    settle_payload = _dict(payload.get("worker_scoped_settle_evidence", payload.get("settle_evidence", {})))
    worker_scoped_lock_ok = _validate_lock(lock_payload, worker_id, blocked)
    worker_scoped_settle_ok = _validate_settle(settle_payload, worker_id, blocked)

    _validate_forbidden_writes(payload, blocked)

    would_execute_live = False
    would_update_registry = False
    business_ready_candidate = False
    mixed_ready_candidate = (
        not blocked
        and terminal_protocol_ok
        and worker_scoped_lock_ok
        and worker_scoped_settle_ok
    )
    return OpenCodeMixedReadinessEvidenceValidationResult(
        ok=mixed_ready_candidate,
        done_smoke_ok=done,
        blocked_smoke_ok=blocked_smoke,
        fail_smoke_ok=fail,
        terminal_protocol_ok=terminal_protocol_ok,
        worker_scoped_lock_ok=worker_scoped_lock_ok,
        worker_scoped_settle_ok=worker_scoped_settle_ok,
        mixed_ready_candidate=mixed_ready_candidate,
        business_ready_candidate=business_ready_candidate,
        would_execute_live=would_execute_live,
        would_update_registry=would_update_registry,
        blocked_reasons=_dedupe(blocked),
        warnings=warnings,
        evidence=[
            {"source": "evidence_file", "detail": {"path": str(evidence_path or "")}},
            {"source": "terminal_smokes", "detail": smokes},
            {"source": "worker_scoped_lock_evidence", "detail": lock_payload},
            {"source": "worker_scoped_settle_evidence", "detail": settle_payload},
        ],
    )


def _validate_smoke(label: str, payload: Any, expected_marker: str, blocked: list[str]) -> bool:
    data = _dict(payload)
    if not data:
        blocked.append(f"{label} evidence missing")
        return False
    report_nonempty = bool(data.get("report_md_nonempty", data.get("required_report_md_nonempty", False)))
    markers = data.get("terminal_markers", data.get("markers", []))
    if isinstance(markers, str):
        markers = [markers]
    markers = [str(marker) for marker in markers] if isinstance(markers, list) else []
    if not report_nonempty:
        blocked.append(f"{label} report.md missing or empty")
    if len(markers) != 1:
        blocked.append(f"{label} terminal marker count must be 1: {len(markers)}")
    elif markers[0] != expected_marker:
        blocked.append(f"{label} wrong marker: expected {expected_marker} got {markers[0]}")
    return report_nonempty and len(markers) == 1 and markers[0] == expected_marker


def _validate_lock(payload: dict[str, Any], worker_id: str, blocked: list[str]) -> bool:
    checks = {
        "worker_id": str(payload.get("worker_id", "")) == worker_id,
        "worker_scoped_lock_evidence_ok": bool(payload.get("worker_scoped_lock_evidence_ok", False)),
        "one_worker_one_active_task": bool(payload.get("one_worker_one_active_task", False)),
        "lock_must_reference_run_and_task": bool(payload.get("lock_must_reference_run_and_task", False)),
        "nonterminal_lock_blocks_new_task": bool(payload.get("nonterminal_lock_blocks_new_task", False)),
        "terminal_lock_can_be_archived": bool(payload.get("terminal_lock_can_be_archived", False)),
        "lock_archive_requires_terminal_truth": bool(payload.get("lock_archive_requires_terminal_truth", False)),
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        blocked.append("OpenCode worker lacks worker-scoped lock evidence")
        for key in missing:
            blocked.append(f"worker-scoped lock evidence missing: {key}")
    return not missing


def _validate_settle(payload: dict[str, Any], worker_id: str, blocked: list[str]) -> bool:
    checks = {
        "worker_id": str(payload.get("worker_id", "")) == worker_id,
        "worker_scoped_settle_evidence_ok": bool(payload.get("worker_scoped_settle_evidence_ok", False)),
        "settle_latest_required": bool(payload.get("settle_latest_required", False)),
        "post_timeout_pending_supported": bool(payload.get("post_timeout_pending_supported", False)),
        "terminal_after_timeout_must_settle": bool(payload.get("terminal_after_timeout_must_settle", False)),
        "nonterminal_after_timeout_must_block": bool(payload.get("nonterminal_after_timeout_must_block", False)),
        "no_redispatch_on_pending": bool(payload.get("no_redispatch_on_pending", False)),
    }
    missing = [key for key, ok in checks.items() if not ok]
    if missing:
        blocked.append("OpenCode worker lacks worker-scoped settle evidence")
        for key in missing:
            if key == "terminal_after_timeout_must_settle":
                blocked.append("post-timeout terminal settle evidence missing")
            elif key == "nonterminal_after_timeout_must_block":
                blocked.append("post-timeout nonterminal block evidence missing")
            else:
                blocked.append(f"worker-scoped settle evidence missing: {key}")
    return not missing


def _validate_forbidden_writes(payload: dict[str, Any], blocked: list[str]) -> None:
    writes = _dict(payload.get("write_evidence", payload.get("writes", {})))
    business = _list(writes.get("business_repo", [])) or _list(payload.get("business_repo_writes", []))
    state_team = _list(writes.get("state_team", [])) or _list(payload.get("state_team_writes", []))
    registries = _list(writes.get("registries", [])) or _list(payload.get("registry_writes", []))
    if bool(payload.get("business_repo_writes_detected", False)) or business:
        blocked.append("business repo write evidence detected")
    if bool(payload.get("state_team_writes_detected", False)) or state_team:
        blocked.append(".omx/state/team write evidence detected")
    if bool(payload.get("registry_writes_detected", False)) or registries:
        blocked.append("registry write evidence detected")
    for path in _list(writes.get("paths", [])):
        text = str(path)
        if text.startswith(BUSINESS_REPO_ROOT):
            blocked.append("business repo write evidence detected")
        if "/.omx/state/team" in text:
            blocked.append(".omx/state/team write evidence detected")
        if text.endswith("/registered-workers/registered-workers.json") or text.endswith("/opencode_visible_workers.json"):
            blocked.append("registry write evidence detected")


def _lock_contract(worker_id: str, evidence_root: Path) -> dict[str, Any]:
    return {
        "one_worker_one_active_task": True,
        "lock_path_pattern": str(evidence_root / "locks" / f"{worker_id}-<run-id>-<task-id>.lock.json"),
        "lock_owner_worker_id": worker_id,
        "lock_must_reference_run_and_task": True,
        "nonterminal_lock_blocks_new_task": True,
        "terminal_lock_can_be_archived": True,
        "lock_archive_requires_terminal_truth": True,
    }


def _settle_contract(worker_id: str, evidence_root: Path) -> dict[str, Any]:
    return {
        "settle_latest_required": True,
        "settle_path_pattern": str(evidence_root / "settle" / f"{worker_id}-<run-id>-<task-id>.json"),
        "post_timeout_pending_supported": True,
        "terminal_after_timeout_must_settle": True,
        "nonterminal_after_timeout_must_block": True,
        "no_redispatch_on_pending": True,
    }


def _lock_requirements(worker_id: str, evidence_root: Path) -> dict[str, Any]:
    return {**_lock_contract(worker_id, evidence_root), "evidence_file": str(evidence_root / "lock-evidence.json")}


def _settle_requirements(worker_id: str, evidence_root: Path) -> dict[str, Any]:
    return {**_settle_contract(worker_id, evidence_root), "evidence_file": str(evidence_root / "settle-evidence.json")}


def _terminal_protocol() -> dict[str, Any]:
    return {
        "required_report": "report.md",
        "report_must_be_nonempty": True,
        "allowed_terminal_markers": TERMINAL_MARKERS,
        "terminal_marker_count": "exactly_one",
        "done_smoke_expected_marker": ".done",
        "blocked_smoke_expected_marker": ".blocked",
        "fail_smoke_expected_marker": ".fail",
    }


def _forbidden_next_actions() -> list[str]:
    return [
        "execute OpenCode live",
        "invoke Claude or claude-code",
        "send tmux input",
        "call direct /tui endpoints",
        "write OpenCode visible registry",
        "write registered worker registry",
        "write .omx/state/team",
        "write business repo",
        "mark mixed_ready=true",
        "run mixed live",
        "run cursor-agent",
        "write Obsidian",
    ]


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return data


def _resolve_optional(value: Any, base: Path | None) -> Path:
    text = str(value or ".")
    path = Path(text)
    if not path.is_absolute() and base:
        path = base / path
    return path.resolve()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
