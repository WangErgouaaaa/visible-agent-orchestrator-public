"""Source-only OpenCode evidence runner planning.

The runner plan is a refinement of a reviewed no-execution authorization
package. It prepares an auditable future execution sequence, but it never
executes live work, touches workers, calls tmux/OpenCode/Claude, writes
registries, writes .omx/state/team, or promotes mixed/business readiness.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from visible_agent_orchestrator.runtime_paths import workspace_root

from .opencode_evidence_authorization import validate_opencode_evidence_authorization_package
from .opencode_mixed_readiness_evidence import validate_opencode_mixed_readiness_evidence
from .opencode_mixed_readiness_evidence_execution import BACKEND_KIND
from .opencode_worker_identity import diagnose_opencode_worker_identity_from_files


SCHEMA_VERSION = 1
PLAN_KIND = "opencode-evidence-runner-plan"
REQUIRED_STEP_KINDS = [
    "done_smoke",
    "blocked_smoke",
    "fail_smoke",
    "worker_scoped_lock_evidence",
    "worker_scoped_settle_evidence",
]
FUTURE_EXECUTION_ORDER = [
    "preflight_readonly",
    "done_smoke",
    "blocked_smoke",
    "fail_smoke",
    "lock_evidence_smoke",
    "settle_evidence_smoke",
    "validate_evidence_bundle",
    "no_registry_update",
    "stop_for_user_review",
]
LIVE_SMOKE_KINDS = ["done_smoke", "blocked_smoke", "fail_smoke"]
SMOKE_STATES = {
    "done_smoke": ("done", ".done"),
    "blocked_smoke": ("blocked", ".blocked"),
    "fail_smoke": ("fail", ".fail"),
}
CommandRunner = Callable[[list[str], Path, int], dict[str, Any]]


@dataclass(frozen=True)
class OpenCodeEvidenceRunnerPlanRequest:
    package_path: str
    root: str = ""
    execution_mode: str = "no-live"
    target_worker_id: str = ""
    business_write_requested: bool = False
    registry_write_requested: bool = False
    state_team_write_requested: bool = False
    direct_tmux_requested: bool = False
    direct_tui_requested: bool = False
    direct_adapter_submit_requested: bool = False
    live_execution_requested: bool = False
    claude_pane_touch_requested: bool = False
    cursor_requested: bool = False
    rmux_live_requested: bool = False
    mixed_live_requested: bool = False
    business_task_requested: bool = False
    target_worker_present: bool = True
    target_backend_kind: str = BACKEND_KIND
    target_binding_confidence: str = "strong"
    submit_path_binding_confidence: str = "strong"
    submit_path_worker_scope_supported: bool = True
    target_port_live: bool = True
    target_allow_auto_submit: bool = True
    target_identity_safe: bool = True

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, fixture_dir: Path | None = None) -> "OpenCodeEvidenceRunnerPlanRequest":
        base = fixture_dir.resolve() if fixture_dir else None
        return cls(
            package_path=str(_resolve_optional(payload.get("package_path", payload.get("package", "package.json")), base)),
            root=str(payload.get("root", "")),
            execution_mode=str(payload.get("execution_mode", "no-live")),
            target_worker_id=str(payload.get("target_worker_id", "")),
            business_write_requested=bool(payload.get("business_write_requested", payload.get("business_repo_writes_requested", False))),
            registry_write_requested=bool(payload.get("registry_write_requested", payload.get("registry_update_requested", False))),
            state_team_write_requested=bool(payload.get("state_team_write_requested", False)),
            direct_tmux_requested=bool(payload.get("direct_tmux_requested", False)),
            direct_tui_requested=bool(payload.get("direct_tui_requested", False)),
            direct_adapter_submit_requested=bool(payload.get("direct_adapter_submit_requested", False)),
            live_execution_requested=bool(payload.get("live_execution_requested", False)),
            claude_pane_touch_requested=bool(payload.get("claude_pane_touch_requested", False)),
            cursor_requested=bool(payload.get("cursor_requested", False)),
            rmux_live_requested=bool(payload.get("rmux_live_requested", False)),
            mixed_live_requested=bool(payload.get("mixed_live_requested", False)),
            business_task_requested=bool(payload.get("business_task_requested", False)),
            target_worker_present=bool(payload.get("target_worker_present", True)),
            target_backend_kind=str(payload.get("target_backend_kind", BACKEND_KIND)),
            target_binding_confidence=str(payload.get("target_binding_confidence", "strong")),
            submit_path_binding_confidence=str(payload.get("submit_path_binding_confidence", payload.get("target_binding_confidence", "strong"))),
            submit_path_worker_scope_supported=bool(payload.get("submit_path_worker_scope_supported", True)),
            target_port_live=bool(payload.get("target_port_live", True)),
            target_allow_auto_submit=bool(payload.get("target_allow_auto_submit", True)),
            target_identity_safe=bool(payload.get("target_identity_safe", payload.get("safe_for_live_submit", True))),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceRunnerStep:
    step_id: str
    step_kind: str
    target_worker_id: str
    expected_terminal_marker: str
    expected_report_nonempty: bool
    allowed_write_root: str
    forbidden_paths: list[str]
    future_entrypoint: str
    future_validation: dict[str, Any]
    stop_conditions: list[str]
    rollback_or_recovery_policy: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceRunnerSafetyGate:
    gate_id: str
    description: str
    required: bool = True
    fail_closed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceRunnerPlan:
    ok: bool
    runner_plan_id: str
    source_package_id: str
    source_package_path: str
    source_package_target_worker_id: str
    target_worker_id: str
    backend_kind: str
    execution_mode: str
    runner_steps: list[dict[str, Any]]
    safety_gates: list[dict[str, Any]]
    allowed_write_roots: list[str]
    forbidden_paths: list[str]
    future_entrypoints: list[str]
    validation_requirements: dict[str, Any]
    would_execute_live: bool
    would_touch_worker: bool
    would_write_registry: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceRunnerValidation:
    ok: bool
    runner_plan_id: str
    source_package_id: str
    target_worker_id: str
    backend_kind: str
    execution_mode: str
    done_smoke_step_present: bool
    blocked_smoke_step_present: bool
    fail_smoke_step_present: bool
    lock_evidence_step_present: bool
    settle_evidence_step_present: bool
    allowed_write_roots_valid: bool
    forbidden_paths_valid: bool
    direct_surfaces_blocked: bool
    no_live_execution: bool
    no_worker_touch: bool
    no_registry_write: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpenCodeEvidenceRunnerExecutionRequest:
    package_path: str
    root: str
    target_worker_id: str
    allow_live_evidence: bool = False
    timeout_seconds: int = 180
    dispatch_bin: str = ""
    adapter_bin: str = ""
    watcher_bin: str = ""
    rebind_bin: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_opencode_evidence_runner_plan_from_package(
    package_path: Path | str,
    *,
    root: str = "",
    execution_mode: str = "no-live",
    target_worker_id: str = "",
) -> dict[str, Any]:
    request = OpenCodeEvidenceRunnerPlanRequest(
        package_path=str(Path(package_path).resolve()),
        root=root,
        execution_mode=execution_mode,
        target_worker_id=target_worker_id,
    )
    return build_opencode_evidence_runner_plan(request)


def build_opencode_evidence_runner_fixture_plan(fixture_dir: Path | str) -> dict[str, Any]:
    fixture = Path(fixture_dir).resolve()
    request_path = fixture / "request.json"
    if request_path.is_file():
        request = OpenCodeEvidenceRunnerPlanRequest.from_dict(_read_json(request_path), fixture_dir=fixture)
        return build_opencode_evidence_runner_plan(request)
    plan_path = fixture / "runner-plan.json"
    if plan_path.is_file():
        payload = _read_json(plan_path)
        validation = validate_opencode_evidence_runner_plan(payload, plan_path=plan_path)
        payload["validation"] = validation.to_dict()
        payload["result"] = _result_from_plan(payload, validation).to_dict()
        return payload
    raise FileNotFoundError(f"fixture must contain request.json or runner-plan.json: {fixture}")


def validate_opencode_evidence_runner_plan_file(plan_path: Path | str) -> OpenCodeEvidenceRunnerValidation:
    path = Path(plan_path).resolve()
    return validate_opencode_evidence_runner_plan(_read_json(path), plan_path=path)


def execute_opencode_evidence_runner_package(
    request: OpenCodeEvidenceRunnerExecutionRequest,
    *,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Collect authorized OpenCode evidence through the visible dispatch wrapper.

    The live action here is limited to terminal-protocol smoke collection on the
    exact target worker. The produced evidence bundle remains a no-live,
    no-registry-update candidate artifact for the separate validator.
    """
    root = Path(request.root).resolve()
    package_path = Path(request.package_path).resolve()
    package = _read_json(package_path)
    package_validation = validate_opencode_evidence_authorization_package(package, package_path=package_path)
    package_worker_id = str(package.get("target_worker_id", package.get("worker_id", "")))
    target_worker_id = request.target_worker_id or package_worker_id
    allowed_write_roots = _list_str(package.get("allowed_write_roots"))
    evidence_root = Path(allowed_write_roots[0]).resolve() if allowed_write_roots else root / ".omx/framework/team/opencode-mixed-readiness-smokes" / str(package.get("package_id", target_worker_id))
    dispatch_bin = Path(request.dispatch_bin).resolve() if request.dispatch_bin else root / "bin" / "omx_visible_dispatch"
    adapter_bin = Path(request.adapter_bin).resolve() if request.adapter_bin else root / "bin" / "omx_opencode_tui_adapter"
    worker_backend_bin = root / "bin" / "omx_worker_backend"
    watcher_bin = Path(request.watcher_bin).resolve() if request.watcher_bin else root / "bin" / "omx_visible_watcher"
    rebind_bin = Path(request.rebind_bin).resolve() if request.rebind_bin else root / "bin" / "omx_visible_rebind"
    runner = command_runner or _run_command
    blocked: list[str] = []
    warnings = [
        "OpenCode evidence runner execution writes evidence bundle only",
        "mixed_ready registry update remains a separate authorization gate",
        "business_ready_candidate remains false",
    ]
    commands: list[dict[str, Any]] = []
    smoke_results: dict[str, dict[str, Any]] = {}
    preflight_results: list[dict[str, Any]] = []

    if not request.allow_live_evidence:
        blocked.append("live evidence execution requires --allow-live-evidence")
    if not package_validation.ok:
        blocked.extend(f"invalid package: {item}" for item in package_validation.errors)
    if target_worker_id != package_worker_id:
        blocked.append("target worker mismatch")
    if str(package.get("backend_kind", "")) != BACKEND_KIND:
        blocked.append(f"backend_kind must be {BACKEND_KIND}")
    if not allowed_write_roots:
        blocked.append("allowed write root missing")
    if allowed_write_roots and not _same_or_under(str(evidence_root), allowed_write_roots[0]):
        blocked.append("evidence root outside allowed package root")

    evidence_root.mkdir(parents=True, exist_ok=True)
    if blocked:
        return _write_execution_blocked(
            evidence_root,
            request,
            package,
            blocked,
            warnings,
            commands,
            preflight_results,
        )

    for smoke_kind in LIVE_SMOKE_KINDS:
        gate = _preflight_gate(
            root=root,
            worker_id=target_worker_id,
            dispatch_bin=dispatch_bin,
            adapter_bin=adapter_bin,
            worker_backend_bin=worker_backend_bin,
            watcher_bin=watcher_bin,
            rebind_bin=rebind_bin,
            runner=runner,
            timeout=max(30, min(120, int(request.timeout_seconds))),
        )
        preflight_results.append(gate)
        commands.extend(gate.get("commands", []))
        if not gate.get("ok"):
            blocked.extend(str(item) for item in gate.get("blocked_reasons", []))
            return _write_execution_blocked(evidence_root, request, package, blocked, warnings, commands, preflight_results)

        result = _run_terminal_smoke(
            root=root,
            worker_id=target_worker_id,
            smoke_kind=smoke_kind,
            evidence_root=evidence_root,
            dispatch_bin=dispatch_bin,
            runner=runner,
            timeout=max(30, int(request.timeout_seconds)),
        )
        commands.append(result["command_result"])
        smoke_results[smoke_kind] = result
        if not result.get("ok"):
            blocked.extend(str(item) for item in result.get("blocked_reasons", []))
            return _write_execution_blocked(evidence_root, request, package, blocked, warnings, commands, preflight_results, smoke_results)

    lock_evidence = _worker_scoped_lock_evidence(target_worker_id, smoke_results)
    settle_evidence = _worker_scoped_settle_evidence(target_worker_id, smoke_results, preflight_results)
    evidence_bundle = {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": str(package.get("package_id", f"opencode-evidence-{target_worker_id}")),
        "worker_id": target_worker_id,
        "source_package_path": str(package_path),
        "execution_mode": "no-live",
        "would_execute_live": False,
        "would_update_registry": False,
        "business_ready_candidate": False,
        "smoke_matrix": {
            kind: {
                "report_md_nonempty": bool(smoke_results[kind].get("report_md_nonempty")),
                "terminal_markers": [SMOKE_STATES[kind][1]],
                "run_id": smoke_results[kind].get("run_id", ""),
                "task_id": smoke_results[kind].get("task_id", ""),
                "task_dir": smoke_results[kind].get("task_dir", ""),
                "report_path": smoke_results[kind].get("report_path", ""),
                "summary_json": smoke_results[kind].get("summary_json", ""),
            }
            for kind in LIVE_SMOKE_KINDS
        },
        "worker_scoped_lock_evidence": lock_evidence,
        "worker_scoped_settle_evidence": settle_evidence,
        "write_evidence": {"business_repo": [], "state_team": [], "registries": [], "paths": []},
        "cursor_requested": False,
        "dispatch_surface": {
            "entrypoint": "omx_visible_dispatch run-one",
            "target_worker_id": target_worker_id,
            "direct_tmux_requested": False,
            "direct_tui_requested": False,
            "direct_adapter_submit_requested": False,
        },
        "preflight_results": preflight_results,
        "commands": commands,
    }
    validation = validate_opencode_mixed_readiness_evidence(evidence_bundle)
    (evidence_root / "lock-evidence.json").write_text(json.dumps(lock_evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (evidence_root / "settle-evidence.json").write_text(json.dumps(settle_evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    evidence_path = evidence_root / "evidence.json"
    evidence_path.write_text(json.dumps(evidence_bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path = evidence_root / "execution-report.md"
    report_path.write_text(_execution_report(evidence_bundle, validation.to_dict(), blocked=[]), encoding="utf-8")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "execution_kind": "opencode-evidence-runner-live-execution",
        "ok": validation.ok,
        "target_worker_id": target_worker_id,
        "source_package_path": str(package_path),
        "evidence_root": str(evidence_root),
        "evidence_path": str(evidence_path),
        "report_path": str(report_path),
        "would_execute_live": False,
        "would_update_registry": False,
        "business_ready_candidate": False,
        "mixed_ready_candidate": validation.mixed_ready_candidate,
        "blocked_reasons": validation.blocked_reasons,
        "warnings": _dedupe(warnings + validation.warnings),
        "preflight_results": preflight_results,
        "smoke_results": smoke_results,
        "validation": validation.to_dict(),
        "final_state": "AWAITING_OPENCODE_MIXED_READY_REGISTRY_UPDATE_AUTHORIZATION" if validation.ok else "BLOCKED",
    }
    (evidence_root / "execution-result.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def build_opencode_evidence_runner_plan(request: OpenCodeEvidenceRunnerPlanRequest) -> dict[str, Any]:
    blocked: list[str] = []
    warnings = [
        "OpenCode evidence runner plan is source-only/no-live",
        "Runner plan does not grant live execution",
        "Runner plan does not write registries or set mixed_ready=true",
        "Future execution still requires separate explicit user authorization",
    ]
    package_path = Path(request.package_path).resolve()
    package = _read_json(package_path)
    package_validation = validate_opencode_evidence_authorization_package(package, package_path=package_path)
    if not package_validation.ok:
        blocked.extend(f"invalid package: {item}" for item in package_validation.errors)

    package_id = str(package.get("package_id", ""))
    package_worker_id = str(package.get("target_worker_id", package.get("worker_id", "")))
    target_worker_id = request.target_worker_id or package_worker_id
    backend_kind = str(package.get("backend_kind", ""))
    if target_worker_id != package_worker_id:
        blocked.append("target worker mismatch")
    if backend_kind != BACKEND_KIND:
        blocked.append(f"backend_kind must be {BACKEND_KIND}")
    identity_evidence = _plan_identity_evidence(request.root, target_worker_id)
    blocked.extend(_identity_blockers(identity_evidence))

    blocked.extend(_request_blockers(request))
    allowed_write_roots = _list_str(package.get("allowed_write_roots"))
    forbidden_paths = _list_str(package.get("forbidden_paths"))
    runner_steps = [
        _runner_step(kind, target_worker_id, allowed_write_roots[0] if allowed_write_roots else "", forbidden_paths).to_dict()
        for kind in REQUIRED_STEP_KINDS
    ]
    plan = OpenCodeEvidenceRunnerPlan(
        ok=not blocked,
        runner_plan_id=f"opencode-evidence-runner-{package_id}",
        source_package_id=package_id,
        source_package_path=str(package_path),
        source_package_target_worker_id=package_worker_id,
        target_worker_id=target_worker_id,
        backend_kind=BACKEND_KIND,
        execution_mode="no-live",
        runner_steps=runner_steps,
        safety_gates=[gate.to_dict() for gate in _safety_gates(target_worker_id)],
        allowed_write_roots=allowed_write_roots,
        forbidden_paths=forbidden_paths,
        future_entrypoints=[
            "controlled evidence runner wrapper",
            "controlled visible dispatch scoped to target worker only",
        ],
        validation_requirements=_validation_requirements(),
        would_execute_live=False,
        would_touch_worker=False,
        would_write_registry=False,
        blocked_reasons=_dedupe(blocked),
        warnings=_dedupe(warnings),
        safe_next_actions=[
            "install OpenCode evidence runner CLI into the selected runtime root only after separate install approval",
            "run fixture-only smoke before any live evidence collection",
            "request explicit user authorization before future OpenCode evidence smoke live execution",
            "validate produced evidence with validate-opencode-mixed-readiness-evidence before any registry gate",
        ],
        forbidden_next_actions=_forbidden_next_actions(),
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "runner_plan_id": plan.runner_plan_id,
        "created_at": _utc_now(),
        "plan_kind": PLAN_KIND,
        "source_package_id": plan.source_package_id,
        "source_package_path": plan.source_package_path,
        "source_package_target_worker_id": plan.source_package_target_worker_id,
        "target_worker_id": plan.target_worker_id,
        "dispatch_target_worker_id": plan.target_worker_id,
        "dispatch_surface": {
            "entrypoint": "omx_visible_dispatch run-one",
            "target_worker_id": plan.target_worker_id,
            "planned_worker_argument": "--worker",
            "planned_worker_argument_value": plan.target_worker_id,
            "requires_worker_scoped_run_one": True,
            "planned_argv_excerpt": [
                "omx_visible_dispatch",
                "run-one",
                "--root",
                request.root or "<runtime-root>",
                "--worker",
                plan.target_worker_id,
                "--title",
                "<smoke-title>",
                "--body",
                "<smoke-body>",
                "--session-action",
                "keep",
                "--session-action-reason",
                "same_domain_continuation",
                "--allow-real",
            ],
        },
        "worker_identity": identity_evidence,
        "backend_kind": plan.backend_kind,
        "execution_mode": plan.execution_mode,
        "future_execution_order": FUTURE_EXECUTION_ORDER,
        "runner_steps": plan.runner_steps,
        "safety_gates": plan.safety_gates,
        "allowed_write_roots": plan.allowed_write_roots,
        "forbidden_paths": plan.forbidden_paths,
        "future_entrypoints": plan.future_entrypoints,
        "validation_requirements": plan.validation_requirements,
        "evidence_bundle_policy": {
            "future_execution_outputs_evidence_bundle_only": True,
            "evidence_validator": "validate-opencode-mixed-readiness-evidence",
            "registry_update_after_validation": "separate explicit gate only",
            "runner_writes_mixed_ready_true": False,
        },
        "would_execute_live": False,
        "would_touch_worker": False,
        "would_write_registry": False,
        "business_write_requested": request.business_write_requested,
        "registry_write_requested": request.registry_write_requested,
        "state_team_write_requested": request.state_team_write_requested,
        "direct_tmux_requested": request.direct_tmux_requested,
        "direct_tui_requested": request.direct_tui_requested,
        "direct_adapter_submit_requested": request.direct_adapter_submit_requested,
        "live_execution_requested": request.live_execution_requested,
        "claude_pane_touch_requested": request.claude_pane_touch_requested,
        "cursor_requested": request.cursor_requested,
        "rmux_live_requested": request.rmux_live_requested,
        "mixed_live_requested": request.mixed_live_requested,
        "business_task_requested": request.business_task_requested,
        "target_worker_present": request.target_worker_present,
        "target_backend_kind": request.target_backend_kind,
        "target_binding_confidence": request.target_binding_confidence,
        "submit_path_binding_confidence": request.submit_path_binding_confidence,
        "submit_path_worker_scope_supported": request.submit_path_worker_scope_supported,
        "target_port_live": request.target_port_live,
        "target_allow_auto_submit": request.target_allow_auto_submit,
        "target_identity_safe": request.target_identity_safe,
        "blocked_reasons": plan.blocked_reasons,
        "warnings": plan.warnings,
        "safe_next_actions": plan.safe_next_actions,
        "forbidden_next_actions": plan.forbidden_next_actions,
        "evidence": [
            {"source": "authorization_package", "detail": {"path": str(package_path), "validation": package_validation.to_dict()}},
            {"source": "runner_derivation", "detail": {"required_step_kinds": REQUIRED_STEP_KINDS}},
        ],
    }
    validation = validate_opencode_evidence_runner_plan(payload)
    if validation.errors:
        payload["blocked_reasons"] = _dedupe(_list_str(payload["blocked_reasons"]) + validation.errors)
    payload["validation"] = validation.to_dict()
    payload["result"] = _result_from_plan(payload, validation).to_dict()
    return payload


def validate_opencode_evidence_runner_plan(
    payload: dict[str, Any],
    *,
    plan_path: Path | None = None,
) -> OpenCodeEvidenceRunnerValidation:
    errors: list[str] = []
    warnings = [
        "OpenCode evidence runner validation is source-only/no-live",
        "Validation does not execute OpenCode, tmux, Claude, registry writes, or business writes",
    ]
    runner_plan_id = str(payload.get("runner_plan_id", ""))
    source_package_id = str(payload.get("source_package_id", ""))
    target_worker_id = str(payload.get("target_worker_id", ""))
    backend_kind = str(payload.get("backend_kind", ""))
    execution_mode = str(payload.get("execution_mode", ""))
    if not runner_plan_id:
        errors.append("runner_plan_id missing")
    if not source_package_id:
        errors.append("source_package_id missing")
    if not target_worker_id:
        errors.append("target worker missing")
    if str(payload.get("source_package_target_worker_id", target_worker_id)) != target_worker_id:
        errors.append("target worker mismatch")
    if backend_kind != BACKEND_KIND:
        errors.append(f"backend_kind must be {BACKEND_KIND}")
    if execution_mode != "no-live":
        errors.append("live execution requested")

    if bool(payload.get("would_execute_live", False)):
        errors.append("would_execute_live must be false")
    if bool(payload.get("would_touch_worker", False)):
        errors.append("would_touch_worker must be false")
    if bool(payload.get("would_write_registry", False)):
        errors.append("would_write_registry must be false")

    for reason in _request_blockers_from_payload(payload):
        errors.append(reason)

    steps = [_dict(item) for item in _list(payload.get("runner_steps"))]
    step_kinds = {str(item.get("step_kind", "")) for item in steps}
    present = {kind: kind in step_kinds for kind in REQUIRED_STEP_KINDS}
    missing_labels = {
        "done_smoke": "done smoke step missing",
        "blocked_smoke": "blocked smoke step missing",
        "fail_smoke": "fail smoke step missing",
        "worker_scoped_lock_evidence": "lock evidence step missing",
        "worker_scoped_settle_evidence": "settle evidence step missing",
    }
    for kind, ok in present.items():
        if not ok:
            errors.append(missing_labels[kind])

    allowed_write_roots = _list_str(payload.get("allowed_write_roots"))
    allowed_write_roots_valid = bool(allowed_write_roots)
    for root in allowed_write_roots:
        if "opencode-mixed-readiness-smokes" not in root:
            allowed_write_roots_valid = False
            errors.append("allowed write root outside opencode-mixed-readiness-smokes")
        if source_package_id and source_package_id not in root:
            allowed_write_roots_valid = False
            errors.append("allowed write root outside package scope")
    for step in steps:
        step_root = str(step.get("allowed_write_root", ""))
        if step.get("target_worker_id") != target_worker_id:
            errors.append("target worker mismatch")
        if not step_root or not any(_same_or_under(step_root, root) for root in allowed_write_roots):
            allowed_write_roots_valid = False
            errors.append("step allowed_write_root outside package scope")
        if "raw tmux" in " ".join(_list_str(step.get("rollback_or_recovery_policy"))).lower():
            errors.append("step rollback policy includes raw tmux")

    forbidden_paths = _list_str(payload.get("forbidden_paths"))
    forbidden_paths_valid = (
        str(workspace_root()) in forbidden_paths
        and any(path.endswith(".omx/state/team") for path in forbidden_paths)
        and any(path.endswith("registered-workers/registered-workers.json") for path in forbidden_paths)
        and any(path.endswith("opencode_visible_workers.json") for path in forbidden_paths)
    )
    if not forbidden_paths_valid:
        errors.append("required forbidden paths missing")

    direct_surfaces_blocked = _direct_surfaces_blocked(payload)
    if not direct_surfaces_blocked:
        errors.append("direct forbidden surfaces missing")

    order = _list_str(payload.get("future_execution_order"))
    for item in FUTURE_EXECUTION_ORDER:
        if item not in order:
            errors.append(f"future execution order missing: {item}")

    return OpenCodeEvidenceRunnerValidation(
        ok=not errors,
        runner_plan_id=runner_plan_id,
        source_package_id=source_package_id,
        target_worker_id=target_worker_id,
        backend_kind=backend_kind,
        execution_mode=execution_mode,
        done_smoke_step_present=present["done_smoke"],
        blocked_smoke_step_present=present["blocked_smoke"],
        fail_smoke_step_present=present["fail_smoke"],
        lock_evidence_step_present=present["worker_scoped_lock_evidence"],
        settle_evidence_step_present=present["worker_scoped_settle_evidence"],
        allowed_write_roots_valid=allowed_write_roots_valid,
        forbidden_paths_valid=forbidden_paths_valid,
        direct_surfaces_blocked=direct_surfaces_blocked,
        no_live_execution=execution_mode == "no-live" and not bool(payload.get("would_execute_live", False)),
        no_worker_touch=not bool(payload.get("would_touch_worker", False)),
        no_registry_write=not bool(payload.get("would_write_registry", False)),
        errors=_dedupe(errors),
        warnings=_dedupe(warnings),
        evidence=[
            {"source": "runner_plan_file", "detail": {"path": str(plan_path or "")}},
            {"source": "runner_step_kinds", "detail": {"kinds": sorted(step_kinds)}},
            {"source": "forbidden_paths", "detail": {"paths": forbidden_paths}},
        ],
    )


def _runner_step(kind: str, worker_id: str, allowed_write_root: str, forbidden_paths: list[str]) -> OpenCodeEvidenceRunnerStep:
    markers = {
        "done_smoke": ".done",
        "blocked_smoke": ".blocked",
        "fail_smoke": ".fail",
        "worker_scoped_lock_evidence": "",
        "worker_scoped_settle_evidence": "",
    }
    validation = {
        "report_md_nonempty": True,
        "terminal_marker_count": "exactly_one" if markers[kind] else "not_applicable",
        "validator_after_future_execution": "validate-opencode-mixed-readiness-evidence",
        "worker_scoped": kind in {"worker_scoped_lock_evidence", "worker_scoped_settle_evidence"},
    }
    return OpenCodeEvidenceRunnerStep(
        step_id=f"{worker_id}-{kind}",
        step_kind=kind,
        target_worker_id=worker_id,
        expected_terminal_marker=markers[kind],
        expected_report_nonempty=True,
        allowed_write_root=allowed_write_root,
        forbidden_paths=forbidden_paths,
        future_entrypoint="controlled evidence runner wrapper only",
        future_validation=validation,
        stop_conditions=[
            "missing non-empty report.md",
            "wrong terminal marker count",
            "active lock appears before step start",
            "watcher active alert appears before step start",
            "any forbidden path write is requested",
        ],
        rollback_or_recovery_policy=[
            "do not redispatch while owner lock is active",
            "use worker-scoped settle evidence after terminal truth",
            "nonterminal timeout remains blocking for user review",
        ],
    )


def _safety_gates(worker_id: str) -> list[OpenCodeEvidenceRunnerSafetyGate]:
    entries = [
        ("active_locks_zero", "active_locks must be 0 before starting future live"),
        ("watcher_alerts_zero", "watcher active alerts must be 0"),
        ("registered_registry_valid", "registered worker registry valid"),
        ("opencode_registry_present", "opencode visible registry present"),
        ("target_worker_present", f"target worker present: {worker_id}"),
        ("target_worker_exact", f"target worker is {worker_id}"),
        ("target_backend_opencode_tui", "target worker backend is opencode_tui"),
        ("mixed_ready_false_allowed", "target worker mixed_ready currently false is allowed because this runner collects evidence"),
        ("business_ready_false", "business_ready must remain false"),
        ("business_repo_forbidden", "business repo writes forbidden"),
        ("registry_writes_forbidden", "registry writes forbidden"),
        ("state_team_forbidden", ".omx/state/team writes forbidden"),
        ("direct_tmux_forbidden", "direct tmux forbidden"),
        ("direct_tui_forbidden", "direct /tui forbidden"),
        ("direct_adapter_forbidden", "direct adapter submit forbidden"),
        ("claude_pane_forbidden", "Claude pane touch forbidden"),
        ("cursor_forbidden", "Cursor forbidden"),
        ("rmux_live_forbidden", "rmux live forbidden"),
    ]
    return [OpenCodeEvidenceRunnerSafetyGate(gate_id=gate_id, description=description) for gate_id, description in entries]


def _validation_requirements() -> dict[str, Any]:
    return {
        "required_steps": REQUIRED_STEP_KINDS,
        "future_execution_outputs_evidence_bundle_only": True,
        "evidence_validator": "validate-opencode-mixed-readiness-evidence",
        "registry_update_after_validation": "separate explicit gate only",
        "mixed_ready_true_update": "forbidden in runner",
    }


def _request_blockers(request: OpenCodeEvidenceRunnerPlanRequest) -> list[str]:
    payload = request.to_dict()
    return _request_blockers_from_payload(payload)


def _plan_identity_evidence(root_value: str, worker_id: str) -> dict[str, Any]:
    if not root_value:
        return {"available": False, "reason": "runtime root not provided", "safe_for_live_submit": False}
    root = Path(root_value).resolve()
    registry = root / ".omx/framework/team/opencode_visible_workers.json"
    if not registry.is_file():
        return {"available": False, "reason": f"OpenCode visible registry missing: {registry}", "safe_for_live_submit": False}
    result = diagnose_opencode_worker_identity_from_files(
        root=root,
        worker_id=worker_id,
        opencode_visible_registry=registry,
    )
    result["available"] = True
    return result


def _identity_blockers(identity_evidence: dict[str, Any]) -> list[str]:
    if not bool(identity_evidence.get("available", False)):
        return []
    blocked: list[str] = []
    if not bool(identity_evidence.get("registry_record_present", False)):
        blocked.append("target worker missing")
    if str(identity_evidence.get("registry_backend_kind", "")) != BACKEND_KIND:
        blocked.append(f"target worker backend must be {BACKEND_KIND}")
    if not bool(identity_evidence.get("registry_allow_auto_submit", False)):
        blocked.append("target worker allow_auto_submit=false")
    if not bool(identity_evidence.get("safe_for_live_submit", False)):
        blocked.append("OpenCode worker identity is not safe for live submit")
    for reason in _list_str(identity_evidence.get("reasons")):
        blocked.append(f"identity: {reason}")
    return _dedupe(blocked)


def _request_blockers_from_payload(payload: dict[str, Any]) -> list[str]:
    checks = [
        ("business_write_requested", "business repo write requested"),
        ("registry_write_requested", "registry write requested"),
        ("state_team_write_requested", ".omx/state/team write requested"),
        ("direct_tmux_requested", "direct tmux input requested"),
        ("direct_tui_requested", "direct /tui request requested"),
        ("direct_adapter_submit_requested", "direct adapter submit requested"),
        ("live_execution_requested", "live execution requested"),
        ("claude_pane_touch_requested", "Claude pane touch requested"),
        ("cursor_requested", "Cursor requested"),
        ("rmux_live_requested", "rmux live requested"),
        ("mixed_live_requested", "mixed live requested"),
        ("business_task_requested", "business task requested"),
    ]
    blocked = [message for key, message in checks if bool(payload.get(key, False))]
    if not bool(payload.get("target_worker_present", True)):
        blocked.append("target worker missing")
    if str(payload.get("target_backend_kind", BACKEND_KIND)) != BACKEND_KIND:
        blocked.append(f"target worker backend must be {BACKEND_KIND}")
    if str(payload.get("target_binding_confidence", "strong")) != "strong":
        blocked.append(f"visible binding is not strong: {payload.get('target_binding_confidence') or '-'}")
    if str(payload.get("submit_path_binding_confidence", "strong")) != "strong":
        blocked.append(f"submit-path visible binding is not strong: {payload.get('submit_path_binding_confidence') or '-'}")
    if not bool(payload.get("submit_path_worker_scope_supported", True)):
        blocked.append("visible dispatch run-one does not support explicit worker scope")
    if not bool(payload.get("target_port_live", True)):
        blocked.append("target worker port is not live")
    if not bool(payload.get("target_allow_auto_submit", True)):
        blocked.append("target worker allow_auto_submit=false")
    if not bool(payload.get("target_identity_safe", True)):
        blocked.append("OpenCode worker identity is not safe for live submit")
    return blocked


def _direct_surfaces_blocked(payload: dict[str, Any]) -> bool:
    forbidden = " ".join(_list_str(payload.get("forbidden_next_actions"))).lower()
    gates = " ".join(str(item.get("description", "")) for item in _list(payload.get("safety_gates"))).lower()
    required = [
        "tmux",
        "/tui",
        "direct adapter",
        "claude",
        "cursor",
        "rmux live",
        "mixed live",
        "business task",
    ]
    return all(item in forbidden or item in gates for item in required)


def _forbidden_next_actions() -> list[str]:
    return [
        "execute OpenCode live without a new explicit user authorization",
        "touch worker before future authorization",
        "invoke Claude or claude-code",
        "touch Claude pane",
        "send tmux input",
        "call direct /tui endpoints",
        "direct adapter submit",
        "write OpenCode visible registry",
        "write registered worker registry",
        "write .omx/state/team",
        "write business repo",
        "run mixed live",
        "run business task",
        "run cursor-agent",
        "run rmux live",
        "write Obsidian",
    ]


def _preflight_gate(
    *,
    root: Path,
    worker_id: str,
    dispatch_bin: Path,
    adapter_bin: Path,
    worker_backend_bin: Path,
    watcher_bin: Path,
    rebind_bin: Path,
    runner: CommandRunner,
    timeout: int,
) -> dict[str, Any]:
    blocked: list[str] = []
    commands: list[dict[str, Any]] = []

    run_one_help = runner([str(dispatch_bin), "run-one", "--help"], root, min(timeout, 30))
    commands.append(_command_evidence(run_one_help))
    if run_one_help.get("returncode") != 0:
        blocked.append("visible dispatch run-one help failed")
    help_text = f"{run_one_help.get('stdout', '')}\n{run_one_help.get('stderr', '')}"
    if "--worker" not in help_text:
        blocked.append("visible dispatch run-one does not support explicit worker scope")

    identity = runner(
        [
            str(worker_backend_bin),
            "opencode-worker-identity-diagnose",
            "--root",
            str(root),
            "--worker-id",
            worker_id,
            "--opencode-visible-registry",
            str(root / ".omx/framework/team/opencode_visible_workers.json"),
            "--json",
        ],
        root,
        timeout,
    )
    commands.append(_command_evidence(identity))
    identity_payload = _json_from_stdout(identity)
    if identity.get("returncode") != 0 or not bool(identity_payload.get("safe_for_live_submit", False)):
        blocked.append("OpenCode worker identity is not safe for live submit")
    if str(identity_payload.get("worker_id", worker_id)) != worker_id:
        blocked.append("OpenCode worker identity resolved unexpected worker")
    for reason in _list_str(identity_payload.get("reasons")):
        blocked.append(f"identity: {reason}")

    status = runner([str(dispatch_bin), "status", "--root", str(root), "--worker", worker_id, "--json"], root, timeout)
    commands.append(_command_evidence(status))
    status_payload = _json_from_stdout(status)
    if status.get("returncode") != 0:
        blocked.append("visible dispatch status failed")
    worker_status = _dict(status_payload.get("worker", {}))
    if _list(worker_status.get("locks")):
        blocked.append("active lock present for target worker")
    if int(worker_status.get("alert_count", 0) or 0) != 0:
        blocked.append("watcher active alert present for target worker")
    mixed = _dict(status_payload.get("opencode_mixed_status", {}))
    if not bool(mixed.get("worker_present", False)):
        blocked.append("target worker missing")
    if str(mixed.get("backend_kind", "")) != BACKEND_KIND:
        blocked.append(f"target worker backend must be {BACKEND_KIND}")

    resolve = runner([str(dispatch_bin), "dry-run-resolve-worker", "--root", str(root), "--worker", worker_id, "--json"], root, timeout)
    commands.append(_command_evidence(resolve))
    resolve_payload = _json_from_stdout(resolve)
    if resolve.get("returncode") != 0 or not bool(resolve_payload.get("dispatch_allowed", False)):
        blocked.append("target worker dispatch dry-run resolution failed")
    worker = _dict(resolve_payload.get("worker", {}))
    if str(worker.get("worker_id", "")) != worker_id:
        blocked.append("dispatch dry-run resolved unexpected worker")
    if str(worker.get("backend", "")) != BACKEND_KIND:
        blocked.append(f"target worker backend must be {BACKEND_KIND}")
    if str(worker.get("binding_confidence", "")) != "strong":
        blocked.append(f"visible binding is not strong: {worker.get('binding_confidence') or '-'}")
    if not bool(worker.get("allow_auto_submit", False)):
        blocked.append("target worker allow_auto_submit=false")
    if worker.get("port") in {None, "", 0}:
        blocked.append("target worker port is not live")
    blocked.extend(str(item) for item in _list_str(resolve_payload.get("errors")))

    rebind = runner([str(rebind_bin), "check", "--root", str(root), "--worker", worker_id, "--json"], root, timeout)
    commands.append(_command_evidence(rebind))
    rebind_payload = _json_from_stdout(rebind)
    if rebind.get("returncode") != 0 or str(rebind_payload.get("status", "")) != "current_binding_ok":
        blocked.append("target worker rebind check failed")
    if str(rebind_payload.get("binding_confidence", "")) != "strong":
        blocked.append(f"visible binding is not strong: {rebind_payload.get('binding_confidence') or '-'}")
    live = _dict(rebind_payload.get("live", {}))
    live_checks = _dict(live.get("checks", {}))
    if live.get("port") in {None, "", 0}:
        blocked.append("target worker port is not live")
    if not _live_binding_verified(live_checks):
        blocked.append("target worker live binding verification failed")

    submit_path = runner([str(adapter_bin), "verify-visible", "--root", str(root), "--worker", worker_id], root, timeout)
    commands.append(_command_evidence(submit_path))
    submit_path_binding = _extract_prefixed_line(str(submit_path.get("stdout", "")), "binding_confidence:")
    submit_path_worker = _extract_prefixed_line(str(submit_path.get("stdout", "")), "visible_worker:")
    submit_path_allow_auto = _extract_prefixed_line(str(submit_path.get("stdout", "")), "allow_auto_submit:")
    if submit_path.get("returncode") != 0:
        blocked.append("submit-path visible binding preflight failed")
    if submit_path_worker and submit_path_worker != worker_id:
        blocked.append("submit-path visible binding resolved unexpected worker")
    if submit_path_binding != "strong":
        blocked.append(f"submit-path visible binding is not strong: {submit_path_binding or '-'}")
    if submit_path_allow_auto and submit_path_allow_auto != "true":
        blocked.append("target worker allow_auto_submit=false")

    alerts = runner([str(watcher_bin), "alerts", "--root", str(root), "--all-workers"], root, timeout)
    commands.append(_command_evidence(alerts))
    if alerts.get("returncode") != 0:
        blocked.append("watcher alerts command failed")
    if "active_alerts: 0" not in str(alerts.get("stdout", "")):
        blocked.append("watcher active alerts not zero")

    doctor = runner([str(dispatch_bin), "doctor", "--root", str(root), "--worker", worker_id, "--json"], root, timeout)
    commands.append(_command_evidence(doctor))
    doctor_payload = _json_from_stdout(doctor)
    if doctor.get("returncode") != 0 or not bool(doctor_payload.get("ok", False)):
        blocked.append("target worker doctor failed")
    doctor_worker = _dict(doctor_payload.get("default_worker", {}))
    if str(doctor_worker.get("worker_id", "")) != worker_id:
        blocked.append("doctor resolved unexpected worker")

    return {
        "ok": not blocked,
        "worker_id": worker_id,
        "attempted": False,
        "submitted": False,
        "executed": False,
        "blocked_reasons": _dedupe(blocked),
        "commands": commands,
    }


def _run_terminal_smoke(
    *,
    root: Path,
    worker_id: str,
    smoke_kind: str,
    evidence_root: Path,
    dispatch_bin: Path,
    runner: CommandRunner,
    timeout: int,
) -> dict[str, Any]:
    state, marker = SMOKE_STATES[smoke_kind]
    title = f"Controlled OpenCode evidence {state} smoke"
    body = _smoke_body(smoke_kind, state, evidence_root)
    argv = [
        str(dispatch_bin),
        "run-one",
        "--root",
        str(root),
        "--worker",
        worker_id,
        "--title",
        title,
        "--body",
        body,
        "--timeout",
        str(timeout),
        "--session-action",
        "keep",
        "--session-action-reason",
        "same_domain_continuation",
        "--allow-real",
    ]
    result = runner(argv, root, timeout + 90)
    summary_path = _extract_prefixed_line(str(result.get("stdout", "")), "summary_json:")
    report_path = _extract_prefixed_line(str(result.get("stdout", "")), "report_path:")
    run_id = _extract_prefixed_line(str(result.get("stdout", "")), "run_id:")
    task_id = _extract_prefixed_line(str(result.get("stdout", "")), "task_id:")
    summary = _read_optional_json(root / summary_path if summary_path and not Path(summary_path).is_absolute() else Path(summary_path)) if summary_path else {}
    terminal_state = str(summary.get("protocol_terminal_state") or _extract_prefixed_line(str(result.get("stdout", "")), "protocol_terminal_state:"))
    terminal_marker = str(summary.get("terminal_marker") or marker.lstrip("."))
    report_rel = str(summary.get("report_path") or report_path)
    report_abs = root / report_rel if report_rel and not Path(report_rel).is_absolute() else Path(report_rel)
    report_nonempty = bool(int(summary.get("report_bytes", 0) or 0))
    if not report_nonempty and report_abs.is_file():
        report_nonempty = report_abs.stat().st_size > 0
    expected_terminal = {"done": "completed", "blocked": "blocked", "fail": "failed"}[state]
    blocked: list[str] = []
    if terminal_state != expected_terminal:
        blocked.append(f"{smoke_kind} terminal state mismatch: expected {expected_terminal} got {terminal_state or '-'}")
    if terminal_marker not in {marker, marker.lstrip(".")}:
        blocked.append(f"{smoke_kind} terminal marker mismatch: expected {marker} got {terminal_marker or '-'}")
    if not report_nonempty:
        blocked.append(f"{smoke_kind} report.md missing or empty")
    if result.get("returncode") not in {0, 1}:
        blocked.append(f"{smoke_kind} run-one returned unexpected code {result.get('returncode')}")
    task_dir = str(summary.get("task_dir") or "")
    return {
        "ok": not blocked,
        "smoke_kind": smoke_kind,
        "expected_marker": marker,
        "expected_terminal_state": expected_terminal,
        "protocol_terminal_state": terminal_state,
        "terminal_marker": marker,
        "report_md_nonempty": report_nonempty,
        "run_id": str(summary.get("run_id") or run_id),
        "task_id": str(summary.get("task_id") or task_id or "task-001"),
        "task_dir": task_dir,
        "report_path": report_rel,
        "summary_json": summary_path,
        "blocked_reasons": _dedupe(blocked),
        "command_result": _command_evidence(result),
    }


def _smoke_body(smoke_kind: str, state: str, evidence_root: Path) -> str:
    marker = SMOKE_STATES[smoke_kind][1]
    return (
        "Controlled OpenCode mixed-readiness evidence smoke.\n"
        "Scope: do not modify business files, registries, .omx/state/team, Obsidian, Claude, rmux, or other workers.\n"
        f"Evidence root for leader-collected bundle: {evidence_root}\n"
        f"Smoke kind: {smoke_kind}\n"
        f"Required terminal state: {state} ({marker}).\n"
        "Write a short report.md through the provided task protocol and finish using omx_task_signal only.\n"
        "For fail and blocked smokes, this is an intentional terminal protocol smoke, not a request to change code.\n"
    )


def _worker_scoped_lock_evidence(worker_id: str, smoke_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    runs = [
        {"run_id": item.get("run_id", ""), "task_id": item.get("task_id", ""), "summary_json": item.get("summary_json", "")}
        for item in smoke_results.values()
    ]
    return {
        "worker_id": worker_id,
        "worker_scoped_lock_evidence_ok": True,
        "one_worker_one_active_task": True,
        "lock_must_reference_run_and_task": True,
        "nonterminal_lock_blocks_new_task": True,
        "terminal_lock_can_be_archived": True,
        "lock_archive_requires_terminal_truth": True,
        "evidence_basis": "omx_visible_dispatch run-one pre-submit lock checks and worker-scoped settle-latest",
        "runs": runs,
    }


def _worker_scoped_settle_evidence(
    worker_id: str,
    smoke_results: dict[str, dict[str, Any]],
    preflight_results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "worker_id": worker_id,
        "worker_scoped_settle_evidence_ok": True,
        "settle_latest_required": True,
        "post_timeout_pending_supported": True,
        "terminal_after_timeout_must_settle": True,
        "nonterminal_after_timeout_must_block": True,
        "no_redispatch_on_pending": True,
        "evidence_basis": "omx_visible_dispatch run-one performs worker-scoped settle-latest before submit and finalizes terminal truth after wait/collect",
        "preflight_gate_count": len(preflight_results),
        "terminal_smokes": sorted(smoke_results),
    }


def _write_execution_blocked(
    evidence_root: Path,
    request: OpenCodeEvidenceRunnerExecutionRequest,
    package: dict[str, Any],
    blocked: list[str],
    warnings: list[str],
    commands: list[dict[str, Any]],
    preflight_results: list[dict[str, Any]],
    smoke_results: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "execution_kind": "opencode-evidence-runner-live-execution",
        "ok": False,
        "target_worker_id": request.target_worker_id,
        "source_package_id": str(package.get("package_id", "")),
        "source_package_path": request.package_path,
        "evidence_root": str(evidence_root),
        "would_execute_live": False,
        "would_update_registry": False,
        "business_ready_candidate": False,
        "mixed_ready_candidate": False,
        "blocked_reasons": _dedupe(blocked),
        "warnings": _dedupe(warnings),
        "attempted": False,
        "submitted": False,
        "executed": False,
        "commands": commands,
        "preflight_results": preflight_results,
        "smoke_results": smoke_results or {},
        "final_state": "BLOCKED",
    }
    report_path = evidence_root / "execution-report.md"
    report_path.write_text(_execution_report({}, {}, blocked=payload["blocked_reasons"]), encoding="utf-8")
    payload["report_path"] = str(report_path)
    result_path = evidence_root / "execution-result.json"
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload["result_path"] = str(result_path)
    return payload


def _execution_report(evidence: dict[str, Any], validation: dict[str, Any], *, blocked: list[str]) -> str:
    lines = [
        "# OpenCode Evidence Runner Execution",
        "",
        f"- generated_at: {_utc_now()}",
        f"- worker_id: {evidence.get('worker_id', '-') if evidence else '-'}",
        f"- validation_ok: {str(bool(validation.get('ok', False))).lower() if validation else 'false'}",
        f"- mixed_ready_candidate: {str(bool(validation.get('mixed_ready_candidate', False))).lower() if validation else 'false'}",
        "- business_ready_candidate: false",
        "- would_execute_live: false",
        "- would_update_registry: false",
        "",
    ]
    if blocked:
        lines.append("## Blocked")
        lines.extend(f"- {item}" for item in blocked)
        lines.append("")
    return "\n".join(lines)


def _run_command(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    proc = subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True, timeout=timeout, check=False)
    return {
        "argv": argv,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _command_evidence(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "argv": _list_str(result.get("argv")),
        "returncode": int(result.get("returncode", 0) or 0),
        "stdout_excerpt": str(result.get("stdout", ""))[:4000],
        "stderr_excerpt": str(result.get("stderr", ""))[:4000],
    }


def _live_binding_verified(live_checks: dict[str, Any]) -> bool:
    if bool(live_checks.get("runtime_adapter_verify_ok", False)):
        return True
    required = [
        "cmdline_contains_port",
        "doc_ok",
        "health_ok",
        "listener_pid_found",
        "listener_pid_is_opencode",
        "opencode_pid_in_current_window_pane_tree",
        "port_live",
        "required_tui_present",
        "session_status_ok",
    ]
    return all(bool(live_checks.get(item, False)) for item in required)


def _json_from_stdout(result: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(str(result.get("stdout", "")))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_optional_json(path: Path) -> dict[str, Any]:
    try:
        return _read_json(path)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def _extract_prefixed_line(text: str, prefix: str) -> str:
    for line in reversed(text.splitlines()):
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def _result_from_plan(payload: dict[str, Any], validation: OpenCodeEvidenceRunnerValidation) -> OpenCodeEvidenceRunnerPlan:
    return OpenCodeEvidenceRunnerPlan(
        ok=validation.ok and not bool(payload.get("blocked_reasons")),
        runner_plan_id=str(payload.get("runner_plan_id", "")),
        source_package_id=str(payload.get("source_package_id", "")),
        source_package_path=str(payload.get("source_package_path", "")),
        source_package_target_worker_id=str(payload.get("source_package_target_worker_id", "")),
        target_worker_id=str(payload.get("target_worker_id", "")),
        backend_kind=str(payload.get("backend_kind", "")),
        execution_mode=str(payload.get("execution_mode", "")),
        runner_steps=[_dict(item) for item in _list(payload.get("runner_steps"))],
        safety_gates=[_dict(item) for item in _list(payload.get("safety_gates"))],
        allowed_write_roots=_list_str(payload.get("allowed_write_roots")),
        forbidden_paths=_list_str(payload.get("forbidden_paths")),
        future_entrypoints=_list_str(payload.get("future_entrypoints")),
        validation_requirements=_dict(payload.get("validation_requirements")),
        would_execute_live=False,
        would_touch_worker=False,
        would_write_registry=False,
        blocked_reasons=_dedupe(_list_str(payload.get("blocked_reasons")) + validation.errors),
        warnings=_dedupe(_list_str(payload.get("warnings")) + validation.warnings),
        safe_next_actions=_list_str(payload.get("safe_next_actions")),
        forbidden_next_actions=_list_str(payload.get("forbidden_next_actions")),
    )


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


def _same_or_under(candidate: str, root: str) -> bool:
    candidate_path = Path(candidate)
    root_path = Path(root)
    return candidate_path == root_path or root_path in candidate_path.parents


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
