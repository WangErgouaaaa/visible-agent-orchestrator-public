"""No-live Coder/Reviewer/Gate role task packages."""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from visible_agent_orchestrator.runtime_paths import runtime_root

from ..gate_decision import build_gate_decision
from .authorization_profiles import (
    baseline_delta_evidence_blockers,
    prep_created_at_blocker,
    prepare_business_scoped_write_smoke,
)


ROLE_TASK_PACKAGE_FILES = (
    "role_task_package.json",
    "coder_task.json",
    "reviewer_task.json",
    "gate_task.json",
    "shared_evidence.json",
)
SANDBOX_ROLE_LIVE_SMOKE_PACKAGE_FILES = {
    "role_contract": "role-contract.json",
    "coder_prompt": "coder-prompt.md",
    "reviewer_prompt": "reviewer-prompt.md",
    "gate_prompt": "gate-prompt.md",
    "role_run_artifact_template": "role-run-artifact.template.json",
    "run_summary_template": "run-summary.template.json",
}
TERMINAL_MARKERS = (".done", ".fail", ".blocked")
SAFE_REVIEW_TMPDIR_TEMPLATE = "/tmp/visible-agent-orchestrator-review-XXXXXX"
FORBIDDEN_REVIEW_TMPDIR_ROOT = str(Path.home() / ".config" / "opencode")
CommandRunner = Callable[[list[str], Path, int], dict[str, Any]]


@dataclass(frozen=True)
class RoleTaskPackageRequest:
    profile_path: str
    contract_path: str
    coder_worker_id: str
    reviewer_worker_id: str
    output_dir: str = ""
    execution_mode: str = "no-live"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_role_task_package(request: RoleTaskPackageRequest) -> dict[str, Any]:
    profile_path = str(Path(request.profile_path).expanduser().resolve())
    contract_path = str(Path(request.contract_path).expanduser().resolve())
    coder_id = str(request.coder_worker_id or "").strip()
    reviewer_id = str(request.reviewer_worker_id or "").strip()
    output_dir = str(request.output_dir or "").strip()
    blockers: list[str] = []
    if request.execution_mode != "no-live":
        blockers.append("execution_mode must be no-live")
    if coder_id and reviewer_id and coder_id == reviewer_id:
        blockers.append("coder and reviewer must be different workers")

    prep = prepare_business_scoped_write_smoke(
        profile_path,
        contract_path,
        writer=coder_id,
        reviewer=reviewer_id,
    )
    blockers.extend(str(item) for item in prep.get("blocked_reasons", []))
    blocked_reasons = _dedupe(blockers)
    package_dir = str(Path(output_dir).expanduser().resolve()) if output_dir else ""

    coder_task = _coder_task(prep, coder_id)
    reviewer_task = _reviewer_task(prep, reviewer_id)
    gate_task = _gate_task(prep)
    shared_evidence = _shared_evidence(prep, coder_id, reviewer_id)
    package_files = {
        "role_task_package": "role_task_package.json",
        "coder_task": "coder_task.json",
        "reviewer_task": "reviewer_task.json",
        "gate_task": "gate_task.json",
        "shared_evidence": "shared_evidence.json",
    }
    role_package = {
        "schema_version": 1,
        "package_kind": "role-task-package-prep",
        "created_at": str(prep.get("created_at", "")),
        "ok": not blocked_reasons,
        "profile_id": str(prep.get("profile_id", "")),
        "profile_path": profile_path,
        "contract_path": contract_path,
        "package_dir": package_dir,
        "package_files": package_files,
        "profile_allowed_workers": list(prep.get("profile_allowed_workers", [])),
        "coder_worker_id": coder_id,
        "reviewer_worker_id": reviewer_id,
        "would_execute_live": False,
        "would_write_business": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "would_use_raw_tmux": False,
        "would_use_direct_tui": False,
        "would_use_direct_adapter": False,
        "would_use_rmux": False,
        "source_package_kind": str(prep.get("package_kind", "")),
        "blocked_reasons": blocked_reasons,
    }
    if package_dir:
        _write_package_dir(
            Path(package_dir),
            {
                "role_task_package.json": role_package,
                "coder_task.json": coder_task,
                "reviewer_task.json": reviewer_task,
                "gate_task.json": gate_task,
                "shared_evidence.json": shared_evidence,
            },
        )
    return role_package


def build_sandbox_role_live_smoke_package(request: dict[str, Any]) -> dict[str, Any]:
    """Build a no-live package for a sandbox Coder/Reviewer/Gate smoke."""

    case_id = str(request.get("case_id") or "sandbox-role-live-smoke").strip()
    control_root = str(request.get("control_root") or runtime_root()).strip()
    repository_root = str(request.get("repository_root") or "").strip()
    artifact_root_raw = str(request.get("artifact_root") or "").strip()
    coder_id = str(request.get("coder_worker_id") or "").strip()
    reviewer_id = str(request.get("reviewer_worker_id") or "").strip()
    gate_id = str(request.get("gate_worker_id") or "").strip()
    allowed_file = str(request.get("allowed_file") or "").strip()
    output_dir = str(request.get("output_dir") or "").strip()
    created_at = str(request.get("created_at") or "").strip()
    timeout_seconds = int(request.get("timeout_seconds") or 240)
    required_markers = [str(item) for item in _list(request.get("required_markers"))]
    blocked_reasons: list[str] = []
    verification_commands = [str(item).strip() for item in _list(request.get("verification_commands"))]
    verification_argv: list[list[str]] = []
    for command in verification_commands:
        try:
            argv = shlex.split(command)
        except ValueError:
            blocked_reasons.append("verification_commands must be valid argv")
            continue
        if not argv:
            blocked_reasons.append("verification_commands must be non-empty argv")
            continue
        verification_argv.append(argv)
    safe_test_tmpdir_authorized = request.get("safe_test_tmpdir_authorized") is True
    if not required_markers and coder_id:
        required_markers = [
            case_id,
            f"coder_worker_id: {coder_id}",
            f"allowed_write_scope: {case_id}",
        ]

    if not case_id:
        blocked_reasons.append("case_id is required")
    if not repository_root:
        blocked_reasons.append("repository_root is required")
    if not allowed_file:
        blocked_reasons.append("allowed_file is required")
    role_ids = [role_id for role_id in (coder_id, reviewer_id, gate_id) if role_id]
    if not coder_id:
        blocked_reasons.append("coder_worker_id is required")
    if not reviewer_id:
        blocked_reasons.append("reviewer_worker_id is required")
    if not gate_id:
        blocked_reasons.append("gate_worker_id is required")
    if len(set(role_ids)) != len(role_ids):
        blocked_reasons.append("role workers must be distinct")
    if verification_commands and not safe_test_tmpdir_authorized:
        blocked_reasons.append("verification_commands require safe_test_tmpdir_authorized=true")
    if any("TMPDIR" in argument for argv in verification_argv for argument in argv):
        blocked_reasons.append("verification_commands must not set TMPDIR")
    if any(FORBIDDEN_REVIEW_TMPDIR_ROOT in argument for argv in verification_argv for argument in argv):
        blocked_reasons.append(f"verification_commands must not reference {FORBIDDEN_REVIEW_TMPDIR_ROOT}")
    path_contract = _sandbox_role_allowed_file_contract(
        repository_root=repository_root,
        artifact_root=artifact_root_raw,
        allowed_file=allowed_file,
        blocked_reasons=blocked_reasons,
    )
    artifact_root = path_contract["artifact_root"]
    allowed_file = path_contract["allowed_file"]
    allowed_file_absolute = path_contract["allowed_file_absolute"]

    package_dir = str(Path(output_dir).expanduser().resolve()) if output_dir else ""
    package_files = dict(SANDBOX_ROLE_LIVE_SMOKE_PACKAGE_FILES)
    role_assignments = {
        "coder": coder_id,
        "reviewer": reviewer_id,
        "gate": gate_id,
    }
    role_contract = {
        "schema_version": 1,
        "contract_kind": "sandbox-role-live-smoke",
        "case_id": case_id,
        "created_at": created_at,
        "control_root": control_root,
        "repository_root": repository_root,
        "artifact_root": artifact_root,
        "workers": role_assignments,
        "allowed_files": [allowed_file] if allowed_file else [],
        "allowed_file_absolute": allowed_file_absolute,
        "required_markers": required_markers,
        "reviewer_read_only": True,
        "gate_read_only": True,
        "business_write_authorized": False,
        "git_index_write_authorized": False,
        "verification": {
            "commands": verification_argv,
            "safe_test_tmpdir_authorized": safe_test_tmpdir_authorized,
            "tmpdir_template": SAFE_REVIEW_TMPDIR_TEMPLATE if verification_commands else "",
            "environment_scope": "command-only" if verification_commands else "",
            "cleanup_required": bool(verification_commands),
        },
    }
    artifact_template = {
        "schema_version": 1,
        "artifact_kind": "sandbox-role-run-artifact",
        "case_id": case_id,
        "task_id": "task-001",
        "created_at": created_at,
        "root": control_root,
        "repository_root": repository_root,
        "artifact_root": artifact_root,
        "allowed_files": [allowed_file] if allowed_file else [],
        "allowed_file_absolute": allowed_file_absolute,
        "actual_files": [allowed_file] if allowed_file else [],
        "required_markers": required_markers,
        "verification": role_contract["verification"],
        "actual_markers": required_markers,
        "role_assignments": role_assignments,
        "worker_results": [
            {
                "worker_id": coder_id,
                "visible_run_id": "<coder-visible-run-id>",
                "visible_task_id": "task-001",
                "state": "completed",
            },
            {
                "worker_id": reviewer_id,
                "visible_run_id": "<reviewer-visible-run-id>",
                "visible_task_id": "task-001",
                "state": "completed",
            },
            {
                "worker_id": gate_id,
                "visible_run_id": "<gate-visible-run-id>",
                "visible_task_id": "task-001",
                "state": "completed",
            },
        ],
    }
    run_summary_template = {
        "schema_version": 1,
        "artifact_kind": "sandbox-role-live-smoke-run-summary",
        "case_id": case_id,
        "roles": {
            "coder": {"worker_id": coder_id, "run_id": "<coder-visible-run-id>", "task_id": "task-001"},
            "reviewer": {"worker_id": reviewer_id, "run_id": "<reviewer-visible-run-id>", "task_id": "task-001"},
            "gate": {"worker_id": gate_id, "run_id": "<gate-visible-run-id>", "task_id": "task-001"},
        },
        "artifact_closeout": {
            "ok": False,
            "closeout_decision": "human_review_required",
            "blocked_reasons": ["template must be filled from real run evidence"],
        },
    }
    prompt_context = {
        "case_id": case_id,
        "control_root": control_root,
        "repository_root": repository_root,
        "artifact_root": artifact_root,
        "coder_id": coder_id,
        "reviewer_id": reviewer_id,
        "gate_id": gate_id,
        "allowed_file": allowed_file,
        "allowed_file_absolute": allowed_file_absolute,
        "required_markers": required_markers,
        "verification": role_contract["verification"],
    }
    prompts = {
        "coder-prompt.md": _sandbox_role_coder_prompt(prompt_context),
        "reviewer-prompt.md": _sandbox_role_reviewer_prompt(prompt_context),
        "gate-prompt.md": _sandbox_role_gate_prompt(prompt_context),
    }
    file_refs = {key: _package_file_reference(package_dir, filename) for key, filename in package_files.items()}
    manual_commands = {
        "coder": _manual_run_one_command(control_root, coder_id, "Coder", file_refs["coder_prompt"], timeout_seconds),
        "reviewer": _manual_run_one_command(control_root, reviewer_id, "Reviewer", file_refs["reviewer_prompt"], timeout_seconds),
        "gate": _manual_run_one_command(control_root, gate_id, "Gate", file_refs["gate_prompt"], timeout_seconds),
        "closeout": (
            "./bin/omx_worker_backend closeout-sandbox-role-run-artifact "
            f"--artifact {shlex.quote(file_refs['role_run_artifact_template'])} --json"
        ),
    }
    blocked_reasons = _dedupe(blocked_reasons)
    if package_dir and not blocked_reasons:
        _write_smoke_package_dir(
            Path(package_dir),
            {
                "role-contract.json": role_contract,
                "coder-prompt.md": prompts["coder-prompt.md"],
                "reviewer-prompt.md": prompts["reviewer-prompt.md"],
                "gate-prompt.md": prompts["gate-prompt.md"],
                "role-run-artifact.template.json": artifact_template,
                "run-summary.template.json": run_summary_template,
            },
        )
    return {
        "schema_version": 1,
        "package_kind": "sandbox-role-live-smoke-package",
        "advisory_only": True,
        "ok": not blocked_reasons,
        "case_id": case_id,
        "control_root": control_root,
        "repository_root": repository_root,
        "artifact_root": artifact_root,
        "package_dir": package_dir,
        "package_files": package_files,
        "role_assignments": role_assignments,
        "allowed_files": [allowed_file] if allowed_file else [],
        "allowed_file_absolute": allowed_file_absolute,
        "required_markers": required_markers,
        "manual_commands": manual_commands,
        "blocked_reasons": blocked_reasons,
        "safe_next_actions": (
            ["operator may run role prompts through visible dispatch and fill the artifact template"]
            if not blocked_reasons
            else ["fix blocked_reasons before live dispatch"]
        ),
        "would_execute_live": False,
        "would_dispatch_workers": False,
        "would_write_business": False,
        "would_release_locks": False,
        "would_write_terminal_markers": False,
        "would_use_raw_tmux": False,
        "would_use_direct_tui": False,
        "would_use_direct_adapter": False,
        "would_use_rmux": False,
    }


