"""Source-only OpenCode mixed-readiness evidence execution planning.

This module designs future evidence smoke execution. It never dispatches a
worker, calls tmux, calls OpenCode or Claude, writes registries, writes
.omx/state/team, or promotes business or mixed readiness.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime_paths import workspace_root

from .registered_registry import validate_registered_worker_registry_file


SCHEMA_VERSION = 1
PLAN_KIND = "opencode-mixed-readiness-evidence-execution-plan"
BACKEND_KIND = "opencode_tui"
EVIDENCE_ROOT_REL = ".omx/framework/team/opencode-mixed-readiness-smokes"
TERMINAL_MARKERS = [".done", ".blocked", ".fail"]
SMOKE_KINDS = ["done", "blocked", "fail", "lock_evidence", "settle_evidence"]


@dataclass(frozen=True)
class OpenCodeEvidenceExecutionPlanRequest:
    root: str
    worker_id: str
    opencode_visible_registry: str
    registered_worker_registry: str
    execution_mode: str = "no-live"
    evidence_root: str = ""
    run_root: str = ""
    business_write_requested: bool = False
    registry_write_requested: bool = False
    state_team_write_requested: bool = False
    direct_tmux_requested: bool = False
    direct_tui_requested: bool = False
    direct_adapter_submit_requested: bool = False
    live_execution_requested: bool = False
    cursor_requested: bool = False

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        fixture_dir: Path | None = None,
    ) -> "OpenCodeEvidenceExecutionPlanRequest":
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
            evidence_root=str(_resolve_optional(payload.get("evidence_root", ""), base)) if payload.get("evidence_root") else "",
            run_root=str(_resolve_optional(payload.get("run_root", ""), base)) if payload.get("run_root") else "",
            business_write_requested=bool(payload.get("business_write_requested", payload.get("business_repo_writes_requested", False))),
            registry_write_requested=bool(payload.get("registry_write_requested", payload.get("registry_update_requested", False))),
            state_team_write_requested=bool(payload.get("state_team_write_requested", False)),
            direct_tmux_requested=bool(payload.get("direct_tmux_requested", False)),
            direct_tui_requested=bool(payload.get("direct_tui_requested", False)),
            direct_adapter_submit_requested=bool(payload.get("direct_adapter_submit_requested", False)),
            live_execution_requested=bool(payload.get("live_execution_requested", False)),
            cursor_requested=bool(payload.get("cursor_requested", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceSmokeTaskPlan:
    smoke_kind: str
    task_id: str
    title: str
    prompt_template: str
    expected_report_path: str
    expected_marker: str
    expected_artifacts: list[str]
    allowed_write_root: str
    forbidden_paths: list[str]
    expected_validation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceExecutionPlanResult:
    ok: bool
    execution_plan_id: str
    worker_id: str
    backend_kind: str
    execution_mode: str
    would_execute_live: bool
    would_write_registry: bool
    would_touch_worker: bool
    evidence_root: str
    smoke_task_count: int
    smoke_tasks: list[dict[str, Any]]
    allowed_write_roots: list[str]
    forbidden_paths: list[str]
    dispatch_surface: dict[str, Any]
    required_wrappers: list[str]
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceExecutionBundleValidation:
    ok: bool
    evidence_bundle_present: bool
    done_smoke_planned: bool
    blocked_smoke_planned: bool
    fail_smoke_planned: bool
    lock_evidence_planned: bool
    settle_evidence_planned: bool
    business_writes_forbidden: bool
    registry_writes_forbidden: bool
    state_team_writes_forbidden: bool
    would_execute_live: bool
    would_write_registry: bool
    would_touch_worker: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_opencode_mixed_readiness_evidence_execution_plan_from_files(
    *,
    root: Path | str,
    worker_id: str,
    opencode_visible_registry: Path | str,
    registered_worker_registry: Path | str,
    execution_mode: str = "no-live",
) -> dict[str, Any]:
    request = OpenCodeEvidenceExecutionPlanRequest(
        root=str(Path(root).resolve()),
        worker_id=worker_id,
        opencode_visible_registry=str(Path(opencode_visible_registry).resolve()),
        registered_worker_registry=str(Path(registered_worker_registry).resolve()),
        execution_mode=execution_mode,
    )
    return build_opencode_mixed_readiness_evidence_execution_plan(request)


def build_opencode_mixed_readiness_evidence_execution_fixture_plan(fixture_dir: Path | str) -> dict[str, Any]:
    fixture = Path(fixture_dir).resolve()
    request = OpenCodeEvidenceExecutionPlanRequest.from_dict(_read_json(fixture / "request.json"), fixture_dir=fixture)
    return build_opencode_mixed_readiness_evidence_execution_plan(request)


def validate_opencode_mixed_readiness_evidence_execution_plan_file(plan_path: Path | str) -> OpenCodeEvidenceExecutionBundleValidation:
    plan_path = Path(plan_path).resolve()
    payload = _read_json(plan_path)
    return validate_opencode_mixed_readiness_evidence_execution_plan(payload, plan_path=plan_path)


def build_opencode_mixed_readiness_evidence_execution_plan(
    request: OpenCodeEvidenceExecutionPlanRequest,
) -> dict[str, Any]:
    root = Path(request.root).resolve() if request.root else Path(".").resolve()
    evidence_root = Path(request.evidence_root).resolve() if request.evidence_root else root / EVIDENCE_ROOT_REL / request.worker_id
    run_root = Path(request.run_root).resolve() if request.run_root else evidence_root / "future-run"
    blocked: list[str] = []
    warnings = [
        "Evidence execution wrapper plan is source-only/no-live in this phase",
        "Evidence execution produces evidence bundle only; registry update remains separate",
        "Mixed live remains forbidden and business tasks remain forbidden",
    ]
    evidence: list[dict[str, Any]] = [{"source": "plan_request", "detail": request.to_dict()}]

    worker_record = _find_opencode_worker(request, blocked, evidence)
    if worker_record and str(worker_record.get("backend", worker_record.get("backend_kind", ""))) != BACKEND_KIND:
        blocked.append(f"backend_kind must be {BACKEND_KIND}")

    try:
        registry_result = validate_registered_worker_registry_file(request.registered_worker_registry)
        if not registry_result.ok:
            reason = "; ".join(registry_result.blocked_reasons)
            warnings.append(f"pre-live blocker: registered worker registry invalid: {reason}")
            evidence.append({"source": "registered_worker_registry_validation", "detail": registry_result.to_dict()})
    except Exception as exc:
        warnings.append(f"pre-live blocker: registered worker registry invalid: {exc}")
        evidence.append({"source": "registered_worker_registry_validation", "detail": {"error": str(exc)}})

    if request.execution_mode != "no-live":
        blocked.append("live execution requested but disabled in source-only phase")
    if request.live_execution_requested:
        blocked.append("live execution requested but disabled in source-only phase")
    if request.business_write_requested:
        blocked.append("business repo write requested")
    if request.registry_write_requested:
        blocked.append("registry write requested")
    if request.state_team_write_requested:
        blocked.append(".omx/state/team write requested")
    if request.direct_tmux_requested:
        blocked.append("direct tmux input requested")
    if request.direct_tui_requested:
        blocked.append("direct /tui request requested")
    if request.direct_adapter_submit_requested:
        blocked.append("direct adapter submit requested")
    if request.cursor_requested:
        blocked.append("Cursor requested but disabled / out of scope")

    allowed_write_roots = [str(evidence_root)]
    forbidden_paths = _forbidden_paths(root)
    smoke_tasks = [_task_plan(kind, request.worker_id, run_root, evidence_root, forbidden_paths).to_dict() for kind in SMOKE_KINDS]
    dispatch_surface = _dispatch_surface()
    required_wrappers = ["controlled visible dispatch", "controlled evidence execution wrapper"]

    result = OpenCodeEvidenceExecutionPlanResult(
        ok=not blocked,
        execution_plan_id=f"opencode-evidence-execution-{request.worker_id}",
        worker_id=request.worker_id,
        backend_kind=BACKEND_KIND,
        execution_mode="no-live",
        would_execute_live=False,
        would_write_registry=False,
        would_touch_worker=False,
        evidence_root=str(evidence_root),
        smoke_task_count=len(smoke_tasks),
        smoke_tasks=smoke_tasks,
        allowed_write_roots=allowed_write_roots,
        forbidden_paths=forbidden_paths,
        dispatch_surface=dispatch_surface,
        required_wrappers=required_wrappers,
        blocked_reasons=_dedupe(blocked),
        warnings=_dedupe(warnings),
        safe_next_actions=[
            "install OpenCode evidence execution wrapper CLI into the selected runtime root only after separate install approval",
            "run fixture-only smoke for the wrapper before any live evidence collection",
            "validate produced evidence with validate-opencode-mixed-readiness-evidence before any separate registry update",
        ],
        forbidden_next_actions=_forbidden_next_actions(),
        evidence=evidence,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "execution_plan_id": result.execution_plan_id,
        "created_at": _utc_now(),
        "plan_kind": PLAN_KIND,
        "backend_kind": BACKEND_KIND,
        "worker_id": request.worker_id,
        "root": str(root),
        "execution_mode": "no-live",
        "opencode_visible_registry": request.opencode_visible_registry,
        "registered_worker_registry": request.registered_worker_registry,
        "evidence_root": str(evidence_root),
        "run_root": str(run_root),
        "smoke_tasks": smoke_tasks,
        "done_smoke_task": smoke_tasks[0],
        "blocked_smoke_task": smoke_tasks[1],
        "fail_smoke_task": smoke_tasks[2],
        "lock_evidence_task": smoke_tasks[3],
        "settle_evidence_task": smoke_tasks[4],
        "allowed_write_roots": allowed_write_roots,
        "forbidden_paths": forbidden_paths,
        "dispatch_surface": dispatch_surface,
        "required_wrappers": required_wrappers,
        "terminal_protocol": _terminal_protocol(),
        "lock_policy": _lock_policy(request.worker_id),
        "settle_policy": _settle_policy(request.worker_id),
        "expected_evidence_bundle": _expected_evidence_bundle(run_root),
        "would_execute_live": False,
        "would_write_registry": False,
        "would_touch_worker": False,
        "blocked_reasons": result.blocked_reasons,
        "warnings": result.warnings,
        "safe_next_actions": result.safe_next_actions,
        "forbidden_next_actions": result.forbidden_next_actions,
        "evidence": result.evidence,
        "result": result.to_dict(),
    }
    validation = validate_opencode_mixed_readiness_evidence_execution_plan(payload)
    if validation.blocked_reasons:
        payload["blocked_reasons"] = _dedupe(payload["blocked_reasons"] + validation.blocked_reasons)
        payload["result"]["blocked_reasons"] = payload["blocked_reasons"]
        payload["result"]["ok"] = False
    return payload


def validate_opencode_mixed_readiness_evidence_execution_plan(
    payload: dict[str, Any],
    *,
    plan_path: Path | None = None,
) -> OpenCodeEvidenceExecutionBundleValidation:
    blocked: list[str] = []
    warnings = [
        "Evidence execution plan validation is source-only and no-live",
        "Validation does not execute OpenCode, tmux, Claude, registry writes, or business writes",
    ]
    if "registered worker registry invalid" in " ".join(_list(payload.get("warnings", []))):
        warnings.append("pre-live blocker surfaced: registered worker registry invalid")

    smoke_tasks = [_dict(item) for item in _list(payload.get("smoke_tasks", []))]
    kinds = {str(item.get("smoke_kind", "")) for item in smoke_tasks}
    done = "done" in kinds
    blocked_smoke = "blocked" in kinds
    fail = "fail" in kinds
    lock = "lock_evidence" in kinds
    settle = "settle_evidence" in kinds
    if not done:
        blocked.append("done smoke task missing")
    if not blocked_smoke:
        blocked.append("blocked smoke task missing")
    if not fail:
        blocked.append("fail smoke task missing")
    if not lock:
        blocked.append("lock evidence task missing")
    if not settle:
        blocked.append("settle evidence task missing")

    root = str(payload.get("root", ""))
    allowed_write_roots = [str(item) for item in _list(payload.get("allowed_write_roots", []))]
    if not allowed_write_roots:
        blocked.append("allowed write root missing")
    for path in allowed_write_roots:
        if EVIDENCE_ROOT_REL not in path:
            blocked.append("write scope outside opencode-mixed-readiness-smokes")
        if root and not path.startswith(root):
            blocked.append("write scope outside root")

    if bool(payload.get("business_write_requested", payload.get("business_repo_writes_requested", False))):
        blocked.append("business repo write requested")
    if bool(payload.get("registry_write_requested", payload.get("registry_update_requested", False))):
        blocked.append("registry write requested")
    if bool(payload.get("state_team_write_requested", False)):
        blocked.append(".omx/state/team write requested")
    if bool(payload.get("cursor_requested", False)):
        blocked.append("Cursor requested but disabled / out of scope")
    if bool(payload.get("live_execution_requested", False)) or str(payload.get("execution_mode", "no-live")) != "no-live":
        blocked.append("live execution requested but disabled in source-only phase")

    dispatch = _dict(payload.get("dispatch_surface", {}))
    if bool(dispatch.get("direct_tmux_requested", payload.get("direct_tmux_requested", False))):
        blocked.append("direct tmux input requested")
    if bool(dispatch.get("direct_tui_requested", payload.get("direct_tui_requested", False))):
        blocked.append("direct /tui request requested")
    if bool(dispatch.get("direct_adapter_submit_requested", payload.get("direct_adapter_submit_requested", False))):
        blocked.append("direct adapter submit requested")
    forbidden = " ".join(str(item) for item in _list(dispatch.get("forbidden", [])))
    for phrase in ("raw tmux input", "direct /tui", "direct adapter submit", "run-one", "rmux live"):
        if phrase not in forbidden:
            blocked.append(f"dispatch forbidden surface missing: {phrase}")

    if bool(payload.get("would_execute_live", False)):
        blocked.append("would_execute_live must be false")
    if bool(payload.get("would_write_registry", False)):
        blocked.append("would_write_registry must be false")
    if bool(payload.get("would_touch_worker", False)):
        blocked.append("would_touch_worker must be false")

    forbidden_paths = [str(item) for item in _list(payload.get("forbidden_paths", []))]
    business_forbidden = str(workspace_root()) in forbidden_paths
    registry_forbidden = (
        any(path.endswith("/registered-workers/registered-workers.json") for path in forbidden_paths)
        and any(path.endswith("/opencode_visible_workers.json") for path in forbidden_paths)
    )
    state_forbidden = any(path.endswith("/.omx/state/team") for path in forbidden_paths)
    if not business_forbidden:
        blocked.append("business repo forbidden path missing")
    if not registry_forbidden:
        blocked.append("registry forbidden path missing")
    if not state_forbidden:
        blocked.append(".omx/state/team forbidden path missing")

    ok = not blocked
    return OpenCodeEvidenceExecutionBundleValidation(
        ok=ok,
        evidence_bundle_present=bool(payload.get("expected_evidence_bundle")),
        done_smoke_planned=done,
        blocked_smoke_planned=blocked_smoke,
        fail_smoke_planned=fail,
        lock_evidence_planned=lock,
        settle_evidence_planned=settle,
        business_writes_forbidden=business_forbidden,
        registry_writes_forbidden=registry_forbidden,
        state_team_writes_forbidden=state_forbidden,
        would_execute_live=False,
        would_write_registry=False,
        would_touch_worker=False,
        blocked_reasons=_dedupe(blocked),
        warnings=_dedupe(warnings),
        evidence=[
            {"source": "plan_file", "detail": {"path": str(plan_path or "")}},
            {"source": "smoke_task_kinds", "detail": {"kinds": sorted(kinds)}},
            {"source": "dispatch_surface", "detail": dispatch},
        ],
    )


def _find_opencode_worker(
    request: OpenCodeEvidenceExecutionPlanRequest,
    blocked: list[str],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        payload = _read_json(Path(request.opencode_visible_registry))
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        blocked.append(f"OpenCode visible registry unreadable: {exc}")
        return {}
    workers = [_dict(item) for item in _list(payload.get("workers", []))]
    worker = next((item for item in workers if str(item.get("worker_id", "")) == request.worker_id), {})
    evidence.append({"source": "opencode_visible_registry", "detail": {"path": request.opencode_visible_registry, "worker_id": request.worker_id, "present": bool(worker)}})
    if not worker:
        blocked.append(f"OpenCode worker missing from registry: {request.worker_id}")
    return worker


def _task_plan(kind: str, worker_id: str, run_root: Path, evidence_root: Path, forbidden_paths: list[str]) -> OpenCodeEvidenceSmokeTaskPlan:
    task_dir = run_root / kind / "task-001"
    markers = {"done": ".done", "blocked": ".blocked", "fail": ".fail", "lock_evidence": "", "settle_evidence": ""}
    titles = {
        "done": "OpenCode evidence done terminal smoke",
        "blocked": "OpenCode evidence blocked terminal smoke",
        "fail": "OpenCode evidence fail terminal smoke",
        "lock_evidence": "OpenCode worker-scoped lock evidence smoke",
        "settle_evidence": "OpenCode worker-scoped settle evidence smoke",
    }
    artifacts = [str(task_dir / "report.md")]
    if markers[kind]:
        artifacts.append(str(task_dir / markers[kind]))
    elif kind == "lock_evidence":
        artifacts.append(str(evidence_root / "lock-evidence.json"))
    else:
        artifacts.append(str(evidence_root / "settle-evidence.json"))
    return OpenCodeEvidenceSmokeTaskPlan(
        smoke_kind=kind,
        task_id=f"{worker_id}-{kind}-task-001",
        title=titles[kind],
        prompt_template=f"future source-only evidence smoke for {worker_id}: {kind}",
        expected_report_path=str(task_dir / "report.md"),
        expected_marker=markers[kind],
        expected_artifacts=artifacts,
        allowed_write_root=str(evidence_root),
        forbidden_paths=forbidden_paths,
        expected_validation={
            "report_md_nonempty": True,
            "terminal_marker_count": "exactly_one" if markers[kind] else "not_applicable",
            "worker_scoped": kind in {"lock_evidence", "settle_evidence"},
        },
    )


def _dispatch_surface() -> dict[str, Any]:
    return {
        "allowed_future_entrypoint": "visible dispatch / controlled wrapper only",
        "forbidden": [
            "raw tmux input",
            "direct /tui/*",
            "direct adapter submit",
            "run-one from this source-only planner",
            "omx_multiworker_smoke unless future wrapper explicitly uses it",
            "rmux live",
        ],
        "direct_tmux_requested": False,
        "direct_tui_requested": False,
        "direct_adapter_submit_requested": False,
    }


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


def _lock_policy(worker_id: str) -> dict[str, Any]:
    return {
        "worker_id": worker_id,
        "one_worker_one_active_task": True,
        "lock_must_reference_run_and_task": True,
        "nonterminal_lock_blocks_new_task": True,
        "terminal_lock_can_be_archived": True,
        "lock_archive_requires_terminal_truth": True,
    }


def _settle_policy(worker_id: str) -> dict[str, Any]:
    return {
        "worker_id": worker_id,
        "settle_latest_required": True,
        "post_timeout_pending_supported": True,
        "terminal_after_timeout_must_settle": True,
        "nonterminal_after_timeout_must_block": True,
        "no_redispatch_on_pending": True,
    }


def _expected_evidence_bundle(run_root: Path) -> dict[str, Any]:
    return {
        "run_root": str(run_root),
        "required_terminal_smokes": ["done", "blocked", "fail"],
        "required_worker_scoped_evidence": ["lock_evidence", "settle_evidence"],
        "validator_after_future_execution": "validate-opencode-mixed-readiness-evidence",
    }


def _forbidden_paths(root: Path) -> list[str]:
    return [
        str(workspace_root()),
        str(root / ".omx" / "state" / "team"),
        str(root / ".omx" / "framework" / "team" / "registered-workers" / "registered-workers.json"),
        str(root / ".omx" / "framework" / "team" / "opencode_visible_workers.json"),
    ]


def _forbidden_next_actions() -> list[str]:
    return [
        "execute OpenCode live",
        "invoke Claude or claude-code",
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
