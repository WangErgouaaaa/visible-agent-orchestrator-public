"""No-execution OpenCode evidence authorization package planning.

This module prepares a user-reviewable package for future OpenCode
mixed-readiness evidence smokes. Building the package does not execute live
work, touch workers, call tmux/OpenCode/Claude, update registries, or grant
business readiness.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime_paths import workspace_root

from .opencode_mixed_readiness_evidence_execution import (
    BACKEND_KIND,
    EVIDENCE_ROOT_REL,
    OpenCodeEvidenceExecutionPlanRequest,
    build_opencode_mixed_readiness_evidence_execution_plan,
)
from .opencode_mixed_status import source_opencode_mixed_status_summary


SCHEMA_VERSION = 1
PACKAGE_KIND = "opencode-evidence-live-authorization-package"
AUTHORIZATION_ROOT_REL = ".omx/framework/team/opencode-mixed-readiness-authorizations"
STATE_TEAM_REL = ".omx/state/team"
REGISTERED_REGISTRY_REL = ".omx/framework/team/registered-workers/registered-workers.json"
OPENCODE_REGISTRY_REL = ".omx/framework/team/opencode_visible_workers.json"
REQUIRED_SMOKES = ["done_smoke", "blocked_smoke", "fail_smoke"]


@dataclass(frozen=True)
class OpenCodeEvidenceAuthorizationRequest:
    root: str
    worker_id: str
    opencode_visible_registry: str
    registered_worker_registry: str
    execution_mode: str = "no-live"
    package_id: str = ""
    requested_future_execution: bool = True
    live_execution_requested: bool = False
    business_write_requested: bool = False
    registry_write_requested: bool = False
    state_team_write_requested: bool = False
    direct_tmux_requested: bool = False
    direct_tui_requested: bool = False
    direct_adapter_submit_requested: bool = False
    cursor_requested: bool = False
    claude_pane_touch_requested: bool = False
    rmux_live_requested: bool = False
    include_done_smoke_plan: bool = True
    include_blocked_smoke_plan: bool = True
    include_fail_smoke_plan: bool = True
    include_lock_evidence_plan: bool = True
    include_settle_evidence_plan: bool = True

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, fixture_dir: Path | None = None) -> "OpenCodeEvidenceAuthorizationRequest":
        base = fixture_dir.resolve() if fixture_dir else None
        return cls(
            root=str(_resolve_optional(payload.get("root", "."), base)),
            worker_id=str(payload.get("worker_id", "")),
            opencode_visible_registry=str(
                _resolve_optional(
                    payload.get("opencode_visible_registry", payload.get("opencode_visible_registry_path", "opencode_visible_workers.json")),
                    base,
                )
            ),
            registered_worker_registry=str(
                _resolve_optional(
                    payload.get("registered_worker_registry", payload.get("registered_worker_registry_path", "registered-workers.json")),
                    base,
                )
            ),
            execution_mode=str(payload.get("execution_mode", "no-live")),
            package_id=str(payload.get("package_id", "")),
            requested_future_execution=bool(payload.get("requested_future_execution", True)),
            live_execution_requested=bool(payload.get("live_execution_requested", False)),
            business_write_requested=bool(payload.get("business_write_requested", payload.get("business_repo_writes_requested", False))),
            registry_write_requested=bool(payload.get("registry_write_requested", payload.get("registry_update_requested", False))),
            state_team_write_requested=bool(payload.get("state_team_write_requested", False)),
            direct_tmux_requested=bool(payload.get("direct_tmux_requested", False)),
            direct_tui_requested=bool(payload.get("direct_tui_requested", False)),
            direct_adapter_submit_requested=bool(payload.get("direct_adapter_submit_requested", False)),
            cursor_requested=bool(payload.get("cursor_requested", False)),
            claude_pane_touch_requested=bool(payload.get("claude_pane_touch_requested", False)),
            rmux_live_requested=bool(payload.get("rmux_live_requested", False)),
            include_done_smoke_plan=bool(payload.get("include_done_smoke_plan", True)),
            include_blocked_smoke_plan=bool(payload.get("include_blocked_smoke_plan", True)),
            include_fail_smoke_plan=bool(payload.get("include_fail_smoke_plan", True)),
            include_lock_evidence_plan=bool(payload.get("include_lock_evidence_plan", True)),
            include_settle_evidence_plan=bool(payload.get("include_settle_evidence_plan", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceAuthorizationBuildResult:
    ok: bool
    package_id: str
    target_worker_id: str
    backend_kind: str
    would_execute_live: bool
    would_touch_worker: bool
    would_write_registry: bool
    planned_smoke_count: int
    allowed_write_roots: list[str]
    forbidden_paths: list[str]
    current_blockers: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)
    package_path: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceAuthorizationValidation:
    ok: bool
    package_id: str
    target_worker_id: str
    backend_kind: str
    no_live_execution: bool
    no_registry_write: bool
    no_business_writes: bool
    no_direct_tui: bool
    no_raw_tmux: bool
    smoke_matrix_complete: bool
    lock_evidence_planned: bool
    settle_evidence_planned: bool
    allowed_write_roots_valid: bool
    forbidden_paths_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_opencode_evidence_authorization_plan_from_files(
    *,
    root: Path | str,
    worker_id: str,
    opencode_visible_registry: Path | str,
    registered_worker_registry: Path | str,
    execution_mode: str = "no-live",
) -> dict[str, Any]:
    request = OpenCodeEvidenceAuthorizationRequest(
        root=str(Path(root).resolve()),
        worker_id=worker_id,
        opencode_visible_registry=str(Path(opencode_visible_registry).resolve()),
        registered_worker_registry=str(Path(registered_worker_registry).resolve()),
        execution_mode=execution_mode,
    )
    return build_opencode_evidence_authorization_package(request)


def build_opencode_evidence_authorization_fixture_plan(fixture_dir: Path | str) -> dict[str, Any]:
    fixture = Path(fixture_dir).resolve()
    request = OpenCodeEvidenceAuthorizationRequest.from_dict(_read_json(fixture / "request.json"), fixture_dir=fixture)
    package_path = fixture / "package.json"
    if package_path.is_file():
        validation = validate_opencode_evidence_authorization_package_file(package_path)
        package = _read_json(package_path)
        package["validation"] = validation.to_dict()
        package["result"] = _result_from_package(package, package_path="").to_dict()
        return package
    return build_opencode_evidence_authorization_package(request)


def write_opencode_evidence_authorization_package(
    request: OpenCodeEvidenceAuthorizationRequest,
    *,
    output_dir: Path | str,
) -> dict[str, Any]:
    package = build_opencode_evidence_authorization_package(request)
    out = Path(output_dir).resolve()
    if STATE_TEAM_REL in str(out):
        package["blocked_reasons"] = _dedupe(_list_str(package.get("blocked_reasons")) + [".omx/state/team package output requested"])
        package["result"]["blocked_reasons"] = package["blocked_reasons"]
        package["result"]["ok"] = False
        package["result"]["package_path"] = ""
        return package

    out.mkdir(parents=True, exist_ok=True)
    package_path = out / f"{package['package_id']}.json"
    md_path = out / f"{package['package_id']}.md"
    package["package_path"] = str(package_path)
    package["package_markdown_path"] = str(md_path)
    package["result"]["package_path"] = str(package_path)
    validation = validate_opencode_evidence_authorization_package(package)
    package["validation"] = validation.to_dict()
    package["result"]["ok"] = bool(package["result"].get("ok")) and validation.ok
    package_path.write_text(json.dumps(package, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(_package_markdown(package), encoding="utf-8")
    return package


def validate_opencode_evidence_authorization_package_file(package_path: Path | str) -> OpenCodeEvidenceAuthorizationValidation:
    path = Path(package_path).resolve()
    return validate_opencode_evidence_authorization_package(_read_json(path), package_path=path)


def validate_opencode_evidence_authorization_package(
    payload: dict[str, Any],
    *,
    package_path: Path | None = None,
) -> OpenCodeEvidenceAuthorizationValidation:
    errors: list[str] = []
    warnings = [
        "Authorization package validation is no-execution",
        "Package does not grant live execution or registry updates",
    ]

    package_id = str(payload.get("package_id", ""))
    target_worker_id = str(payload.get("target_worker_id", payload.get("worker_id", "")))
    backend_kind = str(payload.get("backend_kind", ""))
    if not package_id:
        errors.append("package_id missing")
    if not target_worker_id:
        errors.append("target worker missing")
    if backend_kind != BACKEND_KIND:
        errors.append(f"backend_kind must be {BACKEND_KIND}")

    no_live_execution = not bool(payload.get("would_execute_live", False)) and str(payload.get("execution_mode", "no-live")) == "no-live"
    no_registry_write = not bool(payload.get("would_write_registry", False)) and not bool(payload.get("registry_write_requested", False))
    no_business_writes = not bool(payload.get("business_write_requested", False)) and not bool(payload.get("allow_business_repo_writes", False))
    no_direct_tui = not bool(payload.get("direct_tui_requested", False))
    no_raw_tmux = not bool(payload.get("direct_tmux_requested", False))
    if not no_live_execution:
        errors.append("live execution requested")
    if bool(payload.get("would_touch_worker", False)):
        errors.append("would_touch_worker must be false")
    if not no_registry_write:
        errors.append("registry write requested")
    if not no_business_writes:
        errors.append("business write requested")
    if bool(payload.get("state_team_write_requested", False)):
        errors.append(".omx/state/team write requested")
    if not no_raw_tmux:
        errors.append("direct tmux input requested")
    if not no_direct_tui:
        errors.append("direct /tui request requested")
    if bool(payload.get("direct_adapter_submit_requested", False)):
        errors.append("direct adapter submit requested")
    if bool(payload.get("cursor_requested", False)):
        errors.append("Cursor requested")
    if bool(payload.get("claude_pane_touch_requested", False)):
        errors.append("Claude pane touch requested")
    if bool(payload.get("rmux_live_requested", False)):
        errors.append("rmux live requested")

    planned_smokes = [_dict(item) for item in _list(payload.get("planned_smokes"))]
    smoke_kinds = {str(item.get("smoke_kind", "")) for item in planned_smokes}
    smoke_matrix_complete = all(kind in smoke_kinds for kind in REQUIRED_SMOKES)
    if not smoke_matrix_complete:
        for kind in REQUIRED_SMOKES:
            if kind not in smoke_kinds:
                errors.append(f"{kind} plan missing")

    planned_lock = _dict(payload.get("planned_lock_evidence"))
    planned_settle = _dict(payload.get("planned_settle_evidence"))
    lock_evidence_planned = all(
        bool(planned_lock.get(key))
        for key in (
            "active_nonterminal_lock_blocks_new_task",
            "terminal_stale_lock_settle_requires_terminal_truth",
            "one_worker_one_active_task",
            "owner_lock_references_worker_run_task",
        )
    )
    settle_evidence_planned = all(
        bool(planned_settle.get(key))
        for key in (
            "settle_latest_available",
            "pending_after_timeout_supported",
            "terminal_after_timeout_can_be_settled",
            "nonterminal_after_timeout_remains_blocking",
            "no_redispatch_on_pending",
        )
    )
    if not lock_evidence_planned:
        errors.append("lock evidence plan missing")
    if not settle_evidence_planned:
        errors.append("settle evidence plan missing")

    root = str(payload.get("root", ""))
    allowed_write_roots = _list_str(payload.get("allowed_write_roots"))
    allowed_write_roots_valid = bool(allowed_write_roots)
    for path in allowed_write_roots:
        if EVIDENCE_ROOT_REL not in path:
            allowed_write_roots_valid = False
            errors.append("allowed write root outside opencode-mixed-readiness-smokes")
        if package_id and package_id not in path:
            allowed_write_roots_valid = False
            errors.append("allowed write root does not include package_id")
        if root and not path.startswith(root):
            allowed_write_roots_valid = False
            errors.append("allowed write root outside root")

    forbidden_paths = _list_str(payload.get("forbidden_paths"))
    forbidden_paths_valid = (
        str(workspace_root()) in forbidden_paths
        and any(path.endswith(STATE_TEAM_REL) for path in forbidden_paths)
        and any(path.endswith(REGISTERED_REGISTRY_REL) for path in forbidden_paths)
        and any(path.endswith(OPENCODE_REGISTRY_REL) for path in forbidden_paths)
    )
    if not forbidden_paths_valid:
        errors.append("required forbidden paths missing")

    dispatch = _dict(payload.get("required_dispatch_surface"))
    forbidden_dispatch = " ".join(_list_str(dispatch.get("forbidden")))
    for phrase in ("raw tmux", "direct /tui", "direct adapter submit", "direct curl", "rmux live"):
        if phrase not in forbidden_dispatch:
            errors.append(f"dispatch forbidden surface missing: {phrase}")

    return OpenCodeEvidenceAuthorizationValidation(
        ok=not errors,
        package_id=package_id,
        target_worker_id=target_worker_id,
        backend_kind=backend_kind,
        no_live_execution=no_live_execution,
        no_registry_write=no_registry_write,
        no_business_writes=no_business_writes,
        no_direct_tui=no_direct_tui,
        no_raw_tmux=no_raw_tmux,
        smoke_matrix_complete=smoke_matrix_complete,
        lock_evidence_planned=lock_evidence_planned,
        settle_evidence_planned=settle_evidence_planned,
        allowed_write_roots_valid=allowed_write_roots_valid,
        forbidden_paths_valid=forbidden_paths_valid,
        errors=_dedupe(errors),
        warnings=warnings,
        evidence=[
            {"source": "package_file", "detail": {"path": str(package_path or "")}},
            {"source": "planned_smokes", "detail": {"smoke_kinds": sorted(smoke_kinds)}},
            {"source": "required_dispatch_surface", "detail": dispatch},
        ],
    )


def build_opencode_evidence_authorization_package(request: OpenCodeEvidenceAuthorizationRequest) -> dict[str, Any]:
    root = Path(request.root).resolve()
    package_id = request.package_id or f"opencode-evidence-auth-{request.worker_id}-{_utc_now(compact=True)}"
    allowed_root = root / EVIDENCE_ROOT_REL / package_id
    blocked: list[str] = []
    warnings = [
        "Authorization package is no-execution and does not grant live execution",
        "Future live evidence smoke still requires separate explicit user authorization",
        "Package cannot set mixed_ready=true and cannot write registries",
        "Business tasks and business repo writes remain forbidden",
    ]
    evidence: list[dict[str, Any]] = [{"source": "authorization_request", "detail": request.to_dict()}]

    current_status = source_opencode_mixed_status_summary(
        root,
        worker_id=request.worker_id,
        include_all=False,
        execution_mode="no-live",
        opencode_visible_registry=request.opencode_visible_registry,
        registered_worker_registry=request.registered_worker_registry,
    )
    evidence.append({"source": "current_opencode_mixed_status", "detail": _status_evidence_summary(current_status)})
    current_blockers = _list_str(current_status.get("current_blockers", current_status.get("blockers", [])))
    worker_present = bool(current_status.get("worker_present", False))
    backend_kind = str(current_status.get("backend_kind") or BACKEND_KIND)
    if not worker_present:
        blocked.append(f"target worker missing: {request.worker_id}")
    if backend_kind != BACKEND_KIND:
        blocked.append(f"backend_kind must be {BACKEND_KIND}")

    if request.execution_mode != "no-live" or request.live_execution_requested:
        blocked.append("requested live execution now")
    if request.registry_write_requested:
        blocked.append("requested registry write")
    if request.business_write_requested:
        blocked.append("requested business repo write")
    if request.state_team_write_requested:
        blocked.append("requested .omx/state/team write")
    if request.direct_tmux_requested:
        blocked.append("requested direct tmux input")
    if request.direct_tui_requested:
        blocked.append("requested direct /tui")
    if request.direct_adapter_submit_requested:
        blocked.append("requested direct adapter submit")
    if request.cursor_requested:
        blocked.append("requested Cursor execution")
    if request.claude_pane_touch_requested:
        blocked.append("requested Claude pane touch")
    if request.rmux_live_requested:
        blocked.append("requested rmux live")

    execution_request = OpenCodeEvidenceExecutionPlanRequest(
        root=str(root),
        worker_id=request.worker_id,
        opencode_visible_registry=request.opencode_visible_registry,
        registered_worker_registry=request.registered_worker_registry,
        execution_mode="no-live",
        evidence_root=str(allowed_root),
        run_root=str(allowed_root / "future-run"),
    )
    execution_plan = build_opencode_mixed_readiness_evidence_execution_plan(execution_request)
    evidence.append({"source": "evidence_execution_plan", "detail": _execution_plan_evidence_summary(execution_plan)})

    planned_smokes = _planned_smokes(execution_plan, request)
    planned_kinds = {str(item.get("smoke_kind", "")) for item in planned_smokes}
    if "done_smoke" not in planned_kinds:
        blocked.append("done_smoke plan missing")
    if "blocked_smoke" not in planned_kinds:
        blocked.append("blocked_smoke plan missing")
    if "fail_smoke" not in planned_kinds:
        blocked.append("fail_smoke plan missing")
    planned_lock = _planned_lock_evidence(request)
    planned_settle = _planned_settle_evidence(request)
    if not request.include_lock_evidence_plan:
        blocked.append("lock evidence plan missing")
    if not request.include_settle_evidence_plan:
        blocked.append("settle evidence plan missing")

    forbidden_paths = _forbidden_paths(root)
    package = {
        "schema_version": SCHEMA_VERSION,
        "package_id": package_id,
        "created_at": _utc_now(),
        "package_kind": PACKAGE_KIND,
        "backend_kind": BACKEND_KIND,
        "target_worker_id": request.worker_id,
        "root": str(root),
        "execution_mode": "no-live",
        "requested_future_execution": bool(request.requested_future_execution),
        "current_readiness_summary": current_status,
        "current_blockers": current_blockers,
        "planned_smokes": planned_smokes,
        "planned_lock_evidence": planned_lock,
        "planned_settle_evidence": planned_settle,
        "allowed_write_roots": [str(allowed_root)],
        "forbidden_paths": forbidden_paths,
        "required_preflight": _required_preflight(request.worker_id),
        "required_dispatch_surface": _required_dispatch_surface(),
        "required_terminal_protocol": _required_terminal_protocol(),
        "required_post_execution_validation": _required_post_execution_validation(request.worker_id, allowed_root),
        "required_settle_commands": _required_settle_commands(request.worker_id),
        "would_execute_live": False,
        "would_touch_worker": False,
        "would_write_registry": False,
        "allow_business_repo_writes": False,
        "business_ready": False,
        "live_execution_requested": request.live_execution_requested,
        "business_write_requested": request.business_write_requested,
        "registry_write_requested": request.registry_write_requested,
        "state_team_write_requested": request.state_team_write_requested,
        "direct_tmux_requested": request.direct_tmux_requested,
        "direct_tui_requested": request.direct_tui_requested,
        "direct_adapter_submit_requested": request.direct_adapter_submit_requested,
        "cursor_requested": request.cursor_requested,
        "claude_pane_touch_requested": request.claude_pane_touch_requested,
        "rmux_live_requested": request.rmux_live_requested,
        "blocked_reasons": _dedupe(blocked),
        "warnings": _dedupe(warnings),
        "safe_next_actions": [
            "review this package with the user before any live evidence smoke",
            "request separate explicit future live authorization before execution",
            "execute only through controlled visible dispatch / controlled evidence wrapper if later authorized",
            "validate evidence bundle before any separate registry update path",
        ],
        "forbidden_next_actions": _forbidden_next_actions(),
        "package_path": "",
        "evidence": evidence,
    }
    validation = validate_opencode_evidence_authorization_package(package)
    if validation.errors:
        package["blocked_reasons"] = _dedupe(_list_str(package["blocked_reasons"]) + validation.errors)
    result = _result_from_package(package, package_path="")
    package["result"] = result.to_dict()
    package["validation"] = validation.to_dict()
    return package


def _planned_smokes(execution_plan: dict[str, Any], request: OpenCodeEvidenceAuthorizationRequest) -> list[dict[str, Any]]:
    smoke_tasks = [_dict(item) for item in _list(execution_plan.get("smoke_tasks"))]
    include = {
        "done": request.include_done_smoke_plan,
        "blocked": request.include_blocked_smoke_plan,
        "fail": request.include_fail_smoke_plan,
    }
    names = {"done": "done_smoke", "blocked": "blocked_smoke", "fail": "fail_smoke"}
    planned = []
    for task in smoke_tasks:
        kind = str(task.get("smoke_kind", ""))
        if kind not in include or not include[kind]:
            continue
        planned.append(
            {
                **task,
                "smoke_kind": names[kind],
                "requires_non_empty_report_md": True,
                "expected_terminal_marker": task.get("expected_marker", ""),
            }
        )
    return planned


def _planned_lock_evidence(request: OpenCodeEvidenceAuthorizationRequest) -> dict[str, Any]:
    if not request.include_lock_evidence_plan:
        return {}
    return {
        "worker_id": request.worker_id,
        "active_nonterminal_lock_blocks_new_task": True,
        "terminal_stale_lock_settle_requires_terminal_truth": True,
        "one_worker_one_active_task": True,
        "owner_lock_references_worker_run_task": True,
    }


def _planned_settle_evidence(request: OpenCodeEvidenceAuthorizationRequest) -> dict[str, Any]:
    if not request.include_settle_evidence_plan:
        return {}
    return {
        "worker_id": request.worker_id,
        "settle_latest_available": True,
        "pending_after_timeout_supported": True,
        "terminal_after_timeout_can_be_settled": True,
        "nonterminal_after_timeout_remains_blocking": True,
        "no_redispatch_on_pending": True,
    }


def _required_dispatch_surface() -> dict[str, Any]:
    return {
        "allowed_future_entrypoint": "controlled visible dispatch / controlled evidence wrapper only",
        "forbidden": [
            "raw tmux input",
            "direct /tui/*",
            "direct curl /tui/*",
            "direct adapter submit",
            "rmux live",
            "Cursor",
            "Claude pane touch",
        ],
        "run_one_allowed_only_if_future_wrapper_scopes_worker": True,
        "direct_tmux_requested": False,
        "direct_tui_requested": False,
        "direct_adapter_submit_requested": False,
    }


def _required_terminal_protocol() -> dict[str, Any]:
    return {
        "report_md_nonempty": True,
        "terminal_marker_count": "exactly_one",
        "done_smoke_expected_marker": ".done",
        "blocked_smoke_expected_marker": ".blocked",
        "fail_smoke_expected_marker": ".fail",
    }


def _required_preflight(worker_id: str) -> list[str]:
    return [
        f"confirm worker {worker_id} binding with read-only health",
        "confirm active_locks=0",
        "confirm active_alerts=0",
        "confirm registered worker registry valid",
        "confirm board validate Errors none",
        "confirm user explicitly authorizes future live evidence smoke execution",
    ]


def _required_post_execution_validation(worker_id: str, allowed_root: Path) -> dict[str, Any]:
    return {
        "worker_id": worker_id,
        "evidence_root": str(allowed_root),
        "validator": "validate-opencode-mixed-readiness-evidence",
        "registry_update_after_validation": "separate explicit path only",
    }


def _required_settle_commands(worker_id: str) -> list[str]:
    return [
        f"controlled settle-latest for {worker_id} after terminal truth only",
        "do not redispatch pending tasks",
        "nonterminal timeout remains blocking",
    ]


def _forbidden_paths(root: Path) -> list[str]:
    return [
        str(workspace_root()),
        str(root / STATE_TEAM_REL),
        str(root / REGISTERED_REGISTRY_REL),
        str(root / OPENCODE_REGISTRY_REL),
    ]


def _forbidden_next_actions() -> list[str]:
    return [
        "execute OpenCode live now",
        "invoke Claude or claude-code",
        "touch Claude pane",
        "send tmux input",
        "call direct /tui endpoints",
        "direct adapter submit",
        "write OpenCode visible registry",
        "write registered worker registry",
        "write .omx/state/team",
        "write business repo",
        "mark mixed_ready=true",
        "run mixed live",
        "run cursor-agent",
        "run rmux live",
        "write Obsidian",
    ]


def _result_from_package(package: dict[str, Any], *, package_path: str) -> OpenCodeEvidenceAuthorizationBuildResult:
    return OpenCodeEvidenceAuthorizationBuildResult(
        ok=not bool(package.get("blocked_reasons")),
        package_id=str(package.get("package_id", "")),
        target_worker_id=str(package.get("target_worker_id", "")),
        backend_kind=str(package.get("backend_kind", "")),
        would_execute_live=False,
        would_touch_worker=False,
        would_write_registry=False,
        planned_smoke_count=len(_list(package.get("planned_smokes"))),
        allowed_write_roots=_list_str(package.get("allowed_write_roots")),
        forbidden_paths=_list_str(package.get("forbidden_paths")),
        current_blockers=_list_str(package.get("current_blockers")),
        blocked_reasons=_list_str(package.get("blocked_reasons")),
        warnings=_list_str(package.get("warnings")),
        safe_next_actions=_list_str(package.get("safe_next_actions")),
        forbidden_next_actions=_list_str(package.get("forbidden_next_actions")),
        package_path=package_path or str(package.get("package_path", "")),
        evidence=_list(package.get("evidence")),
    )


def _status_evidence_summary(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(status.get("ok")),
        "worker_id": status.get("worker_id", ""),
        "worker_present": bool(status.get("worker_present", False)),
        "mixed_ready": bool(status.get("mixed_ready", False)),
        "evidence_collected": bool(status.get("evidence_collected", False)),
        "current_blockers": _list_str(status.get("current_blockers", status.get("blockers", []))),
    }


def _execution_plan_evidence_summary(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_plan_id": plan.get("execution_plan_id", ""),
        "would_execute_live": bool(plan.get("would_execute_live", False)),
        "would_write_registry": bool(plan.get("would_write_registry", False)),
        "would_touch_worker": bool(plan.get("would_touch_worker", False)),
        "smoke_task_count": len(_list(plan.get("smoke_tasks"))),
    }


def _package_markdown(package: dict[str, Any]) -> str:
    lines = [
        f"# {package.get('package_id', 'OpenCode evidence authorization package')}",
        "",
        f"- package_kind: {package.get('package_kind')}",
        f"- target_worker_id: {package.get('target_worker_id')}",
        f"- execution_mode: {package.get('execution_mode')}",
        f"- would_execute_live: {str(bool(package.get('would_execute_live'))).lower()}",
        f"- would_touch_worker: {str(bool(package.get('would_touch_worker'))).lower()}",
        f"- would_write_registry: {str(bool(package.get('would_write_registry'))).lower()}",
        "",
        "## Planned Evidence",
    ]
    for smoke in _list(package.get("planned_smokes")):
        item = _dict(smoke)
        lines.append(f"- {item.get('smoke_kind')}: {item.get('expected_terminal_marker')}")
    lines.extend(
        [
            "- lock_evidence: planned",
            "- settle_evidence: planned",
            "",
            "## Forbidden",
        ]
    )
    for action in _list_str(package.get("forbidden_next_actions")):
        lines.append(f"- {action}")
    return "\n".join(lines) + "\n"


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


def _list_str(value: Any) -> list[str]:
    return [str(item) for item in _list(value)]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _utc_now(*, compact: bool = False) -> str:
    text = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if compact:
        return text.replace("-", "").replace(":", "").replace("+00:00", "Z")
    return text