def build_sandbox_role_live_smoke_runner_plan(request: dict[str, Any]) -> dict[str, Any]:
    """Build a no-live execution plan from a sandbox role smoke package."""

    package_dir_raw = str(request.get("package_dir") or "").strip()
    output_dir_raw = str(request.get("output_dir") or "").strip()
    requested_allow_real = bool(request.get("allow_real"))
    package_dir = Path(package_dir_raw).expanduser().resolve() if package_dir_raw else None
    output_dir = Path(output_dir_raw).expanduser().resolve() if output_dir_raw else None
    blocked_reasons: list[str] = []
    contract: dict[str, Any] = {}

    if package_dir is None:
        blocked_reasons.append("package_dir is required")
    elif not package_dir.is_dir():
        blocked_reasons.append(f"package_dir is not a directory: {package_dir}")
    else:
        for label, filename in SANDBOX_ROLE_LIVE_SMOKE_PACKAGE_FILES.items():
            path = package_dir / filename
            if not path.is_file():
                blocked_reasons.append(f"{label} package file is required: {filename}")
        contract_path = package_dir / SANDBOX_ROLE_LIVE_SMOKE_PACKAGE_FILES["role_contract"]
        if contract_path.is_file():
            try:
                contract = _read_json(contract_path)
            except (OSError, json.JSONDecodeError) as exc:
                blocked_reasons.append(f"role-contract.json is invalid JSON: {exc}")

    if contract:
        if contract.get("contract_kind") != "sandbox-role-live-smoke":
            blocked_reasons.append("role-contract.contract_kind must be sandbox-role-live-smoke")
        if contract.get("business_write_authorized") is not False:
            blocked_reasons.append("role-contract.business_write_authorized must be false")
        if contract.get("git_index_write_authorized") is not False:
            blocked_reasons.append("role-contract.git_index_write_authorized must be false")
        if contract.get("reviewer_read_only") is not True:
            blocked_reasons.append("role-contract.reviewer_read_only must be true")
        if contract.get("gate_read_only") is not True:
            blocked_reasons.append("role-contract.gate_read_only must be true")

    workers = _mapping(contract.get("workers"))
    coder_id = str(workers.get("coder") or "").strip()
    reviewer_id = str(workers.get("reviewer") or "").strip()
    gate_id = str(workers.get("gate") or "").strip()
    role_ids = [role_id for role_id in (coder_id, reviewer_id, gate_id) if role_id]
    if contract:
        if not coder_id:
            blocked_reasons.append("role-contract.workers.coder is required")
        if not reviewer_id:
            blocked_reasons.append("role-contract.workers.reviewer is required")
        if not gate_id:
            blocked_reasons.append("role-contract.workers.gate is required")
        if len(role_ids) != len(set(role_ids)):
            blocked_reasons.append("role-contract workers must be distinct")

    package_root = package_dir or Path()
    output_root = output_dir or package_root
    allowed_files = [str(item) for item in _list(contract.get("allowed_files"))]
    allowed_file_absolute = str(contract.get("allowed_file_absolute") or "").strip()
    artifact_root = str(contract.get("artifact_root") or "").strip()
    required_markers = [str(item) for item in _list(contract.get("required_markers"))]
    case_id = str(contract.get("case_id") or "sandbox-role-live-smoke").strip()
    control_root = str(contract.get("control_root") or runtime_root()).strip()
    repository_root = str(contract.get("repository_root") or "").strip()
    steps = [
        _sandbox_role_runner_step(
            role="coder",
            worker_id=coder_id,
            prompt_path=package_root / SANDBOX_ROLE_LIVE_SMOKE_PACKAGE_FILES["coder_prompt"],
            postcheck_path=output_root / "coder-postcheck.json",
            read_only=False,
            depends_on=[],
            allowed_files=allowed_files,
        ),
        _sandbox_role_runner_step(
            role="reviewer",
            worker_id=reviewer_id,
            prompt_path=package_root / SANDBOX_ROLE_LIVE_SMOKE_PACKAGE_FILES["reviewer_prompt"],
            postcheck_path=output_root / "reviewer-postcheck.json",
            read_only=True,
            depends_on=["coder"],
            allowed_files=[],
        ),
        _sandbox_role_runner_step(
            role="gate",
            worker_id=gate_id,
            prompt_path=package_root / SANDBOX_ROLE_LIVE_SMOKE_PACKAGE_FILES["gate_prompt"],
            postcheck_path=output_root / "gate-postcheck.json",
            read_only=True,
            depends_on=["coder", "reviewer"],
            allowed_files=[],
        ),
    ]
    blocked_reasons = _dedupe(blocked_reasons)
    plan = {
        "schema_version": 1,
        "plan_kind": "sandbox-role-live-smoke-runner-plan",
        "advisory_only": True,
        "ok": not blocked_reasons,
        "case_id": case_id,
        "control_root": control_root,
        "repository_root": repository_root,
        "artifact_root": artifact_root,
        "package_dir": str(package_root) if package_dir is not None else "",
        "output_dir": str(output_root) if output_dir is not None else "",
        "requested_allow_real": requested_allow_real,
        "role_assignments": {
            "coder": coder_id,
            "reviewer": reviewer_id,
            "gate": gate_id,
        },
        "allowed_files": allowed_files,
        "allowed_file_absolute": allowed_file_absolute,
        "required_markers": required_markers,
        "steps": steps,
        "runner_plan_path": str(output_root / "runner-plan.json"),
        "role_run_artifact_path": str(output_root / "role-run-artifact.json"),
        "run_summary_path": str(output_root / "run-summary.json"),
        "blocked_reasons": blocked_reasons,
        "safe_next_actions": (
            ["operator may run the planned roles through a controlled live runner"]
            if not blocked_reasons
            else ["fix blocked_reasons before live dispatch"]
        ),
        "would_execute_live": False,
        "would_dispatch_workers": False,
        "would_write_business": False,
        "would_release_locks": False,
        "would_write_terminal_markers": False,
        "would_use_raw_tmux": False,
        "would_use_direct_tui": False,
        "would_use_direct_adapter": False,
        "would_use_rmux": False,
    }
    if output_dir is not None:
        _write_smoke_package_dir(output_root, {"runner-plan.json": plan})
    return plan


