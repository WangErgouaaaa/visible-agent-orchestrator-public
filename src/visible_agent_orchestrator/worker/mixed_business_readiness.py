"""Source-only mixed business-readiness gate planning.

This module models the next gate after a successful mixed no-op live closeout.
It never executes workers, never writes registries, and never grants
``business_ready``. The output is a review artifact for a future explicit
authorization step.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime_paths import workspace_root

from .mixed_noop_live_runner import validate_mixed_noop_live_result_file


SCHEMA_VERSION = 1
PLAN_KIND = "mixed_business_readiness_gate_no_live"
NEXT_STATE = "AWAITING_MIXED_BUSINESS_READINESS_GATE_REVIEW_NO_LIVE"
DEFAULT_BUSINESS_REPO_ROOT = str(workspace_root())
DEFAULT_REQUIRED_GATES = [
    "explicit_user_authorization_for_business_readiness",
    "business_write_scope_contract",
    "opencode_writer_claude_reviewer_role_split",
    "dry_run_or_fixture_business_task_first",
    "tiny_sandbox_business_write_smoke",
    "reports_diffs_tests_untracked_review",
    "separate_business_ready_registry_update_gate",
    "business_repo_index_protection",
]
FORBIDDEN_NEXT_ACTIONS = [
    "mixed live execution",
    "business repository writes",
    "registered worker registry writes",
    "OpenCode visible registry writes",
    "raw tmux input",
    "direct /tui HTTP calls",
    "direct adapter submit",
    "rmux live execution",
    "Claude Code invocation",
    "Cursor execution",
]


@dataclass(frozen=True)
class MixedBusinessReadinessRequest:
    root: str
    registered_worker_registry_path: str
    opencode_visible_registry_path: str
    mixed_result_path: str
    include_workers: list[str] = field(default_factory=list)
    execution_mode: str = "no-live"
    business_readiness_requested: bool = False
    business_repo_writes_requested: bool = False
    registry_write_requested: bool = False
    business_task_live_requested: bool = False
    raw_tmux_requested: bool = False
    direct_tui_requested: bool = False
    direct_adapter_submit_requested: bool = False
    rmux_requested: bool = False
    business_write_scope_contract_present: bool = False
    opencode_allowed_write_roots: list[str] = field(default_factory=list)
    claude_allowed_write_roots: list[str] = field(default_factory=list)
    forbidden_business_paths: list[str] = field(default_factory=lambda: [DEFAULT_BUSINESS_REPO_ROOT])
    test_command_plan: list[str] = field(default_factory=list)
    dry_run_fixture_task_present: bool = False
    tiny_sandbox_smoke_planned: bool = False
    fixture_only: bool = False
    fixture_path: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, fixture_path: str = "") -> "MixedBusinessReadinessRequest":
        return cls(
            root=str(payload.get("root", "")),
            registered_worker_registry_path=str(payload.get("registered_worker_registry_path", payload.get("registered_worker_registry", "registered-workers.json"))),
            opencode_visible_registry_path=str(payload.get("opencode_visible_registry_path", payload.get("opencode_visible_registry", "opencode_visible_workers.json"))),
            mixed_result_path=str(payload.get("mixed_result_path", payload.get("mixed_result", "mixed-result.json"))),
            include_workers=[str(item) for item in payload.get("include_workers", payload.get("include_worker", []))],
            execution_mode=str(payload.get("execution_mode", "no-live")),
            business_readiness_requested=bool(payload.get("business_readiness_requested", False)),
            business_repo_writes_requested=bool(payload.get("business_repo_writes_requested", False)),
            registry_write_requested=bool(payload.get("registry_write_requested", False)),
            business_task_live_requested=bool(payload.get("business_task_live_requested", False)),
            raw_tmux_requested=bool(payload.get("raw_tmux_requested", False)),
            direct_tui_requested=bool(payload.get("direct_tui_requested", False)),
            direct_adapter_submit_requested=bool(payload.get("direct_adapter_submit_requested", False)),
            rmux_requested=bool(payload.get("rmux_requested", False)),
            business_write_scope_contract_present=bool(payload.get("business_write_scope_contract_present", False)),
            opencode_allowed_write_roots=[str(item) for item in payload.get("opencode_allowed_write_roots", [])],
            claude_allowed_write_roots=[str(item) for item in payload.get("claude_allowed_write_roots", [])],
            forbidden_business_paths=[str(item) for item in payload.get("forbidden_business_paths", [DEFAULT_BUSINESS_REPO_ROOT])],
            test_command_plan=[str(item) for item in payload.get("test_command_plan", [])],
            dry_run_fixture_task_present=bool(payload.get("dry_run_fixture_task_present", False)),
            tiny_sandbox_smoke_planned=bool(payload.get("tiny_sandbox_smoke_planned", False)),
            fixture_only=bool(payload.get("fixture_only", bool(fixture_path))),
            fixture_path=fixture_path or str(payload.get("fixture_path", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_mixed_business_readiness_plan_from_files(
    *,
    root: Path | str,
    registered_worker_registry_path: Path | str,
    opencode_visible_registry_path: Path | str,
    mixed_result_path: Path | str,
    include_workers: list[str] | None = None,
    execution_mode: str = "no-live",
    business_readiness_requested: bool = False,
    business_repo_writes_requested: bool = False,
    registry_write_requested: bool = False,
    business_task_live_requested: bool = False,
    raw_tmux_requested: bool = False,
    direct_tui_requested: bool = False,
    direct_adapter_submit_requested: bool = False,
    rmux_requested: bool = False,
    business_write_scope_contract_present: bool = False,
    opencode_allowed_write_roots: list[str] | None = None,
    claude_allowed_write_roots: list[str] | None = None,
    forbidden_business_paths: list[str] | None = None,
    test_command_plan: list[str] | None = None,
    dry_run_fixture_task_present: bool = False,
    tiny_sandbox_smoke_planned: bool = False,
    fixture_only: bool = False,
    fixture_path: Path | str | None = None,
) -> dict[str, Any]:
    request = MixedBusinessReadinessRequest(
        root=str(Path(root).resolve()),
        registered_worker_registry_path=str(Path(registered_worker_registry_path).resolve()),
        opencode_visible_registry_path=str(Path(opencode_visible_registry_path).resolve()),
        mixed_result_path=str(Path(mixed_result_path).resolve()),
        include_workers=list(include_workers or []),
        execution_mode=execution_mode,
        business_readiness_requested=business_readiness_requested,
        business_repo_writes_requested=business_repo_writes_requested,
        registry_write_requested=registry_write_requested,
        business_task_live_requested=business_task_live_requested,
        raw_tmux_requested=raw_tmux_requested,
        direct_tui_requested=direct_tui_requested,
        direct_adapter_submit_requested=direct_adapter_submit_requested,
        rmux_requested=rmux_requested,
        business_write_scope_contract_present=business_write_scope_contract_present,
        opencode_allowed_write_roots=list(opencode_allowed_write_roots or []),
        claude_allowed_write_roots=list(claude_allowed_write_roots or []),
        forbidden_business_paths=list(forbidden_business_paths or [DEFAULT_BUSINESS_REPO_ROOT]),
        test_command_plan=list(test_command_plan or []),
        dry_run_fixture_task_present=dry_run_fixture_task_present,
        tiny_sandbox_smoke_planned=tiny_sandbox_smoke_planned,
        fixture_only=fixture_only,
        fixture_path=str(Path(fixture_path).resolve()) if fixture_path else "",
    )
    return build_mixed_business_readiness_plan(request)


def build_mixed_business_readiness_fixture_plan(fixture_dir: Path | str) -> dict[str, Any]:
    fixture = Path(fixture_dir).resolve()
    payload = _read_json(fixture / "request.json")
    request = MixedBusinessReadinessRequest.from_dict(payload, fixture_path=str(fixture))
    request = MixedBusinessReadinessRequest(
        **{
            **request.to_dict(),
            "root": str(fixture),
            "registered_worker_registry_path": str(_fixture_path(request.registered_worker_registry_path, fixture)),
            "opencode_visible_registry_path": str(_fixture_path(request.opencode_visible_registry_path, fixture)),
            "mixed_result_path": str(_fixture_path(request.mixed_result_path, fixture)),
            "fixture_only": True,
            "fixture_path": str(fixture),
        }
    )
    return build_mixed_business_readiness_plan(request)


def build_mixed_business_readiness_plan(request: MixedBusinessReadinessRequest) -> dict[str, Any]:
    root = Path(request.root).resolve() if request.root else Path(".").resolve()
    registered_path = Path(request.registered_worker_registry_path).resolve()
    opencode_path = Path(request.opencode_visible_registry_path).resolve()
    mixed_result_path = Path(request.mixed_result_path).resolve()
    blocked: list[str] = []
    warnings = [
        "business readiness gate is source-only and no-live",
        "business_ready is not granted by this plan",
        "registry writes remain a separate future gate",
        "business repository writes remain forbidden",
    ]
    evidence: list[dict[str, Any]] = []

    if request.execution_mode != "no-live":
        blocked.append("mixed business-readiness gate requires execution_mode=no-live")

    registered_payload, registered_present = _read_optional_json(registered_path)
    opencode_payload, opencode_present = _read_optional_json(opencode_path)
    if not registered_present:
        blocked.append("registered worker registry missing")
    if not opencode_present:
        blocked.append("OpenCode visible registry missing")
    workers = _selected_workers(registered_payload, opencode_payload, request.include_workers)
    present = {str(worker.get("worker_id", "")) for worker in workers}
    for worker_id in request.include_workers:
        if worker_id not in present:
            blocked.append(f"included worker missing: {worker_id}")
    if not any(worker.get("backend_kind") == "opencode_tui" for worker in workers):
        blocked.append("OpenCode worker missing")
    if not any(worker.get("backend_kind") == "claude_code" for worker in workers):
        blocked.append("Claude worker missing")

    for worker in workers:
        worker_id = str(worker.get("worker_id", "<missing>"))
        if worker.get("mixed_ready") is not True:
            blocked.append(f"worker {worker_id} mixed_ready must be true before business-readiness review")
        if worker.get("allow_mixed_worker") is not True:
            blocked.append(f"worker {worker_id} allow_mixed_worker must be true before business-readiness review")
        if worker.get("business_ready") is True:
            blocked.append(f"worker {worker_id} business_ready=true is unexpected before separate registry gate")
        if worker.get("allow_business_repo_writes") is True:
            blocked.append(f"worker {worker_id} allow_business_repo_writes=true requires explicit future gate")

    mixed_validation = _mixed_result_validation(mixed_result_path)
    evidence.append({"source": "mixed_noop_result", "detail": mixed_validation})
    if not mixed_validation.get("present"):
        blocked.append("mixed no-op result missing")
    elif not mixed_validation.get("ok"):
        blocked.append("mixed no-op result validation failed")
    if mixed_validation.get("aggregate_state") != "completed":
        blocked.append("mixed no-op result must be completed")
    if mixed_validation.get("business_repo_modified"):
        blocked.append("mixed no-op result modified business repo")
    if mixed_validation.get("registry_modified"):
        blocked.append("mixed no-op result modified registry")
    if mixed_validation.get("state_team_modified"):
        blocked.append("mixed no-op result modified .omx/state/team")

    blocked.extend(_request_surface_blocks(request))
    if request.business_readiness_requested:
        blocked.extend(_future_business_gate_blocks(request))

    opencode_scope = _dedupe(request.opencode_allowed_write_roots)
    claude_scope = _dedupe(request.claude_allowed_write_roots)
    if _scopes_overlap(opencode_scope, claude_scope):
        blocked.append("requested business write scopes overlap")

    blocked = _dedupe(blocked)
    business_candidate = request.business_readiness_requested and not blocked
    readiness_blockers = [] if business_candidate else _readiness_blockers(request)

    return {
        "schema_version": SCHEMA_VERSION,
        "plan_kind": PLAN_KIND,
        "created_at": _utc_now(),
        "ok": not blocked,
        "root": str(root),
        "registered_worker_registry": str(registered_path),
        "opencode_visible_registry": str(opencode_path),
        "mixed_result_path": str(mixed_result_path),
        "include_workers": list(request.include_workers),
        "workers": workers,
        "worker_ids": sorted(present),
        "mixed_noop_result_valid": bool(mixed_validation.get("ok")),
        "mixed_noop_completed": mixed_validation.get("aggregate_state") == "completed",
        "business_readiness_requested": request.business_readiness_requested,
        "business_ready_gate_candidate": business_candidate,
        "business_ready_granted": False,
        "business_task_allowed": False,
        "business_repo_writes_allowed": False,
        "would_execute_live": False,
        "would_write_registry": False,
        "would_write_business": False,
        "would_write_state_team": False,
        "would_use_raw_tmux": False,
        "would_use_direct_tui": False,
        "would_use_direct_adapter_submit": False,
        "would_use_rmux": False,
        "role_split": {
            "opencode": "business_task_writer_candidate",
            "claude": "business_task_reviewer_candidate",
            "overlap_allowed": False,
        },
        "business_write_scope_contract": {
            "present": request.business_write_scope_contract_present,
            "opencode_allowed_write_roots": opencode_scope,
            "claude_allowed_write_roots": claude_scope,
            "forbidden_business_paths": _dedupe(request.forbidden_business_paths),
            "write_scope_disjoint": not _scopes_overlap(opencode_scope, claude_scope),
        },
        "test_command_plan": list(request.test_command_plan),
        "dry_run_fixture_task_present": request.dry_run_fixture_task_present,
        "tiny_sandbox_smoke_planned": request.tiny_sandbox_smoke_planned,
        "required_next_gates": list(DEFAULT_REQUIRED_GATES),
        "readiness_blockers": readiness_blockers,
        "blocked_reasons": blocked,
        "warnings": _dedupe(warnings),
        "safe_next_actions": [
            "review this no-live plan",
            "prepare a fixture business task only after explicit user authorization",
            "prepare a separate registry update gate only after business readiness is approved",
        ],
        "forbidden_next_actions": list(FORBIDDEN_NEXT_ACTIONS),
        "next_state": NEXT_STATE,
        "evidence": evidence
        + [
            {"source": "registered_worker_registry", "detail": {"present": registered_present, "path": str(registered_path)}},
            {"source": "opencode_visible_registry", "detail": {"present": opencode_present, "path": str(opencode_path)}},
        ],
    }


def validate_mixed_business_readiness_plan_file(path: Path | str) -> dict[str, Any]:
    plan_path = Path(path).resolve()
    payload = _read_json(plan_path)
    blocked: list[str] = []
    if payload.get("plan_kind") != PLAN_KIND:
        blocked.append("plan_kind must be mixed_business_readiness_gate_no_live")
    if payload.get("next_state") != NEXT_STATE:
        blocked.append("next_state must remain AWAITING_MIXED_BUSINESS_READINESS_GATE_REVIEW_NO_LIVE")
    for field in [
        "business_ready_granted",
        "business_task_allowed",
        "business_repo_writes_allowed",
        "would_execute_live",
        "would_write_registry",
        "would_write_business",
        "would_write_state_team",
        "would_use_raw_tmux",
        "would_use_direct_tui",
        "would_use_direct_adapter_submit",
        "would_use_rmux",
    ]:
        if bool(payload.get(field, True)):
            blocked.append(f"{field} must be false")
    gates = payload.get("required_next_gates", [])
    for gate in DEFAULT_REQUIRED_GATES:
        if gate not in gates:
            blocked.append(f"required_next_gates missing {gate}")
    return {
        "schema_version": SCHEMA_VERSION,
        "validation_kind": "mixed_business_readiness_plan",
        "ok": not blocked,
        "plan_path": str(plan_path),
        "blocked_reasons": _dedupe(blocked),
        "warnings": ["validation is source-only and does not grant business readiness"],
    }


def _selected_workers(registered_payload: dict[str, Any], opencode_payload: dict[str, Any], include_workers: list[str]) -> list[dict[str, Any]]:
    include = set(include_workers)
    workers: list[dict[str, Any]] = []
    for item in _list(opencode_payload.get("workers")):
        worker_id = str(item.get("worker_id", ""))
        if include and worker_id not in include:
            continue
        workers.append(
            {
                "worker_id": worker_id,
                "backend_kind": "opencode_tui",
                "registry_source": "opencode_visible_registry",
                "mixed_ready": bool(item.get("mixed_ready", False)),
                "allow_mixed_worker": bool(item.get("allow_mixed_worker", False)),
                "business_ready": bool(item.get("business_ready", False)),
                "allow_business_repo_writes": bool(item.get("allow_business_repo_writes", False)),
                "allowed_mixed_task_kinds": _list(item.get("allowed_mixed_task_kinds")),
            }
        )
    for item in _list(registered_payload.get("workers")):
        worker_id = str(item.get("worker_id", ""))
        if include and worker_id not in include:
            continue
        backend = str(item.get("backend_kind", item.get("backend", "")))
        workers.append(
            {
                "worker_id": worker_id,
                "backend_kind": backend,
                "registry_source": "registered_worker_registry",
                "mixed_ready": bool(item.get("mixed_ready", False)),
                "allow_mixed_worker": bool(item.get("allow_mixed_worker", False)),
                "business_ready": bool(item.get("business_ready", False)),
                "allow_business_repo_writes": bool(item.get("allow_business_repo_writes", False)),
                "allowed_mixed_task_kinds": _list(item.get("allowed_mixed_task_kinds")),
            }
        )
    return workers


def _mixed_result_validation(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False, "ok": False, "path": str(path)}
    try:
        payload = _read_json(path)
        if payload.get("validation_kind") in {"mixed-noop-live-result", "mixed_noop_live_result"}:
            return {"present": True, **payload}
        if "contract_path" in payload or "worker_results" in payload:
            return {"present": True, **validate_mixed_noop_live_result_file(path)}
        return {"present": True, "ok": bool(payload.get("ok")), **payload}
    except Exception as exc:  # pragma: no cover - defensive boundary for operator-supplied JSON
        return {"present": True, "ok": False, "path": str(path), "blocked_reasons": [str(exc)]}


def _request_surface_blocks(request: MixedBusinessReadinessRequest) -> list[str]:
    blocked: list[str] = []
    if request.business_repo_writes_requested:
        blocked.append("business repo write requested before business-readiness gate approval")
    if request.registry_write_requested:
        blocked.append("registry write requested before separate registry update gate")
    if request.business_task_live_requested:
        blocked.append("business task live execution requested before no-live review")
    if request.raw_tmux_requested:
        blocked.append("raw tmux requested")
    if request.direct_tui_requested:
        blocked.append("direct /tui requested")
    if request.direct_adapter_submit_requested:
        blocked.append("direct adapter submit requested")
    if request.rmux_requested:
        blocked.append("rmux requested")
    return blocked


def _future_business_gate_blocks(request: MixedBusinessReadinessRequest) -> list[str]:
    blocked: list[str] = []
    if not request.business_write_scope_contract_present:
        blocked.append("business write-scope contract missing")
    if not request.opencode_allowed_write_roots:
        blocked.append("OpenCode business write scope missing")
    if not request.claude_allowed_write_roots:
        blocked.append("Claude review scope missing")
    if not request.test_command_plan:
        blocked.append("test command plan missing")
    if not request.dry_run_fixture_task_present:
        blocked.append("dry-run fixture business task missing")
    if not request.tiny_sandbox_smoke_planned:
        blocked.append("tiny sandbox business-write smoke missing")
    return blocked


def _readiness_blockers(request: MixedBusinessReadinessRequest) -> list[str]:
    blockers = list(DEFAULT_REQUIRED_GATES)
    if request.business_readiness_requested:
        blockers.extend(_future_business_gate_blocks(request))
    else:
        blockers.append("business_readiness_requested=false")
    return _dedupe(blockers)


def _scopes_overlap(left: list[str], right: list[str]) -> bool:
    for a in left:
        for b in right:
            if _paths_overlap(a, b):
                return True
    return False


def _paths_overlap(left: str, right: str) -> bool:
    if not left or not right:
        return False
    a = Path(left).resolve()
    b = Path(right).resolve()
    return a == b or str(a).startswith(str(b) + "/") or str(b).startswith(str(a) + "/")


def _fixture_path(value: str, fixture: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else fixture / path


def _read_optional_json(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.is_file():
        return {}, False
    return _read_json(path), True


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_REQUIRED_GATES",
    "MixedBusinessReadinessRequest",
    "build_mixed_business_readiness_fixture_plan",
    "build_mixed_business_readiness_plan",
    "build_mixed_business_readiness_plan_from_files",
    "validate_mixed_business_readiness_plan_file",
]