def execute_sandbox_role_live_smoke_runner(
    request: dict[str, Any],
    *,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Execute a controlled sandbox role smoke from a runner plan."""

    allow_real = bool(request.get("allow_real"))
    runner_plan = build_sandbox_role_live_smoke_runner_plan(request)
    output_dir_raw = str(runner_plan.get("output_dir") or request.get("output_dir") or "").strip()
    output_dir = Path(output_dir_raw).expanduser().resolve() if output_dir_raw else Path.cwd().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    blocked_reasons = [str(item) for item in _list(runner_plan.get("blocked_reasons"))]
    if not allow_real:
        blocked_reasons.append("run-sandbox-role-live-smoke-package requires --allow-real for live worker dispatch")
    if blocked_reasons:
        return _sandbox_role_runner_result(
            runner_plan=runner_plan,
            output_dir=output_dir,
            role_results=[],
            closeout={},
            blocked_reasons=_dedupe(blocked_reasons),
            live_run_allowed=False,
            dispatch_attempted=False,
        )

    timeout_seconds = int(request.get("timeout_seconds") or request.get("timeout") or 240)
    dispatch_bin = Path(
        str(request.get("dispatch_bin") or Path(str(runner_plan.get("repository_root") or ".")) / "bin" / "omx_visible_dispatch")
    ).expanduser()
    runner = command_runner or _run_command
    role_results: list[dict[str, Any]] = []
    prior_evidence: list[dict[str, Any]] = []
    actual_files: list[str] = []
    actual_markers: list[str] = []
    for step in _list(runner_plan.get("steps")):
        step_result = _execute_sandbox_role_step(
            runner_plan=runner_plan,
            step=_mapping(step),
            dispatch_bin=dispatch_bin,
            runner=runner,
            timeout_seconds=timeout_seconds,
            prior_evidence=prior_evidence,
        )
        role_results.append(step_result)
        prior_evidence.append(
            {
                "role": step_result.get("role", ""),
                "worker_id": step_result.get("worker_id", ""),
                "postcheck_path": step_result.get("postcheck_path", ""),
                "ok": bool(step_result.get("ok")),
                "blocked_reasons": _list(step_result.get("blocked_reasons")),
            }
        )
        if str(step_result.get("role")) == "coder":
            actual_files = [str(item) for item in _list(step_result.get("actual_files"))]
            actual_markers = [str(item) for item in _list(step_result.get("actual_markers"))]
        if not step_result.get("ok"):
            break

    blocked_reasons = _dedupe([reason for result in role_results for reason in _list(result.get("blocked_reasons"))])
    artifact = _sandbox_role_execution_artifact(runner_plan, role_results, actual_files, actual_markers)
    artifact_path = Path(str(runner_plan.get("role_run_artifact_path") or output_dir / "role-run-artifact.json")).expanduser().resolve()
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    closeout = closeout_sandbox_role_run_from_run_artifact(artifact) if not blocked_reasons else {}
    blocked_reasons = _dedupe(blocked_reasons + [str(item) for item in _list(closeout.get("blocked_reasons"))])
    result = _sandbox_role_runner_result(
        runner_plan=runner_plan,
        output_dir=output_dir,
        role_results=role_results,
        closeout=closeout,
        blocked_reasons=blocked_reasons,
        live_run_allowed=True,
        dispatch_attempted=bool(role_results),
    )
    Path(str(runner_plan.get("run_summary_path") or output_dir / "run-summary.json")).expanduser().resolve().write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return result


def validate_role_task_package(package_dir: Path | str) -> dict[str, Any]:
    root = Path(package_dir).expanduser().resolve()
    blockers: list[str] = []
    payloads: dict[str, dict[str, Any]] = {}
    for filename in ROLE_TASK_PACKAGE_FILES:
        path = root / filename
        if not path.exists():
            blockers.append(f"{filename} is required")
            continue
        try:
            payloads[filename] = _read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            blockers.append(f"{filename} is invalid JSON: {exc}")

    role_package = payloads.get("role_task_package.json", {})
    coder = payloads.get("coder_task.json", {})
    reviewer = payloads.get("reviewer_task.json", {})
    gate = payloads.get("gate_task.json", {})
    evidence = payloads.get("shared_evidence.json", {})

    if role_package.get("package_kind") != "role-task-package-prep":
        blockers.append("role_task_package.package_kind must be role-task-package-prep")
    created_at_blocker = prep_created_at_blocker(str(role_package.get("created_at", "")), label="role_task_package")
    if created_at_blocker:
        blockers.append(created_at_blocker)
    for key in (
        "would_execute_live",
        "would_write_business",
        "would_write_registry",
        "would_write_state_team",
        "would_use_raw_tmux",
        "would_use_direct_tui",
        "would_use_direct_adapter",
        "would_use_rmux",
    ):
        if role_package.get(key) is not False:
            blockers.append(f"role_task_package.{key} must be false")
    if coder.get("package_kind") != "coder-task-contract":
        blockers.append("coder_task.package_kind must be coder-task-contract")
    if reviewer.get("package_kind") != "reviewer-task-contract":
        blockers.append("reviewer_task.package_kind must be reviewer-task-contract")
    if gate.get("package_kind") != "gate-task-contract":
        blockers.append("gate_task.package_kind must be gate-task-contract")
    if evidence.get("package_kind") != "shared-role-evidence-bundle":
        blockers.append("shared_evidence.package_kind must be shared-role-evidence-bundle")
    coder_id = str(coder.get("worker_id", "")).strip()
    reviewer_id = str(reviewer.get("worker_id", "")).strip()
    if not coder_id:
        blockers.append("coder_task.worker_id is required")
    if not reviewer_id:
        blockers.append("reviewer_task.worker_id is required")
    if coder_id and reviewer_id and coder_id == reviewer_id:
        blockers.append("coder and reviewer must be different workers")
    coder_allowed_files = _list(coder.get("allowed_files"))
    allowed_workers = _list(role_package.get("profile_allowed_workers"))
    if allowed_workers:
        if coder_id and coder_id not in allowed_workers:
            blockers.append(f"coder must be listed in profile_allowed_workers: {coder_id}")
        if reviewer_id and reviewer_id not in allowed_workers:
            blockers.append(f"reviewer must be listed in profile_allowed_workers: {reviewer_id}")
    baseline = _mapping(reviewer.get("baseline_delta_evidence"))
    if not baseline:
        blockers.append("reviewer_task.baseline_delta_evidence is required")
    else:
        blockers.extend(
            baseline_delta_evidence_blockers(
                baseline,
                coder_allowed_files,
                prefix="reviewer_task.baseline_delta_evidence",
                expected_label="coder_task.allowed_files",
            )
        )
    gate_commands = _list(gate.get("validation_commands"))
    if not any("validate-business-write-scope" in str(command) for command in gate_commands):
        blockers.append("gate_task.validation_commands must include validate-business-write-scope")
    if not any("validate-business-baseline-delta" in str(command) for command in gate_commands):
        blockers.append("gate_task.validation_commands must include validate-business-baseline-delta")
    if gate.get("advisory_only") is not True:
        blockers.append("gate_task.advisory_only must be true")
    if evidence.get("advisory_only") is not True:
        blockers.append("shared_evidence.advisory_only must be true")
    blockers.extend(_list(role_package.get("blocked_reasons")))
    blocked_reasons = _dedupe(blockers)
    return {
        "schema_version": 1,
        "validation_kind": "role-task-package",
        "ok": not blocked_reasons,
        "package_dir": str(root),
        "package_files": sorted(payloads.keys()),
        "coder_worker_id": coder_id,
        "reviewer_worker_id": reviewer_id,
        "blocked_reasons": blocked_reasons,
    }


def validate_role_task_cycle_ready(package_dir: Path | str) -> dict[str, Any]:
    root = Path(package_dir).expanduser().resolve()
    blockers: list[str] = []
    package_validation = validate_role_task_package(root)
    if not package_validation.get("ok"):
        blockers.append("role task package validation must pass")
        blockers.extend(_list(package_validation.get("blocked_reasons")))
    evidence_path = root / "shared_evidence.json"
    try:
        evidence = _read_json(evidence_path)
    except (OSError, json.JSONDecodeError) as exc:
        evidence = {}
        blockers.append(f"shared_evidence.json is invalid JSON: {exc}")
    aggregate = _mapping(evidence.get("aggregate_result"))
    role_rounds = _list(evidence.get("role_rounds"))
    if aggregate.get("all_workers_terminal") is not True:
        blockers.append("writer_review_cycle_incomplete")
    if not role_rounds:
        blockers.append("shared_evidence.role_rounds is required")
    for role_round in role_rounds:
        if not isinstance(role_round, dict):
            blockers.append("shared_evidence.role_rounds entries must be objects")
            continue
        terminal_state = str(role_round.get("terminal_state", "")).strip()
        if terminal_state in {"", "pending", "running"}:
            blockers.append("writer_review_cycle_incomplete")
            break
    blocked_reasons = _dedupe(blockers)
    return {
        "schema_version": 1,
        "validation_kind": "role-task-cycle-ready",
        "ok": not blocked_reasons,
        "package_dir": str(root),
        "coder_worker_id": str(package_validation.get("coder_worker_id", "")),
        "reviewer_worker_id": str(package_validation.get("reviewer_worker_id", "")),
        "blocked_reasons": blocked_reasons,
    }


def evaluate_sandbox_role_harness(evidence: dict[str, Any]) -> dict[str, Any]:
    """Evaluate no-live Coder/Reviewer/Gate sandbox evidence."""

    case_id = str(evidence.get("case_id", "") or "sandbox-role-harness")
    allowed_files = [str(item) for item in _list(evidence.get("allowed_files"))]
    actual_files = [str(item) for item in _list(evidence.get("actual_files"))]
    required_markers = [str(item) for item in _list(evidence.get("required_markers"))]
    actual_markers = [str(item) for item in _list(evidence.get("actual_markers"))]
    blocked_reasons: list[str] = []

    allowed_file_set = set(allowed_files)
    for actual in actual_files:
        if actual not in allowed_file_set:
            blocked_reasons.append(f"actual file outside allowed sandbox files: {actual}")
    for marker in required_markers:
        if marker not in actual_markers:
            blocked_reasons.append(f"missing required marker: {marker}")

    role_lanes = _sandbox_role_lanes(_mapping(evidence.get("roles")))
    gate_decision = build_gate_decision(
        run_id=case_id,
        task_id=str(evidence.get("task_id", "task-001") or "task-001"),
        role_lanes=role_lanes,
        created_at=str(evidence.get("created_at", "")),
    )
    protocol_decision = str(gate_decision.get("decision", "human_review_required"))
    harness_decision = protocol_decision
    if blocked_reasons and harness_decision == "approve":
        harness_decision = "human_review_required"
    ok = not blocked_reasons and harness_decision == "approve"
    return {
        "schema_version": 1,
        "evaluation_kind": "sandbox-role-harness",
        "advisory_only": True,
        "case_id": case_id,
        "ok": ok,
        "harness_decision": harness_decision,
        "gate_decision": gate_decision,
        "allowed_files": allowed_files,
        "actual_files": actual_files,
        "required_markers": required_markers,
        "actual_markers": actual_markers,
        "role_lanes": role_lanes,
        "blocked_reasons": _dedupe(blocked_reasons),
        "would_execute_live": False,
        "would_write_business": False,
        "would_dispatch_workers": False,
        "would_release_locks": False,
        "would_write_terminal_markers": False,
    }


def build_sandbox_role_run_spec_from_dispatch_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Build a sandbox role-run spec from visible dispatch metadata."""

    blocked_reasons: list[str] = []
    root_raw = str(metadata.get("root", "")).strip()
    root = Path(root_raw).expanduser().resolve() if root_raw else None
    if root is None:
        blocked_reasons.append("root is required")
    roles = _mapping(metadata.get("roles"))
    spec_roles: dict[str, dict[str, Any]] = {}
    for role in ("coder", "reviewer", "gate"):
        spec_roles[role] = _role_run_spec_from_dispatch_metadata(role, _mapping(roles.get(role)), root, blocked_reasons)
    spec = {
        "schema_version": 1,
        "spec_kind": "sandbox-role-run-spec",
        "case_id": str(metadata.get("case_id", "") or "sandbox-role-run"),
        "task_id": str(metadata.get("task_id", "task-001") or "task-001"),
        "created_at": str(metadata.get("created_at", "")),
        "allowed_files": [str(item) for item in _list(metadata.get("allowed_files"))],
        "actual_files": [str(item) for item in _list(metadata.get("actual_files"))],
        "required_markers": [str(item) for item in _list(metadata.get("required_markers"))],
        "actual_markers": [str(item) for item in _list(metadata.get("actual_markers"))],
        "roles": spec_roles,
    }
    return {
        "schema_version": 1,
        "build_kind": "sandbox-role-run-spec-from-dispatch-metadata",
        "advisory_only": True,
        "ok": not blocked_reasons,
        "root": str(root) if root is not None else "",
        "spec": spec,
        "blocked_reasons": _dedupe(blocked_reasons),
        "would_execute_live": False,
        "would_write_business": False,
        "would_dispatch_workers": False,
        "would_release_locks": False,
        "would_write_terminal_markers": False,
    }


def collect_sandbox_role_harness_evidence(spec: dict[str, Any]) -> dict[str, Any]:
    """Collect no-live Coder/Reviewer/Gate evidence from supplied task dirs."""

    blocked_reasons: list[str] = []
    roles = _mapping(spec.get("roles"))
    collected_roles: dict[str, dict[str, Any]] = {}
    for role in ("coder", "reviewer", "gate"):
        collected_roles[role] = _collect_role_task_evidence(role, _mapping(roles.get(role)), blocked_reasons)
    evidence = {
        "case_id": str(spec.get("case_id", "") or "sandbox-role-harness"),
        "task_id": str(spec.get("task_id", "task-001") or "task-001"),
        "created_at": str(spec.get("created_at", "")),
        "allowed_files": [str(item) for item in _list(spec.get("allowed_files"))],
        "actual_files": [str(item) for item in _list(spec.get("actual_files"))],
        "required_markers": [str(item) for item in _list(spec.get("required_markers"))],
        "actual_markers": [str(item) for item in _list(spec.get("actual_markers"))],
        "roles": collected_roles,
    }
    return {
        "schema_version": 1,
        "collection_kind": "sandbox-role-harness-evidence",
        "advisory_only": True,
        "ok": not blocked_reasons,
        "evidence": evidence,
        "blocked_reasons": _dedupe(blocked_reasons),
        "would_execute_live": False,
        "would_write_business": False,
        "would_dispatch_workers": False,
        "would_release_locks": False,
        "would_write_terminal_markers": False,
    }


def evaluate_sandbox_role_run(spec: dict[str, Any]) -> dict[str, Any]:
    """Collect and evaluate no-live sandbox role-run evidence."""

    collection = collect_sandbox_role_harness_evidence(spec)
    evidence = _mapping(collection.get("evidence"))
    evaluation = evaluate_sandbox_role_harness(evidence)
    blocked_reasons = _dedupe(
        [str(item) for item in _list(collection.get("blocked_reasons"))]
        + [str(item) for item in _list(evaluation.get("blocked_reasons"))]
        + [str(item) for item in _list(_mapping(evaluation.get("gate_decision")).get("known_gaps"))]
    )
    ok = bool(collection.get("ok")) and bool(evaluation.get("ok"))
    return {
        "schema_version": 1,
        "evaluation_kind": "sandbox-role-run",
        "advisory_only": True,
        "ok": ok,
        "harness_decision": str(evaluation.get("harness_decision", "human_review_required")),
        "collection": collection,
        "evaluation": evaluation,
        "blocked_reasons": blocked_reasons,
        "would_execute_live": False,
        "would_write_business": False,
        "would_dispatch_workers": False,
        "would_release_locks": False,
        "would_write_terminal_markers": False,
    }


def closeout_sandbox_role_run_from_dispatch_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Build, collect, and evaluate a sandbox role-run from dispatch metadata."""

    spec_build = build_sandbox_role_run_spec_from_dispatch_metadata(metadata)
    spec = _mapping(spec_build.get("spec"))
    evaluation = evaluate_sandbox_role_run(spec) if spec_build.get("ok") and spec else {}
    blocked_reasons = _dedupe(
        [str(item) for item in _list(spec_build.get("blocked_reasons"))]
        + [str(item) for item in _list(evaluation.get("blocked_reasons"))]
    )
    closeout_decision = (
        str(evaluation.get("harness_decision", "human_review_required"))
        if spec_build.get("ok")
        else "human_review_required"
    )
    ok = bool(spec_build.get("ok")) and bool(evaluation.get("ok"))
    return {
        "schema_version": 1,
        "closeout_kind": "sandbox-role-run-dispatch-metadata",
        "advisory_only": True,
        "ok": ok,
        "closeout_decision": closeout_decision,
        "spec_build": spec_build,
        "evaluation": evaluation,
        "blocked_reasons": blocked_reasons,
        "safe_next_actions": (
            ["operator may inspect advisory closeout before broader role runs"]
            if ok
            else ["inspect blocked_reasons and role artifacts before retry"]
        ),
        "would_execute_live": False,
        "would_write_business": False,
        "would_dispatch_workers": False,
        "would_release_locks": False,
        "would_write_terminal_markers": False,
    }


def build_sandbox_role_dispatch_metadata_from_run_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Build dispatch metadata from a role-run artifact or execution result."""

    blocked_reasons: list[str] = []
    root_raw = str(artifact.get("root") or artifact.get("control_root") or artifact.get("runtime_root") or "").strip()
    if not root_raw:
        blocked_reasons.append("root is required")
    metadata_roles: dict[str, dict[str, Any]] = {}
    for role in ("coder", "reviewer", "gate"):
        role_payload = _role_run_artifact_payload(role, artifact)
        if not role_payload:
            blocked_reasons.append(f"{role} role result is required")
            metadata_roles[role] = {"worker_id": "", "run_id": "", "task_id": ""}
            continue
        metadata_roles[role] = _role_dispatch_metadata_from_artifact_payload(role, role_payload, blocked_reasons)
    metadata = {
        "schema_version": 1,
        "metadata_kind": "sandbox-role-dispatch-metadata",
        "case_id": str(artifact.get("case_id") or artifact.get("run_id") or artifact.get("effective_run_id") or "sandbox-role-run"),
        "task_id": str(artifact.get("task_id") or "task-001"),
        "created_at": str(artifact.get("created_at", "")),
        "root": root_raw,
        "allowed_files": [str(item) for item in _list(artifact.get("allowed_files"))],
        "actual_files": [str(item) for item in _list(artifact.get("actual_files"))],
        "required_markers": [str(item) for item in _list(artifact.get("required_markers"))],
        "actual_markers": [str(item) for item in _list(artifact.get("actual_markers"))],
        "roles": metadata_roles,
    }
    return {
        "schema_version": 1,
        "build_kind": "sandbox-role-dispatch-metadata-from-run-artifact",
        "advisory_only": True,
        "ok": not blocked_reasons,
        "metadata": metadata,
        "blocked_reasons": _dedupe(blocked_reasons),
        "would_execute_live": False,
        "would_write_business": False,
        "would_dispatch_workers": False,
        "would_release_locks": False,
        "would_write_terminal_markers": False,
    }


def closeout_sandbox_role_run_from_run_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Build metadata and close out a sandbox role-run from one artifact."""

    metadata_build = build_sandbox_role_dispatch_metadata_from_run_artifact(artifact)
    metadata = _mapping(metadata_build.get("metadata"))
    closeout = closeout_sandbox_role_run_from_dispatch_metadata(metadata) if metadata_build.get("ok") and metadata else {}
    blocked_reasons = _dedupe(
        [str(item) for item in _list(metadata_build.get("blocked_reasons"))]
        + [str(item) for item in _list(closeout.get("blocked_reasons"))]
    )
    closeout_decision = (
        str(closeout.get("closeout_decision", "human_review_required"))
        if metadata_build.get("ok")
        else "human_review_required"
    )
    ok = bool(metadata_build.get("ok")) and bool(closeout.get("ok"))
    return {
        "schema_version": 1,
        "closeout_kind": "sandbox-role-run-artifact",
        "advisory_only": True,
        "ok": ok,
        "closeout_decision": closeout_decision,
        "metadata_build": metadata_build,
        "closeout": closeout,
        "blocked_reasons": blocked_reasons,
        "safe_next_actions": (
            ["operator may inspect advisory artifact closeout before broader role runs"]
            if ok
            else ["inspect blocked_reasons, role assignments, and role artifacts before retry"]
        ),
        "would_execute_live": False,
        "would_write_business": False,
        "would_dispatch_workers": False,
        "would_release_locks": False,
        "would_write_terminal_markers": False,
    }


def _coder_task(prep: dict[str, Any], worker_id: str) -> dict[str, Any]:
    writer = _mapping(prep.get("writer"))
    return {
        "schema_version": 1,
        "package_kind": "coder-task-contract",
        "worker_role": "coder",
        "worker_id": worker_id,
        "source_role": str(writer.get("role", "")),
        "would_execute_live": False,
        "would_write_business": False,
        "allowed_files": _list(writer.get("allowed_files")),
        "allowed_dirs": _list(writer.get("allowed_dirs")),
        "forbidden_files": _list(writer.get("forbidden_files")),
        "forbidden_dirs": _list(writer.get("forbidden_dirs")),
        "max_files_changed": int(writer.get("max_files_changed", 0) or 0),
        "prompt": str(writer.get("prompt", "")),
        "completion_rule": "non-empty report.md plus exactly one .done/.fail/.blocked",
        "forbidden_actions": [
            "do not self-review",
            "do not write outside the contract",
            "do not touch git index unless explicitly authorized",
            "do not write .omx/state/team",
            "do not use raw tmux, direct TUI, direct adapter, or rmux",
        ],
    }


def _reviewer_task(prep: dict[str, Any], worker_id: str) -> dict[str, Any]:
    reviewer = _mapping(prep.get("reviewer"))
    return {
        "schema_version": 1,
        "package_kind": "reviewer-task-contract",
        "worker_role": "reviewer",
        "worker_id": worker_id,
        "source_role": str(reviewer.get("role", "")),
        "would_execute_live": False,
        "would_write_business": False,
        "baseline_delta_evidence": _mapping(reviewer.get("baseline_delta_evidence")),
        "prompt": str(reviewer.get("prompt", "")),
        "completion_rule": "non-empty report.md plus exactly one .done/.fail/.blocked",
        "forbidden_actions": [
            "do not modify code or business files",
            "do not rerun the coder",
            "do not change terminal truth",
            "do not accept dirty baselines without baseline-delta evidence",
        ],
    }


def _gate_task(prep: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "package_kind": "gate-task-contract",
        "worker_role": "gate",
        "advisory_only": True,
        "would_execute_live": False,
        "would_write_business": False,
        "validation_commands": _list(prep.get("post_write_gate_commands")),
        "allowed_decisions": ["approve", "block", "rerun_coder", "human_review_required"],
        "forbidden_actions": [
            "do not dispatch workers",
            "do not release locks",
            "do not write terminal markers",
            "do not authorize business writes",
        ],
    }


def _shared_evidence(prep: dict[str, Any], coder_id: str, reviewer_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "package_kind": "shared-role-evidence-bundle",
        "advisory_only": True,
        "profile_id": str(prep.get("profile_id", "")),
        "task_id": str(prep.get("task_id", "")),
        "role_rounds": [
            {"role": "coder", "worker_id": coder_id, "terminal_state": "pending"},
            {"role": "reviewer", "worker_id": reviewer_id, "terminal_state": "pending"},
        ],
        "gate_results": [],
        "aggregate_result": {
            "all_workers_terminal": False,
            "all_gates_ok": False,
            "leader_suggested_action": "human_review_required",
        },
        "safety_notes": [
            "advisory evidence bundle only",
            "does not dispatch workers, release locks, or write protocol markers",
        ],
    }


def _sandbox_role_lanes(roles: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for role in ("coder", "reviewer", "gate"):
        payload = _mapping(roles.get(role))
        if not payload:
            result.append(
                {
                    "lane_id": role,
                    "worker_id": "",
                    "role": role,
                    "report_present": False,
                    "report_non_empty": False,
                    "terminal_markers": [],
                }
            )
            continue
        result.append(
            {
                "lane_id": role,
                "worker_id": str(payload.get("worker_id", "")),
                "role": role,
                "run_id": str(payload.get("run_id", "")),
                "report_present": bool(payload.get("report_present")),
                "report_non_empty": bool(payload.get("report_non_empty")),
                "terminal_markers": [str(item) for item in _list(payload.get("terminal_markers"))],
            }
        )
    return result


def _collect_role_task_evidence(role: str, spec: dict[str, Any], blocked_reasons: list[str]) -> dict[str, Any]:
    task_dir_raw = str(spec.get("task_dir", "")).strip()
    task_dir = Path(task_dir_raw).expanduser().resolve() if task_dir_raw else None
    report_present = False
    report_non_empty = False
    terminal_markers: list[str] = []
    if task_dir is None:
        blocked_reasons.append(f"{role}.task_dir is required")
    elif not task_dir.is_dir():
        blocked_reasons.append(f"{role}.task_dir is not a directory: {task_dir_raw}")
    else:
        report = task_dir / "report.md"
        report_present = report.is_file()
        if report_present:
            try:
                report_non_empty = bool(report.read_text(encoding="utf-8").strip())
            except OSError as exc:
                blocked_reasons.append(f"{role}.report.md could not be read: {exc}")
        terminal_markers = [marker for marker in TERMINAL_MARKERS if (task_dir / marker).is_file()]
    return {
        "worker_id": str(spec.get("worker_id", "")),
        "role": role,
        "run_id": str(spec.get("run_id", "")),
        "task_id": str(spec.get("task_id", "")),
        "task_dir": str(task_dir) if task_dir is not None else "",
        "report_present": report_present,
        "report_non_empty": report_non_empty,
        "terminal_markers": terminal_markers,
    }


def _role_run_spec_from_dispatch_metadata(
    role: str,
    metadata: dict[str, Any],
    root: Path | None,
    blocked_reasons: list[str],
) -> dict[str, Any]:
    run_id = _normalize_visible_run_id(metadata.get("run_id") or metadata.get("run") or "")
    task_id = str(metadata.get("task_id") or metadata.get("task") or "").strip()
    task_dir_raw = str(metadata.get("task_dir", "")).strip()
    task_dir = _resolve_role_task_dir(root, run_id, task_id, task_dir_raw)
    task_payload: dict[str, Any] = {}
    dispatch_payload: dict[str, Any] = {}
    owner = ""
    if task_dir is None:
        blocked_reasons.append(f"{role}.task_dir requires root plus run_id/task_id or explicit task_dir")
    elif not task_dir.is_dir():
        blocked_reasons.append(f"{role} task_dir is not a directory: {task_dir}")
    else:
        task_payload = _read_json_optional(task_dir / "task.json")
        dispatch_payload = _read_json_optional(task_dir / "dispatch.json")
        owner = _read_text_optional(task_dir / "owner")
    run_id = run_id or str(task_payload.get("run") or dispatch_payload.get("run") or "")
    task_id = task_id or str(task_payload.get("id") or task_payload.get("task") or dispatch_payload.get("task") or "")
    worker_id = str(
        metadata.get("worker_id")
        or task_payload.get("owner")
        or task_payload.get("worker")
        or dispatch_payload.get("worker")
        or dispatch_payload.get("owner")
        or owner
        or ""
    )
    if not worker_id:
        blocked_reasons.append(f"{role}.worker_id could not be resolved from metadata or task artifacts")
    return {
        "worker_id": worker_id,
        "run_id": run_id,
        "task_id": task_id,
        "task_dir": str(task_dir) if task_dir is not None else "",
    }


def _role_run_artifact_payload(role: str, artifact: dict[str, Any]) -> dict[str, Any]:
    roles = _mapping(artifact.get("roles"))
    direct_role = _mapping(roles.get(role))
    if direct_role:
        return _flatten_role_artifact_payload(direct_role)

    for item in _list(artifact.get("role_results")):
        payload = _mapping(item)
        if str(payload.get("role") or payload.get("worker_role") or "").strip() == role:
            return _flatten_role_artifact_payload(payload)

    assignments = _mapping(artifact.get("role_assignments") or artifact.get("role_map"))
    assignment = assignments.get(role)
    assignment_payload = _assignment_payload(assignment)
    worker_id = str(assignment_payload.get("worker_id") or assignment_payload.get("worker") or "").strip()
    if _has_role_run_reference(assignment_payload):
        return assignment_payload
    if not worker_id:
        return {}
    for item in _list(artifact.get("worker_results")) + _list(artifact.get("executions")):
        payload = _mapping(item)
        candidate = _flatten_role_artifact_payload(payload)
        candidate_worker = str(candidate.get("worker_id") or candidate.get("worker") or candidate.get("owner") or "").strip()
        if candidate_worker == worker_id:
            candidate.setdefault("worker_id", worker_id)
            return candidate
    return {}


def _flatten_role_artifact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("execution", "result", "worker_result", "dispatch", "submission", "task"):
        nested = _mapping(payload.get(key))
        if nested:
            combined = dict(payload)
            combined.update(nested)
            return combined
    return payload


def _assignment_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"worker_id": value}
    return _mapping(value)


def _has_role_run_reference(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("task_dir")
        or payload.get("run_id")
        or payload.get("run")
        or payload.get("visible_run_id")
        or payload.get("parent_run_id")
    )


def _role_dispatch_metadata_from_artifact_payload(
    role: str,
    payload: dict[str, Any],
    blocked_reasons: list[str],
) -> dict[str, Any]:
    run_id = _normalize_visible_run_id(
        payload.get("run_id")
        or payload.get("run")
        or payload.get("visible_run_id")
        or payload.get("parent_run_id")
        or ""
    )
    task_id = str(
        payload.get("task_id")
        or payload.get("task")
        or payload.get("visible_task_id")
        or payload.get("parent_task_id")
        or ""
    ).strip()
    worker_id = str(payload.get("worker_id") or payload.get("worker") or payload.get("owner") or "").strip()
    task_dir = str(payload.get("task_dir") or "").strip()
    if not task_dir and not run_id:
        blocked_reasons.append(f"{role}.run_id could not be resolved from role artifact")
    if not task_dir and not task_id:
        blocked_reasons.append(f"{role}.task_id could not be resolved from role artifact")
    return {
        "worker_id": worker_id,
        "run_id": run_id,
        "task_id": task_id,
        "task_dir": task_dir,
    }


def _normalize_visible_run_id(value: Any) -> str:
    run_id = str(value or "").strip()
    if run_id.startswith("visible-runs/"):
        return run_id.split("/", 1)[1]
    return run_id


def _resolve_role_task_dir(root: Path | None, run_id: str, task_id: str, task_dir_raw: str) -> Path | None:
    if task_dir_raw:
        path = Path(task_dir_raw).expanduser()
        return path.resolve() if path.is_absolute() else ((root or Path.cwd()) / path).resolve()
    if root is None or not run_id or not task_id:
        return None
    return (root / ".omx" / "framework" / "team" / "visible-runs" / run_id / "tasks" / task_id).resolve()


def _sandbox_role_coder_prompt(context: dict[str, Any]) -> str:
    content = "\n".join(
        [
            f"# {context['case_id']} Coder Output",
            "",
            *[str(marker) for marker in context["required_markers"]],
            "",
            "This file proves that a visible Coder worker can perform a constrained sandbox write for artifact closeout.",
            "",
            "No business repository write is authorized by this task.",
            "No git index operation is authorized by this task.",
        ]
    )
    return "\n".join(
        [
            f"You are the Coder worker for sandbox role live smoke {context['case_id']}.",
            "",
            "Repository root:",
            str(context["repository_root"]),
            "",
            "Your worker id:",
            str(context["coder_id"]),
            "",
            "Allowed write file (repo-relative):",
            str(context["allowed_file"]),
            "",
            "Allowed write file (absolute):",
            str(context.get("allowed_file_absolute") or ""),
            "",
            "Hard rules:",
            "- Write exactly the allowed file above.",
            "- Do not modify any other repository file.",
            "- Do not touch the git index.",
            "- Do not modify the configured business workspace or any other business repository.",
            "- Do not use raw tmux, direct TUI, direct adapter, or rmux.",
            "",
            "Create the allowed file with this exact Markdown content:",
            "",
            "```markdown",
            content,
            "```",
            "",
            "After writing, verify the file exists and report the exact path written, whether any other file was intentionally modified, whether the git index was touched, and whether any business repository was touched.",
            "",
            "Completion contract:",
            "write a non-empty report.md for this worker task and finish with exactly one terminal marker.",
            "",
        ]
    )


def _sandbox_role_reviewer_prompt(context: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"You are the Reviewer worker for sandbox role live smoke {context['case_id']}.",
            "",
            "Repository root:",
            str(context["repository_root"]),
            "",
            "Your worker id:",
            str(context["reviewer_id"]),
            "",
            "Coder worker id:",
            str(context["coder_id"]),
            "",
            "Allowed sandbox file (repo-relative):",
            str(context["allowed_file"]),
            "",
            "Allowed sandbox file (absolute):",
            str(context.get("allowed_file_absolute") or ""),
            "",
            "Required markers:",
            *[f"- {marker}" for marker in context["required_markers"]],
            "",
            "Hard rules:",
            "- Read only. Do not modify repository files.",
            "- Do not touch the git index.",
            "- Do not modify the configured business workspace or any other business repository.",
            "- Do not rerun or repair the Coder.",
            "- Do not dispatch workers, release locks, use raw tmux, direct TUI, direct adapter, or rmux.",
            "",
            "Review tasks:",
            "1. Inspect the allowed sandbox file.",
            "2. Confirm it contains every required marker.",
            "3. Inspect any Leader-provided Coder postcheck and report evidence.",
            "4. Decide whether the Coder output should be accepted for this sandbox test.",
            *_sandbox_role_verification_prompt_lines(context),
            "",
            "Report format:",
            "- `review_decision: accept|block|human_review_required`",
            "- `scope_check: pass|fail`",
            "- `marker_check: pass|fail`",
            "- `baseline_check: pass|fail|not_checked`",
            "- `business_repo_check: pass|fail|not_checked`",
            "- `git_index_check: pass|fail|not_checked`",
            "- short rationale",
            "",
            "Completion contract:",
            "write a non-empty report.md for this worker task and finish with exactly one terminal marker.",
            "",
        ]
    )


def _sandbox_role_verification_prompt_lines(context: dict[str, Any]) -> list[str]:
    verification = _mapping(context.get("verification"))
    command_argv = [[str(argument) for argument in _list(item)] for item in _list(verification.get("commands"))]
    if not command_argv:
        return []
    command_lines = [
        f'env TMPDIR="$review_tmpdir" {shlex.join(argv)} || verification_status=$?' for argv in command_argv
    ]
    return [
        "",
        "Authorized verification commands:",
        "- This task contract explicitly authorizes the commands below.",
        "- The command-scoped `env` preserves the parent environment outside each command.",
        "- Do not use an inherited temp root or make TMPDIR a global export.",
        "- Record the selected temp path, command status, and successful cleanup in report.md.",
        "",
        "Run this lifecycle from the repository root:",
        "",
        "```bash",
        f"review_tmpdir=$(mktemp -d -- {SAFE_REVIEW_TMPDIR_TEMPLATE}) || exit $?",
        'test -n "$review_tmpdir" || exit 1',
        f'case "$review_tmpdir" in {SAFE_REVIEW_TMPDIR_TEMPLATE.removesuffix("XXXXXX")}*) ;; *) exit 1 ;; esac',
        'test -d "$review_tmpdir" || exit 1',
        "trap 'rm -rf -- \"$review_tmpdir\"' EXIT",
        'printf \'verification_tmpdir: %s\\n\' "$review_tmpdir"',
        "verification_status=0",
        *command_lines,
        "cleanup_status=0",
        'rm -rf -- "$review_tmpdir" || cleanup_status=$?',
        "trap - EXIT",
        'test ! -e "$review_tmpdir" || cleanup_status=$?',
        'test "$verification_status" -eq 0 && test "$cleanup_status" -eq 0',
        "```",
    ]


def _sandbox_role_gate_prompt(context: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"You are the advisory Gate worker for sandbox role live smoke {context['case_id']}.",
            "",
            "Repository root:",
            str(context["repository_root"]),
            "",
            "Your worker id:",
            str(context["gate_id"]),
            "",
            "This is an advisory read-only Gate review. You do not authorize business writes.",
            "",
            "Hard rules:",
            "- Read only. Do not modify repository files.",
            "- Do not dispatch workers.",
            "- Do not release locks.",
            "- Do not write terminal markers for any other worker.",
            "- Do not touch the git index.",
            "- Do not modify the configured business workspace or any other business repository.",
            "- Do not use raw tmux, direct TUI, direct adapter, or rmux.",
            "",
            "Evidence to inspect:",
            f"- Coder sandbox file (repo-relative): {context['allowed_file']}",
            f"- Coder sandbox file (absolute): {context.get('allowed_file_absolute') or ''}",
            "- Role contract and any Leader-provided Coder/Reviewer postcheck evidence.",
            "- Live Coder and Reviewer reports if visible-run paths are provided.",
            "",
            "Gate decision rules:",
            "- `approve` only if the Coder wrote exactly the allowed sandbox file, required markers are present, the Reviewer accepted, and no business repository or git index operation is evident.",
            "- `block` if there is out-of-scope writing, missing protocol evidence, or a failed Reviewer.",
            "- `human_review_required` if evidence is incomplete or ambiguous.",
            "",
            "Report format:",
            "- `gate_advisory_decision: approve|block|human_review_required`",
            "- `protocol_check: pass|fail|unknown`",
            "- `scope_check: pass|fail|unknown`",
            "- `reviewer_check: pass|fail|unknown`",
            "- `baseline_check: pass|fail|unknown`",
            "- `business_repo_check: pass|fail|unknown`",
            "- `git_index_check: pass|fail|unknown`",
            "- short rationale",
            "",
            "Completion contract:",
            "write a non-empty report.md for this worker task and finish with exactly one terminal marker.",
            "",
        ]
    )


def _package_file_reference(package_dir: str, filename: str) -> str:
    return str(Path(package_dir) / filename) if package_dir else filename


def _sandbox_role_allowed_file_contract(
    *,
    repository_root: str,
    artifact_root: str,
    allowed_file: str,
    blocked_reasons: list[str],
) -> dict[str, str]:
    repo = Path(repository_root).expanduser().resolve() if repository_root else None
    allowed = str(allowed_file or "").strip()
    artifact = str(artifact_root or "").strip()
    if not allowed:
        return {"artifact_root": "", "allowed_file": "", "allowed_file_absolute": ""}
    if not repo or not artifact:
        absolute = str((repo / allowed).resolve()) if repo else ""
        return {"artifact_root": "", "allowed_file": allowed, "allowed_file_absolute": absolute}

    artifact_path = Path(artifact).expanduser()
    artifact_abs = artifact_path.resolve() if artifact_path.is_absolute() else (repo / artifact_path).resolve()
    if not _path_is_relative_to(artifact_abs, repo):
        blocked_reasons.append(f"artifact_root must be inside repository_root: {artifact_abs}")
        return {"artifact_root": str(artifact_abs), "allowed_file": allowed, "allowed_file_absolute": ""}

    allowed_path = Path(allowed).expanduser()
    if allowed_path.is_absolute():
        blocked_reasons.append("allowed_file must be relative when artifact_root is set")
        allowed_abs = allowed_path.resolve()
    else:
        allowed_abs = (artifact_abs / allowed_path).resolve()
    if not _path_is_relative_to(allowed_abs, artifact_abs):
        blocked_reasons.append(f"allowed_file must stay inside artifact_root: {allowed}")
    try:
        repo_relative = allowed_abs.relative_to(repo).as_posix()
    except ValueError:
        blocked_reasons.append(f"allowed_file must resolve inside repository_root: {allowed_abs}")
        repo_relative = allowed
    return {
        "artifact_root": str(artifact_abs),
        "allowed_file": repo_relative,
        "allowed_file_absolute": str(allowed_abs),
    }


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _manual_run_one_command(control_root: str, worker_id: str, role: str, prompt_path: str, timeout_seconds: int) -> str:
    if role.lower() in {"reviewer", "gate"}:
        session_args = (
            "--session-action rotate --session-action-reason role_isolation "
            "--rotation-reason role_isolation "
            "--rehydration-packet-type clean-room-review"
        )
    else:
        session_args = (
            "--session-action keep "
            "--session-action-reason same_domain_continuation"
        )
    return (
        f"./bin/omx_visible_dispatch run-one --root {shlex.quote(control_root)} --worker {shlex.quote(worker_id)} "
        f"--title {shlex.quote(f'Sandbox Role Smoke {role}')} --body \"$(cat {shlex.quote(prompt_path)})\" "
        f"--timeout {timeout_seconds} {session_args} --allow-real"
    )


def _sandbox_role_runner_step(
    *,
    role: str,
    worker_id: str,
    prompt_path: Path,
    postcheck_path: Path,
    read_only: bool,
    depends_on: list[str],
    allowed_files: list[str],
) -> dict[str, Any]:
    title_role = role[:1].upper() + role[1:]
    return {
        "role": role,
        "worker_id": worker_id,
        "title": f"Sandbox Role Smoke {title_role}",
        "prompt_path": str(prompt_path),
        "postcheck_path": str(postcheck_path),
        "read_only": read_only,
        "depends_on": depends_on,
        "allowed_files": allowed_files,
        "expected_terminal_markers": list(TERMINAL_MARKERS),
    }


def _execute_sandbox_role_step(
    *,
    runner_plan: dict[str, Any],
    step: dict[str, Any],
    dispatch_bin: Path,
    runner: CommandRunner,
    timeout_seconds: int,
    prior_evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    role = str(step.get("role", "")).strip()
    worker_id = str(step.get("worker_id", "")).strip()
    control_root = Path(str(runner_plan.get("control_root") or ".")).expanduser().resolve()
    repository_root = Path(str(runner_plan.get("repository_root") or ".")).expanduser().resolve()
    title = str(step.get("title") or f"Sandbox Role Smoke {role.title()}")
    body = _sandbox_role_execution_body(Path(str(step.get("prompt_path"))), prior_evidence)
    session_args = [
        "--session-action",
        "rotate",
        "--session-action-reason",
        "role_isolation",
        "--rotation-reason",
        "role_isolation",
        "--rehydration-packet-type",
        "clean-room-review",
    ] if role in {"reviewer", "gate"} else [
        "--session-action",
        "keep",
        "--session-action-reason",
        "same_domain_continuation",
    ]
    argv = [
        str(dispatch_bin),
        "run-one",
        "--root",
        str(control_root),
        "--worker",
        worker_id,
        "--title",
        title,
        "--body",
        body,
        "--timeout",
        str(timeout_seconds),
        *session_args,
        "--allow-real",
    ]
    result = runner(argv, repository_root, timeout_seconds + 90)
    stdout = str(result.get("stdout", ""))
    run_id = _extract_prefixed_line(stdout, "run_id:")
    task_id = _extract_prefixed_line(stdout, "task_id:") or "task-001"
    task_dir = _resolve_role_task_dir(
        control_root,
        run_id,
        task_id,
        _extract_prefixed_line(stdout, "task_dir:"),
    )
    report_path = _resolve_optional_path(
        control_root,
        _extract_prefixed_line(stdout, "report_path:"),
        task_dir / "report.md" if task_dir is not None else None,
    )
    terminal_state = _extract_prefixed_line(stdout, "protocol_terminal_state:")
    terminal_marker = _normalize_terminal_marker(_extract_prefixed_line(stdout, "terminal_marker:"))
    postcheck = _sandbox_role_step_postcheck(
        role=role,
        worker_id=worker_id,
        run_id=run_id,
        task_id=task_id,
        task_dir=task_dir,
        report_path=report_path,
        terminal_state=terminal_state,
        terminal_marker=terminal_marker,
        command_result=result,
        runner_plan=runner_plan,
        step=step,
    )
    postcheck_path = Path(str(step.get("postcheck_path") or "")).expanduser().resolve()
    postcheck_path.parent.mkdir(parents=True, exist_ok=True)
    postcheck_path.write_text(json.dumps(postcheck, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    postcheck["postcheck_path"] = str(postcheck_path)
    return postcheck


def _sandbox_role_execution_body(prompt_path: Path, prior_evidence: list[dict[str, Any]]) -> str:
    body = prompt_path.read_text(encoding="utf-8")
    if not prior_evidence:
        return body
    return "\n".join(
        [
            body.rstrip(),
            "",
            "Leader-provided prior role evidence:",
            "```json",
            json.dumps(prior_evidence, indent=2, sort_keys=True, ensure_ascii=True),
            "```",
            "",
        ]
    )


def _sandbox_role_step_postcheck(
    *,
    role: str,
    worker_id: str,
    run_id: str,
    task_id: str,
    task_dir: Path | None,
    report_path: Path | None,
    terminal_state: str,
    terminal_marker: str,
    command_result: dict[str, Any],
    runner_plan: dict[str, Any],
    step: dict[str, Any],
) -> dict[str, Any]:
    blocked_reasons: list[str] = []
    report_present = bool(report_path and report_path.is_file())
    report_non_empty = bool(report_present and report_path and report_path.stat().st_size > 0)
    terminal_markers = _terminal_markers_in_task_dir(task_dir)
    if terminal_marker and terminal_marker not in terminal_markers:
        terminal_markers.append(terminal_marker)
    terminal_marker_count = len(terminal_markers)
    normalized_state = terminal_state or _terminal_state_from_markers(terminal_markers)
    if command_result.get("returncode") != 0:
        blocked_reasons.append(f"{role} run-one returned {command_result.get('returncode')}")
    if not run_id:
        blocked_reasons.append(f"{role}.run_id is required")
    if not task_id:
        blocked_reasons.append(f"{role}.task_id is required")
    if task_dir is None or not task_dir.is_dir():
        blocked_reasons.append(f"{role}.task_dir is missing")
    if not report_present:
        blocked_reasons.append(f"{role}.report.md is missing")
    if report_present and not report_non_empty:
        blocked_reasons.append(f"{role}.report.md is empty")
    if terminal_marker_count != 1:
        blocked_reasons.append(f"{role} must have exactly one terminal marker")
    if normalized_state != "completed":
        blocked_reasons.append(f"{role} terminal state must be completed")

    actual_files: list[str] = []
    actual_markers: list[str] = []
    marker_check = "not_applicable"
    if role == "coder":
        marker_result = _sandbox_role_coder_marker_check(runner_plan)
        actual_files = marker_result["actual_files"]
        actual_markers = marker_result["actual_markers"]
        marker_check = marker_result["marker_check"]
        blocked_reasons.extend(marker_result["blocked_reasons"])

    blocked_reasons = _dedupe(blocked_reasons)
    return {
        "schema_version": 1,
        "postcheck_kind": "sandbox-role-step-postcheck",
        "ok": not blocked_reasons,
        "role": role,
        "worker_id": worker_id,
        "run_id": run_id,
        "task_id": task_id,
        "task_dir": str(task_dir) if task_dir is not None else "",
        "report_path": str(report_path) if report_path is not None else "",
        "report_present": report_present,
        "report_non_empty": report_non_empty,
        "terminal_state": normalized_state,
        "terminal_markers": terminal_markers,
        "terminal_marker_count": terminal_marker_count,
        "read_only": bool(step.get("read_only")),
        "allowed_files": [str(item) for item in _list(step.get("allowed_files"))],
        "actual_files": actual_files,
        "required_markers": [str(item) for item in _list(runner_plan.get("required_markers"))],
        "actual_markers": actual_markers,
        "marker_check": marker_check,
        "command_result": _command_evidence(command_result),
        "blocked_reasons": blocked_reasons,
    }


def _sandbox_role_coder_marker_check(runner_plan: dict[str, Any]) -> dict[str, Any]:
    repository_root = Path(str(runner_plan.get("repository_root") or ".")).expanduser().resolve()
    allowed_files = [str(item) for item in _list(runner_plan.get("allowed_files"))]
    required_markers = [str(item) for item in _list(runner_plan.get("required_markers"))]
    blocked_reasons: list[str] = []
    actual_files: list[str] = []
    actual_markers: list[str] = []
    if not allowed_files:
        blocked_reasons.append("coder allowed_files are required")
    for rel in allowed_files:
        path = (repository_root / rel).resolve()
        if not path.is_file():
            blocked_reasons.append(f"coder allowed file is missing: {rel}")
            continue
        actual_files.append(rel)
        text = path.read_text(encoding="utf-8")
        for marker in required_markers:
            if marker in text:
                actual_markers.append(marker)
            else:
                blocked_reasons.append(f"missing required marker: {marker}")
    return {
        "actual_files": actual_files,
        "actual_markers": _dedupe(actual_markers),
        "marker_check": "pass" if not blocked_reasons else "fail",
        "blocked_reasons": _dedupe(blocked_reasons),
    }


def _sandbox_role_execution_artifact(
    runner_plan: dict[str, Any],
    role_results: list[dict[str, Any]],
    actual_files: list[str],
    actual_markers: list[str],
) -> dict[str, Any]:
    package_dir = Path(str(runner_plan.get("package_dir") or ".")).expanduser().resolve()
    template = _read_json_optional(package_dir / SANDBOX_ROLE_LIVE_SMOKE_PACKAGE_FILES["role_run_artifact_template"])
    artifact = dict(template)
    artifact.update(
        {
            "schema_version": 1,
            "artifact_kind": "sandbox-role-run-artifact",
            "case_id": str(runner_plan.get("case_id") or template.get("case_id") or "sandbox-role-run"),
            "task_id": str(template.get("task_id") or "task-001"),
            "root": str(runner_plan.get("control_root") or template.get("root") or ""),
            "allowed_files": [str(item) for item in _list(runner_plan.get("allowed_files"))],
            "actual_files": actual_files,
            "required_markers": [str(item) for item in _list(runner_plan.get("required_markers"))],
            "actual_markers": actual_markers,
            "role_assignments": _mapping(runner_plan.get("role_assignments")),
            "worker_results": [
                {
                    "worker_id": str(result.get("worker_id") or ""),
                    "visible_run_id": str(result.get("run_id") or ""),
                    "visible_task_id": str(result.get("task_id") or ""),
                    "state": str(result.get("terminal_state") or "unknown"),
                    "task_dir": str(result.get("task_dir") or ""),
                    "report_path": str(result.get("report_path") or ""),
                    "postcheck_path": str(result.get("postcheck_path") or ""),
                }
                for result in role_results
            ],
        }
    )
    return artifact


def _sandbox_role_runner_result(
    *,
    runner_plan: dict[str, Any],
    output_dir: Path,
    role_results: list[dict[str, Any]],
    closeout: dict[str, Any],
    blocked_reasons: list[str],
    live_run_allowed: bool,
    dispatch_attempted: bool,
) -> dict[str, Any]:
    artifact_path = str(runner_plan.get("role_run_artifact_path") or output_dir / "role-run-artifact.json")
    ok = not blocked_reasons and bool(closeout.get("ok"))
    return {
        "schema_version": 1,
        "run_kind": "sandbox-role-live-smoke-runner",
        "advisory_only": True,
        "ok": ok,
        "live_run_allowed": live_run_allowed,
        "dispatch_attempted": dispatch_attempted,
        "did_dispatch_workers": bool(role_results),
        "runner_plan": runner_plan,
        "role_results": role_results,
        "role_run_artifact_path": artifact_path,
        "run_summary_path": str(runner_plan.get("run_summary_path") or output_dir / "run-summary.json"),
        "closeout": closeout,
        "closeout_decision": str(closeout.get("closeout_decision", "human_review_required")),
        "blocked_reasons": _dedupe(blocked_reasons),
        "would_execute_live": False,
        "would_dispatch_workers": False,
        "would_write_business": False,
        "would_release_locks": False,
        "would_write_terminal_markers": False,
        "would_use_raw_tmux": False,
        "would_use_direct_tui": False,
        "would_use_direct_adapter": False,
        "would_use_rmux": False,
    }


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
        "argv": [str(item) for item in _list(result.get("argv"))],
        "returncode": int(result.get("returncode", 0) or 0),
        "stdout_excerpt": str(result.get("stdout", ""))[:4000],
        "stderr_excerpt": str(result.get("stderr", ""))[:4000],
    }


def _extract_prefixed_line(text: str, prefix: str) -> str:
    for line in reversed(text.splitlines()):
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def _resolve_optional_path(root: Path, raw: str, fallback: Path | None = None) -> Path | None:
    if raw:
        path = Path(raw).expanduser()
        return path.resolve() if path.is_absolute() else (root / path).resolve()
    return fallback


def _terminal_markers_in_task_dir(task_dir: Path | None) -> list[str]:
    if task_dir is None or not task_dir.is_dir():
        return []
    return [marker for marker in TERMINAL_MARKERS if (task_dir / marker).is_file()]


def _normalize_terminal_marker(marker: str) -> str:
    marker = marker.strip()
    if not marker:
        return ""
    return marker if marker.startswith(".") else f".{marker}"


def _terminal_state_from_markers(markers: list[str]) -> str:
    if ".done" in markers:
        return "completed"
    if ".blocked" in markers:
        return "blocked"
    if ".fail" in markers:
        return "failed"
    return ""


def _write_package_dir(package_dir: Path, payloads: dict[str, dict[str, Any]]) -> None:
    package_dir.mkdir(parents=True, exist_ok=True)
    for filename, payload in payloads.items():
        (package_dir / filename).write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )


def _write_smoke_package_dir(package_dir: Path, payloads: dict[str, dict[str, Any] | str]) -> None:
    package_dir.mkdir(parents=True, exist_ok=True)
    for filename, payload in payloads.items():
        if isinstance(payload, str):
            text = payload
        else:
            text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        (package_dir / filename).write_text(text, encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _read_json_optional(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {}


def _read_text_optional(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result
