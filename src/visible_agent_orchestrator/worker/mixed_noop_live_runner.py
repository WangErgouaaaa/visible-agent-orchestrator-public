"""Controlled mixed no-op live runner.

This runner is intentionally narrow: one OpenCode visible worker plus one
registered Claude worker, sandbox/no-op tasks only, no registry writes, and no
business repository writes. OpenCode execution goes through
``omx_visible_dispatch run-one``. Claude execution goes through the existing
``omx_worker_backend claude-live-submit`` wrapper.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from visible_agent_orchestrator.runtime.backends import default_backend_name
from .authorization import validate_authorization_record_file
from .live_enable import load_live_enable_config
from .mixed_noop_contract import (
    BUSINESS_REPO_ROOT,
    MixedNoopContractRequest,
    build_mixed_noop_contract_plan,
    validate_mixed_noop_result_files,
)


SCHEMA_VERSION = 1
RUN_KIND = "mixed-noop-live"
REGISTERED_REGISTRY_REL = ".omx/framework/team/registered-workers/registered-workers.json"
OPENCODE_REGISTRY_REL = ".omx/framework/team/opencode_visible_workers.json"
STATE_TEAM_REL = ".omx/state/team"
TERMINAL_MARKERS = [".done", ".blocked", ".fail"]
CommandRunner = Callable[[list[str], Path, int], dict[str, Any]]


def recommended_outer_timeout_seconds(timeout_seconds: int) -> int:
    return max(1, int(timeout_seconds)) * 2 + 240


@dataclass(frozen=True)
class MixedNoopLiveRequest:
    root: str
    registered_worker_registry_path: str
    opencode_visible_registry_path: str
    include_workers: list[str] = field(default_factory=list)
    execution_mode: str = "no-live"
    allow_mixed_noop_live: bool = False
    timeout_seconds: int = 240
    run_id: str = ""
    dispatch_bin: str = ""
    worker_backend_bin: str = ""
    watcher_bin: str = ""
    live_enable_config_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_mixed_noop_live_plan(request: MixedNoopLiveRequest) -> dict[str, Any]:
    root = Path(request.root).resolve()
    run_id = request.run_id or _default_run_id()
    run_root = _run_root(root, run_id)
    contract = _build_contract(request, run_root)
    blocked = _plan_blockers(request, contract)
    workers = _workers_by_id(contract)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "plan_kind": "mixed-noop-live-plan",
        "created_at": _utc_now(),
        "run_id": run_id,
        "root": str(root),
        "mixed_run_root": str(run_root),
        "registered_worker_registry": str(Path(request.registered_worker_registry_path).resolve()),
        "opencode_visible_registry": str(Path(request.opencode_visible_registry_path).resolve()),
        "include_workers": list(request.include_workers),
        "workers": [workers[item] for item in request.include_workers if item in workers],
        "worker_ids": sorted(workers),
        "contract": contract,
        "ok": not blocked,
        "blocked_reasons": _dedupe(blocked),
        "would_execute_live": False,
        "would_write_registry": False,
        "would_write_business": False,
        "would_write_state_team": False,
        "would_use_raw_tmux": False,
        "would_use_direct_tui": False,
        "would_use_direct_adapter_submit": False,
        "recommended_outer_timeout_seconds": recommended_outer_timeout_seconds(request.timeout_seconds),
        "timeout_budget_note": "leader-side timeout must exceed OpenCode run-one wait, Claude submit wait, and child terminal wait",
        "future_live_requires": ["--allow-mixed-noop-live", "preflight ok immediately before worker touch"],
        "safe_next_actions": ["run execute-mixed-noop-live once with explicit user authorization"],
        "forbidden_next_actions": _forbidden_next_actions(),
    }
    return payload


def execute_mixed_noop_live(
    request: MixedNoopLiveRequest,
    *,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    root = Path(request.root).resolve()
    run_id = request.run_id or _default_run_id()
    run_root = _run_root(root, run_id)
    runner = command_runner or _run_command
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "tasks").mkdir(exist_ok=True)
    (run_root / "aggregate").mkdir(exist_ok=True)
    (run_root / "contract").mkdir(exist_ok=True)

    pre_hashes = _hashes(root, request)
    pre_state = _state_team_snapshot(root)
    pre_business_status = _git_status(BUSINESS_REPO_ROOT)
    pre_business_index = _git_index(BUSINESS_REPO_ROOT)
    contract = _build_contract(request, run_root)
    contract_path = run_root / "contract" / "contract.json"
    contract_path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    blocked = _plan_blockers(request, contract)
    opencode_worker_id, claude_worker_id = _selected_worker_ids(contract, request.include_workers)
    commands: list[dict[str, Any]] = []
    worker_results: list[dict[str, Any]] = []
    if not request.allow_mixed_noop_live:
        blocked.append("execute-mixed-noop-live requires --allow-mixed-noop-live")
    if blocked:
        return _write_blocked_result(run_root, contract_path, request, blocked, commands, pre_hashes, pre_state, pre_business_status, pre_business_index)

    preflight = _execution_preflight(root, request, contract, runner)
    commands.extend(preflight.get("commands", []))
    if not preflight.get("ok"):
        blocked.extend(preflight.get("blocked_reasons", []))
    if blocked:
        return _write_blocked_result(run_root, contract_path, request, blocked, commands, pre_hashes, pre_state, pre_business_status, pre_business_index)

    opencode = _execute_opencode(root, run_root, request, runner, opencode_worker_id)
    commands.append(opencode["command"])
    worker_results.append(
        _worker_result(
            opencode_worker_id,
            opencode["task_dir"],
            expected_task_dir=opencode.get("expected_task_dir", ""),
            visible_run_id=opencode.get("visible_run_id", ""),
            visible_task_id=opencode.get("visible_task_id", ""),
            collection_source=opencode.get("collection_source", ""),
            child_created=opencode.get("child_created", False),
        )
    )
    if not opencode["ok"]:
        blocked.extend(opencode["blocked_reasons"])
        return _write_blocked_result(
            run_root,
            contract_path,
            request,
            blocked,
            commands,
            pre_hashes,
            pre_state,
            pre_business_status,
            pre_business_index,
            worker_results,
        )

    claude = _execute_claude(root, run_root, request, runner, claude_worker_id)
    commands.extend(claude.get("commands", [claude["command"]]))
    worker_results.append(
        _worker_result(
            claude_worker_id,
            claude["task_dir"],
            child_created=claude.get("child_created", True),
            collection_source=claude.get("collection_source", "claude_live_submit"),
            prompt_delivered=claude.get("prompt_delivered", False),
            submitted=claude.get("submitted", False),
            pre_submit_readiness_not_safe=claude.get("pre_submit_readiness_not_safe", False),
            task_start_unconfirmed=claude.get("task_start_unconfirmed", False),
        )
    )
    if not claude["ok"]:
        blocked.extend(claude["blocked_reasons"])

    return _write_result(run_root, contract_path, request, blocked, commands, pre_hashes, pre_state, pre_business_status, pre_business_index, worker_results)


def validate_mixed_noop_live_result_file(result_path: Path | str) -> dict[str, Any]:
    path = Path(result_path).resolve()
    result = _read_json(path)
    contract_path = Path(str(result.get("contract_path", ""))).resolve()
    blocked: list[str] = []
    if not contract_path.is_file():
        blocked.append("contract_path missing")
    validation = validate_mixed_noop_result_files(contract_path, path).to_dict() if contract_path.is_file() else {"ok": False, "blocked_reasons": blocked}
    blocked.extend(validation.get("blocked_reasons", []))
    child_statuses = [_classify_child_result(item) for item in result.get("worker_results", [])]
    for status in child_statuses:
        terminal_status = status.get("terminal_status", "")
        if terminal_status in {"child_not_created", "child_missing_report", "child_missing_marker", "child_conflicting_markers", "child_path_mapping_error", "child_pre_submit_readiness_not_safe", "child_task_start_unconfirmed"}:
            blocked.append(f"{status.get('worker_id')} {terminal_status}")
    expected_worker_ids = sorted(str(item) for item in result.get("include_workers", []) if item)
    if len(expected_worker_ids) != 2 or len(set(expected_worker_ids)) != 2:
        blocked.append("include_workers must name two distinct operator-selected workers")
    opencode_worker_ids = _worker_ids_by_backend(_read_json(contract_path) if contract_path.is_file() else {}, "opencode_tui", expected_worker_ids)
    expected_opencode = opencode_worker_ids[0] if len(opencode_worker_ids) == 1 else ""
    if not expected_opencode:
        blocked.append("contract must identify one selected OpenCode worker")
    registry_hashes_changed = result.get("registry_hashes_before") != result.get("registry_hashes_after")
    if registry_hashes_changed:
        blocked.append("registry hashes changed")
    if result.get("state_team_before") != result.get("state_team_after"):
        blocked.append(".omx/state/team changed")
    if result.get("business_status_before") != result.get("business_status_after"):
        blocked.append("business repo status changed")
    if result.get("business_index_before") != result.get("business_index_after"):
        blocked.append("business repo index changed")
    if result.get("used_raw_tmux") or result.get("used_direct_tui") or result.get("used_direct_adapter_submit") or result.get("used_rmux_live"):
        blocked.append("forbidden execution surface used")
    worker_ids = sorted(item.get("worker_id", "") for item in result.get("worker_results", []))
    result_claims_completed = bool(result.get("ok")) or str(result.get("aggregate_state", "")) == "completed"
    if result_claims_completed and worker_ids != expected_worker_ids:
        blocked.append(f"result workers must be exactly {', '.join(expected_worker_ids)}")
    if not worker_ids:
        blocked.append("result must contain worker_results")
    if expected_opencode not in worker_ids:
        blocked.append("OpenCode child_not_created")
    registry_modified = bool(validation.get("registry_modified", False) or registry_hashes_changed)
    ok = not _dedupe(blocked)
    return {
        "schema_version": SCHEMA_VERSION,
        "validation_kind": "mixed-noop-live-result",
        "ok": ok,
        "result_path": str(path),
        "contract_path": str(contract_path),
        "aggregate_state": result.get("aggregate_state", validation.get("aggregate_state", "invalid")),
        "worker_results": validation.get("worker_results", []),
        "child_statuses": child_statuses,
        "all_reports_nonempty": validation.get("all_reports_nonempty", False),
        "all_terminal_markers_valid": validation.get("all_terminal_markers_valid", False),
        "business_repo_modified": validation.get("business_repo_modified", False),
        "registry_hashes_changed": registry_hashes_changed,
        "registry_modified": registry_modified,
        "state_team_modified": validation.get("state_team_modified", False),
        "blocked_reasons": _dedupe(blocked),
        "warnings": validation.get("warnings", []),
    }


def _build_contract(request: MixedNoopLiveRequest, run_root: Path) -> dict[str, Any]:
    plan = build_mixed_noop_contract_plan(
        MixedNoopContractRequest(
            root=str(Path(request.root).resolve()),
            registered_worker_registry_path=str(Path(request.registered_worker_registry_path).resolve()),
            opencode_visible_registry_path=str(Path(request.opencode_visible_registry_path).resolve()),
            include_workers=list(request.include_workers),
            execution_mode=request.execution_mode,
            contract_id=run_root.name,
            mixed_run_root=str(run_root),
        )
    ).to_dict()
    task_dirs = plan.get("artifact_layout", {}).get("per_worker_task_dirs", {})
    for worker in plan.get("workers", []):
        task_dir = task_dirs.get(worker.get("worker_id"), "")
        if task_dir:
            worker["allowed_write_roots"] = [task_dir]
    plan["allowed_write_roots"] = [task_dirs[item] for item in request.include_workers if item in task_dirs]
    for item in plan.get("task_matrix", []):
        task_dir = task_dirs.get(item.get("worker_id"), "")
        if task_dir:
            item["allowed_write_roots"] = [task_dir]
    plan["would_execute_live"] = False
    plan["would_write_registry"] = False
    return plan


def _plan_blockers(request: MixedNoopLiveRequest, contract: dict[str, Any]) -> list[str]:
    blocked = list(contract.get("blocked_reasons", []))
    if request.execution_mode != "no-live":
        blocked.append("mixed-noop live runner plan requires execution_mode=no-live")
    workers = _workers_by_id(contract)
    for worker_id in request.include_workers:
        if worker_id not in workers:
            blocked.append(f"worker missing: {worker_id}")
    opencode_ids = _worker_ids_by_backend(contract, "opencode_tui", request.include_workers)
    claude_ids = _worker_ids_by_backend(contract, "claude_code", request.include_workers)
    if len(request.include_workers) != 2 or len(opencode_ids) != 1 or len(claude_ids) != 1:
        blocked.append("workers must include exactly one OpenCode worker and one Claude worker")
    for worker_id, worker in workers.items():
        if worker.get("business_ready") is True:
            blocked.append(f"worker {worker_id} business_ready=true is forbidden")
        if worker.get("allow_business_repo_writes") is True:
            blocked.append(f"worker {worker_id} allow_business_repo_writes=true is forbidden")
    return _dedupe(blocked)


def _execution_preflight(root: Path, request: MixedNoopLiveRequest, contract: dict[str, Any], runner: CommandRunner) -> dict[str, Any]:
    commands = []
    blocked = []
    worker_backend = _worker_backend_bin(root, request)
    watcher = _bin(root, request.watcher_bin, "omx_visible_watcher")
    dispatch = _bin(root, request.dispatch_bin, "omx_visible_dispatch")
    opencode_worker_id, claude_worker_id = _selected_worker_ids(contract, request.include_workers)
    for argv, label, ok_check in [
        ([str(watcher), "alerts", "--root", str(root), "--all-workers", "--json"], "watcher alerts", lambda r, p: r["returncode"] == 0 and len(p.get("alerts", [])) == 0),
        (
            [str(dispatch), "locks", "--root", str(root), "--worker", opencode_worker_id, "--json"],
            "OpenCode worker locks",
            lambda r, p: r["returncode"] == 0 and _target_worker_available(p),
        ),
        ([str(worker_backend), "opencode-worker-identity-diagnose", "--root", str(root), "--worker-id", opencode_worker_id, "--opencode-visible-registry", str(Path(request.opencode_visible_registry_path).resolve()), "--json"], "OpenCode identity", lambda r, p: r["returncode"] == 0 and p.get("safe_for_live_submit") is True),
        ([str(worker_backend), "mixed-noop-contract-plan", "--root", str(root), "--registered-worker-registry", str(Path(request.registered_worker_registry_path).resolve()), "--opencode-visible-registry", str(Path(request.opencode_visible_registry_path).resolve()), "--include-worker", opencode_worker_id, "--include-worker", claude_worker_id, "--execution-mode", "no-live", "--json"], "mixed-noop contract", lambda r, p: r["returncode"] == 0 and p.get("ok") is True and p.get("blocked_reasons") == []),
    ]:
        result = runner(argv, root, 120)
        payload = _json_stdout(result)
        commands.append(_command_evidence(result))
        if not ok_check(result, payload):
            blocked.append(f"{label} preflight failed")
    return {"ok": not blocked, "blocked_reasons": _dedupe(blocked), "commands": commands}


def _target_worker_available(payload: dict[str, Any]) -> bool:
    if payload.get("target_worker_available_for_dispatch") is True:
        return True
    target_count = payload.get("target_worker_lock_count")
    if isinstance(target_count, int):
        return target_count == 0
    return len(payload.get("active_locks", []) or payload.get("locks", [])) == 0


def _execute_opencode(root: Path, run_root: Path, request: MixedNoopLiveRequest, runner: CommandRunner, worker_id: str) -> dict[str, Any]:
    expected_task_dir = run_root / "tasks" / worker_id
    expected_task_dir.mkdir(parents=True, exist_ok=True)
    body = _task_prompt(worker_id, "opencode_tui", expected_task_dir)
    dispatch = _bin(root, request.dispatch_bin, "omx_visible_dispatch")
    argv = [
        str(dispatch),
        "run-one",
        "--root",
        str(root),
        "--worker",
        worker_id,
        "--title",
        "Mixed no-op sandbox OpenCode task",
        "--body",
        body,
        "--timeout",
        str(int(request.timeout_seconds)),
        "--session-action",
        "keep",
        "--session-action-reason",
        "same_domain_continuation",
        "--allow-real",
    ]
    result = runner(argv, root, int(request.timeout_seconds) + 120)
    blocked = []
    if result.get("timed_out"):
        blocked.append("OpenCode run-one timed out after child dispatch")
    elif result.get("returncode") != 0:
        blocked.append(f"OpenCode run-one returned {result.get('returncode')}")
    visible_task_dir = _parse_visible_dispatch_task_dir(root, str(result.get("stdout", "")))
    child_created = bool(visible_task_dir and visible_task_dir.is_dir())
    task_dir = visible_task_dir if child_created else expected_task_dir
    if not child_created:
        blocked.append("OpenCode child task not created or not collected")
    blocked.extend(_task_dir_blocks(task_dir, worker_id))
    visible_run_id, visible_task_id = _visible_run_task_ids(visible_task_dir)
    return {
        "ok": not blocked,
        "blocked_reasons": _dedupe(blocked),
        "task_dir": str(task_dir),
        "expected_task_dir": str(expected_task_dir),
        "visible_run_id": visible_run_id,
        "visible_task_id": visible_task_id,
        "collection_source": "visible_dispatch_task_dir" if child_created else "missing_visible_dispatch_task_dir",
        "child_created": child_created,
        "command": _command_evidence(result),
    }


def _execute_claude(root: Path, run_root: Path, request: MixedNoopLiveRequest, runner: CommandRunner, worker_id: str) -> dict[str, Any]:
    task_dir = run_root / "tasks" / worker_id
    task_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = task_dir / "prompt.txt"
    prompt_path.write_text(_task_prompt(worker_id, "claude_code", task_dir), encoding="utf-8")
    auth_path = task_dir / "run-local-authorization.json"
    worker = _registered_claude_worker(Path(request.registered_worker_registry_path), worker_id)
    auth_path.write_text(json.dumps(_run_local_claude_authorization(root, task_dir, worker), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    auth_validation = validate_authorization_record_file(auth_path).to_dict()
    if not auth_validation.get("ok"):
        return {
            "ok": False,
            "blocked_reasons": [f"run-local Claude authorization invalid: {item}" for item in auth_validation.get("blocked_reasons", [])],
            "task_dir": str(task_dir),
            "command": {"argv": [], "returncode": -1, "stdout": "", "stderr": "authorization invalid"},
            "commands": [{"argv": [], "returncode": -1, "stdout": "", "stderr": "authorization invalid"}],
            "child_created": False,
        }
    readiness = _claude_pre_submit_readiness(root, worker, runner)
    readiness_path = task_dir / "pre_submit_readiness.json"
    readiness_path.write_text(json.dumps(_public_readiness(readiness), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not readiness.get("submit_safe"):
        state = str(readiness.get("readiness") or "unknown")
        blocked_reason = f"{worker_id} pre_submit_readiness_not_safe: {state}"
        if readiness.get("reason"):
            blocked_reason = f"{blocked_reason}: {readiness.get('reason')}"
        return {
            "ok": False,
            "blocked_reasons": [blocked_reason],
            "task_dir": str(task_dir),
            "command": readiness["commands"][-1] if readiness.get("commands") else {"argv": [], "returncode": -1, "stdout": "", "stderr": "pre-submit readiness missing"},
            "commands": readiness.get("commands", []),
            "collection_source": "claude_pre_submit_readiness_blocked",
            "child_created": False,
            "prompt_delivered": False,
            "submitted": False,
            "pre_submit_readiness_not_safe": True,
            "task_start_unconfirmed": False,
        }
    live_enable_config_path = _run_local_claude_live_enable_config(root, task_dir, request, worker)
    argv = [
        str(_worker_backend_bin(root, request)),
        "claude-live-submit",
        "--root",
        str(root),
        "--worker-id",
        worker_id,
        "--workspace-root",
        str(root),
        "--runtime-backend",
        str(worker.get("runtime_backend", default_backend_name())),
        "--runtime-binding-kind",
        str(worker.get("runtime_binding_kind", "visible-pane")),
        "--pane-id",
        str(worker.get("pane_id", "")),
        "--pane-pid",
        str(worker.get("pane_pid", "")),
        "--process-id",
        str(worker.get("process_id", "")),
        "--command",
        str(worker.get("command", "")),
        "--user-owned-pane",
        "--task-dir",
        str(task_dir),
        "--task-id",
        worker_id,
        "--prompt-file",
        str(prompt_path),
        "--authorization-record",
        str(auth_path),
        "--config",
        live_enable_config_path,
        "--allowed-write-root",
        str(task_dir),
        "--forbidden-path",
        BUSINESS_REPO_ROOT,
        "--forbidden-path",
        str(root / STATE_TEAM_REL),
        "--report-path",
        str(task_dir / "report.md"),
        "--allow-real",
        "--allow-tmux-input",
        "--allow-user-owned-pane",
        "--json",
    ]
    result = runner(argv, root, 60)
    commands = list(readiness.get("commands", [])) + [_command_evidence(result)]
    blocked = []
    payload = _json_stdout(result)
    prompt_delivered = bool(payload.get("submitted"))
    if result.get("returncode") != 0 or not prompt_delivered:
        blocked.append("Claude live submit wrapper did not submit")
        blocked.extend(str(item) for item in payload.get("blocked_reasons", []))
    wait = _wait_for_task_dir(task_dir, int(request.timeout_seconds))
    task_start_confirmed = _task_start_confirmed(task_dir)
    task_start_unconfirmed = prompt_delivered and not task_start_confirmed
    if task_start_unconfirmed:
        blocked.append(f"{worker_id} task_start_unconfirmed")
    if not wait["ok"]:
        blocked.extend(wait["blocked_reasons"])
    return {
        "ok": not blocked,
        "blocked_reasons": _dedupe(blocked),
        "task_dir": str(task_dir),
        "command": _command_evidence(result),
        "commands": commands,
        "child_created": True,
        "collection_source": "claude_live_submit" if task_start_confirmed else "claude_live_submit_unconfirmed",
        "prompt_delivered": prompt_delivered,
        "submitted": prompt_delivered and task_start_confirmed,
        "pre_submit_readiness_not_safe": False,
        "task_start_unconfirmed": task_start_unconfirmed,
    }


def _claude_pre_submit_readiness(root: Path, worker: dict[str, Any], runner: CommandRunner) -> dict[str, Any]:
    pane_id = str(worker.get("pane_id") or "")
    if not pane_id:
        return {"readiness": "unknown", "submit_safe": False, "reason": "pane_id missing", "commands": []}

    pane_result = runner(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{pane_id}\t#{pane_pid}\t#{pane_current_command}",
        ],
        root,
        10,
    )
    commands = [_command_evidence(pane_result)]
    if pane_result.get("returncode") != 0:
        return {
            "readiness": "unknown",
            "submit_safe": False,
            "reason": "tmux display-message failed",
            "commands": commands,
        }
    pane = _tmux_pane_record(str(pane_result.get("stdout", "")), pane_id)
    if not pane:
        return {
            "readiness": "unhealthy",
            "submit_safe": False,
            "reason": f"pane {pane_id} not found",
            "commands": commands,
        }
    expected_pid = str(worker.get("pane_pid") or "")
    if expected_pid and pane.get("pane_pid") and pane["pane_pid"] != expected_pid:
        return {
            "readiness": "unhealthy",
            "submit_safe": False,
            "reason": f"pane_pid mismatch: expected {expected_pid} got {pane['pane_pid']}",
            "commands": commands,
        }

    process_identity = _claude_process_identity(root, worker, pane, runner)
    commands.extend(process_identity.get("commands", []))
    if not process_identity.get("ok"):
        return {
            "readiness": process_identity.get("readiness", "unhealthy"),
            "submit_safe": False,
            "reason": process_identity.get("reason", "Claude process identity not safe"),
            "commands": commands,
            "pane_id": pane_id,
            "pane_pid": pane.get("pane_pid", ""),
            "pane_current_command": pane.get("pane_current_command", ""),
            "process_id": process_identity.get("process_id", ""),
        }

    capture = runner(["tmux", "capture-pane", "-p", "-t", pane_id, "-S", "-80"], root, 10)
    commands.append(_redacted_capture_evidence(capture))
    if capture.get("returncode") != 0:
        return {
            "readiness": "unknown",
            "submit_safe": False,
            "reason": "tmux capture-pane failed",
            "commands": commands,
        }
    readiness = _classify_claude_capture_readiness(str(capture.get("stdout", "")))
    readiness["commands"] = commands
    readiness["pane_id"] = pane_id
    readiness["pane_pid"] = pane.get("pane_pid", "")
    readiness["pane_current_command"] = pane.get("pane_current_command", "")
    readiness["process_id"] = process_identity.get("process_id", "")
    readiness["process_cwd"] = process_identity.get("process_cwd", "")
    return readiness


def _claude_process_identity(
    root: Path,
    worker: dict[str, Any],
    pane: dict[str, str],
    runner: CommandRunner,
) -> dict[str, Any]:
    process_id = str(worker.get("process_id") or "")
    pane_pid = str(pane.get("pane_pid") or "")
    workspace_root = str(worker.get("workspace_root") or "")
    expected_command = str(worker.get("command") or "")
    if not process_id:
        return {
            "ok": False,
            "readiness": "unhealthy",
            "reason": "registered process_id missing",
            "commands": [],
            "process_id": "",
        }
    if not workspace_root:
        return {
            "ok": False,
            "readiness": "unhealthy",
            "reason": "registered workspace_root missing",
            "commands": [],
            "process_id": process_id,
        }
    if not expected_command:
        return {
            "ok": False,
            "readiness": "unhealthy",
            "reason": "registered command missing",
            "commands": [],
            "process_id": process_id,
        }

    process_tree = runner(["ps", "-eo", "pid=,ppid="], root, 10)
    commands = [_redacted_process_evidence(process_tree)]
    if process_tree.get("returncode") != 0:
        return {
            "ok": False,
            "readiness": "unknown",
            "reason": "process tree probe failed",
            "commands": commands,
            "process_id": process_id,
        }
    parents = _pid_parent_map(str(process_tree.get("stdout", "")))
    if not _pid_descends_from(process_id, pane_pid, parents):
        return {
            "ok": False,
            "readiness": "unhealthy",
            "reason": "process_id is not a descendant of pane_pid",
            "commands": commands,
            "process_id": process_id,
        }

    process_command = runner(["ps", "-o", "comm=,args=", "-p", process_id], root, 10)
    commands.append(_redacted_process_evidence(process_command))
    if process_command.get("returncode") != 0:
        return {
            "ok": False,
            "readiness": "unknown",
            "reason": "process command probe failed",
            "commands": commands,
            "process_id": process_id,
        }
    command_text = str(process_command.get("stdout", "")).strip()
    command_parts = command_text.split(None, 1)
    command_args = command_parts[1] if len(command_parts) == 2 else ""
    expected_name = Path(expected_command).name
    if expected_command not in command_args and expected_name not in command_args.split():
        return {
            "ok": False,
            "readiness": "unhealthy",
            "reason": "process command does not match registered command",
            "commands": commands,
            "process_id": process_id,
        }

    process_cwd = runner(["readlink", "-f", f"/proc/{process_id}/cwd"], root, 10)
    commands.append(_command_evidence(process_cwd))
    if process_cwd.get("returncode") != 0 or not str(process_cwd.get("stdout", "")).strip():
        return {
            "ok": False,
            "readiness": "unknown",
            "reason": "process cwd probe failed",
            "commands": commands,
            "process_id": process_id,
        }
    actual_cwd = Path(str(process_cwd.get("stdout", "")).strip()).resolve()
    expected_workspace = Path(workspace_root).resolve()
    try:
        actual_cwd.relative_to(expected_workspace)
    except ValueError:
        return {
            "ok": False,
            "readiness": "unhealthy",
            "reason": "process cwd is outside registered workspace_root",
            "commands": commands,
            "process_id": process_id,
            "process_cwd": str(actual_cwd),
        }
    return {
        "ok": True,
        "readiness": "ready",
        "reason": "registered Claude process identity matches pane and workspace",
        "commands": commands,
        "process_id": process_id,
        "process_cwd": str(actual_cwd),
    }


def _pid_parent_map(stdout: str) -> dict[str, str]:
    parents: dict[str, str] = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            parents[parts[0]] = parts[1]
    return parents


def _pid_descends_from(process_id: str, pane_pid: str, parents: dict[str, str]) -> bool:
    current = process_id
    seen: set[str] = set()
    while current and current not in seen:
        if current == pane_pid:
            return True
        seen.add(current)
        current = parents.get(current, "")
    return False


def _tmux_pane_record(stdout: str, pane_id: str) -> dict[str, str]:
    for line in stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0] == pane_id:
            return {"pane_id": parts[0], "pane_pid": parts[1], "pane_current_command": parts[2]}
    return {}


def _classify_claude_capture_readiness(stdout: str) -> dict[str, Any]:
    tail = [line.strip() for line in stdout.splitlines() if line.strip()][-12:]
    if any(line == "\u276f" or line.startswith("\u276f ") for line in tail):
        return {"readiness": "ready", "submit_safe": True, "reason": "Claude prompt marker visible"}
    lowered = "\n".join(tail).lower()
    busy_markers = (
        "press esc to interrupt",
        "esc to interrupt",
        "ctrl+c to cancel",
        "ctrl-c to cancel",
        "stop generating",
        "processing",
        "generating",
        "responding",
    )
    if any(marker in lowered for marker in busy_markers):
        return {"readiness": "busy", "submit_safe": False, "reason": "pane shows active Claude work"}
    return {"readiness": "unknown", "submit_safe": False, "reason": "Claude prompt marker not visible"}


def _public_readiness(readiness: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in readiness.items() if key != "commands"}


def _redacted_capture_evidence(result: dict[str, Any]) -> dict[str, Any]:
    evidence = _command_evidence(result)
    evidence["stdout"] = "[redacted tmux pane capture]"
    return evidence


def _redacted_process_evidence(result: dict[str, Any]) -> dict[str, Any]:
    evidence = _command_evidence(result)
    evidence["stdout"] = "[redacted process evidence]"
    return evidence


def _run_local_claude_live_enable_config(root: Path, task_dir: Path, request: MixedNoopLiveRequest, worker: dict[str, Any]) -> str:
    source_text = request.live_enable_config_path or str(worker.get("live_enable_config", ""))
    if not source_text:
        return ""
    source = Path(source_text).resolve()
    try:
        config, _ = load_live_enable_config(source, "claude_code")
    except Exception:
        return str(source)
    if not config.live_enabled:
        return str(source)
    payload = config.to_dict()
    payload["require_no_active_locks"] = False
    payload["notes"] = _dedupe(
        list(payload.get("notes", []))
        + [
            "mixed-noop run-local override: selected worker locks are checked before execution; sibling/unrelated visible locks do not block the Claude no-op child.",
        ]
    )
    path = task_dir / "run-local-live-enable.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


def _write_result(
    run_root: Path,
    contract_path: Path,
    request: MixedNoopLiveRequest,
    blocked: list[str],
    commands: list[dict[str, Any]],
    pre_hashes: dict[str, str],
    pre_state: list[str],
    pre_business_status: list[str],
    pre_business_index: list[str],
    worker_results: list[dict[str, Any]],
) -> dict[str, Any]:
    root = Path(request.root).resolve()
    result_path = run_root / "result.json"
    aggregate_state = _aggregate_state(worker_results, blocked)
    contract = _read_json(contract_path)
    contract_workers = _workers_by_id(contract)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "result_kind": RUN_KIND,
        "ok": not blocked,
        "run_id": run_root.name,
        "mixed_run_root": str(run_root),
        "contract_path": str(contract_path),
        "result_path": str(result_path),
        "aggregate_state": aggregate_state,
        "worker_results": worker_results,
        "opencode_child_submitted": any(_child_submitted(item, contract_workers) and _backend_kind(contract_workers.get(str(item.get("worker_id", "")), {})) == "opencode_tui" for item in worker_results),
        "claude_child_submitted": any(_child_submitted(item, contract_workers) and _is_claude_worker_id(str(item.get("worker_id", ""))) for item in worker_results),
        "include_workers": list(request.include_workers),
        "commands": commands,
        "blocked_reasons": _dedupe(blocked),
        "registry_hashes_before": pre_hashes,
        "registry_hashes_after": _hashes(root, request),
        "state_team_before": pre_state,
        "state_team_after": _state_team_snapshot(root),
        "business_status_before": pre_business_status,
        "business_status_after": _git_status(BUSINESS_REPO_ROOT),
        "business_index_before": pre_business_index,
        "business_index_after": _git_index(BUSINESS_REPO_ROOT),
        "used_raw_tmux": False,
        "used_direct_tui": False,
        "used_direct_adapter_submit": False,
        "used_rmux_live": False,
        "recommended_outer_timeout_seconds": recommended_outer_timeout_seconds(request.timeout_seconds),
        "timeout_budget_note": "leader-side timeout must exceed OpenCode run-one wait, Claude submit wait, and child terminal wait",
        "next_state": "AWAITING_MIXED_NOOP_REVIEW_AND_NEXT_STEP_AUTHORIZATION" if not blocked and aggregate_state == "completed" else "BLOCKED_MIXED_NOOP_LIVE_REVIEW_REQUIRED",
    }
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    validation = validate_mixed_noop_live_result_file(result_path)
    payload["validation"] = validation
    payload["ok"] = bool(payload["ok"] and validation.get("ok"))
    payload["next_state"] = "AWAITING_MIXED_NOOP_REVIEW_AND_NEXT_STEP_AUTHORIZATION" if payload["ok"] and validation.get("aggregate_state") == "completed" else "BLOCKED_MIXED_NOOP_LIVE_REVIEW_REQUIRED"
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_aggregate(run_root, payload)
    return payload


def _write_blocked_result(
    run_root: Path,
    contract_path: Path,
    request: MixedNoopLiveRequest,
    blocked: list[str],
    commands: list[dict[str, Any]],
    pre_hashes: dict[str, str],
    pre_state: list[str],
    pre_business_status: list[str],
    pre_business_index: list[str],
    worker_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _write_result(run_root, contract_path, request, blocked, commands, pre_hashes, pre_state, pre_business_status, pre_business_index, worker_results or [])


def _write_aggregate(run_root: Path, payload: dict[str, Any]) -> None:
    aggregate = run_root / "aggregate"
    aggregate.mkdir(exist_ok=True)
    marker = {"completed": ".done", "blocked": ".blocked", "failed": ".fail"}.get(payload.get("aggregate_state"), ".blocked")
    (aggregate / "report.md").write_text(
        "# Mixed No-op Live Result\n\n"
        f"- ok: {payload.get('ok')}\n"
        f"- aggregate_state: {payload.get('aggregate_state')}\n"
        f"- next_state: {payload.get('next_state')}\n"
        f"- blocked_reasons: {payload.get('blocked_reasons')}\n",
        encoding="utf-8",
    )
    for item in TERMINAL_MARKERS:
        path = aggregate / item
        if path.exists() and item != marker:
            path.unlink()
    (aggregate / marker).write_text(_utc_now() + "\n", encoding="utf-8")
    (aggregate / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _task_prompt(worker_id: str, backend_kind: str, task_dir: Path) -> str:
    return (
        "Execute this controlled mixed no-op sandbox task now.\n"
        f"worker_id: {worker_id}\n"
        f"backend_kind: {backend_kind}\n"
        f"task_dir: {task_dir}\n"
        "Scope: sandbox/no-op mixed contract validation only.\n"
        "Do not modify the configured business workspace.\n"
        "Do not write registered worker registry, OpenCode visible registry, .omx/state/team, Obsidian, rmux, or other workers.\n"
        "Write a non-empty report.md inside task_dir with worker_id, backend_kind, and the statement 'mixed noop completed'.\n"
        "Create exactly one terminal marker inside task_dir: .done. Do not create .blocked or .fail.\n"
        "Begin immediately. Do not wait for another instruction or confirmation.\n"
    )


def _run_local_claude_authorization(root: Path, task_dir: Path, worker: dict[str, Any] | None = None) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    worker = worker or {}
    worker_id = str(worker["worker_id"])
    return {
        "schema_version": 1,
        "authorization_id": f"auth-mixed-noop-live-{now.strftime('%Y%m%dT%H%M%SZ')}",
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "created_by": "current_prompt_explicit_authorization",
        "authorization_scope": "sandbox_noop_live_smoke",
        "backend_kind": "claude_code",
        "worker_id": worker_id,
        "worker_label": str(worker.get("worker_label", worker_id)),
        "workspace_root": str(root),
        "runtime_backend": str(worker.get("runtime_backend", default_backend_name())),
        "runtime_binding_kind": str(worker.get("runtime_binding_kind", "visible-pane")),
        "pane_id": str(worker.get("pane_id", "")),
        "process_id": str(worker.get("process_id", "")),
        "port": "",
        "user_owned_pane": True,
        "allow_user_owned_pane": True,
        "allowed_read_roots": [str(root)],
        "allowed_write_roots": [str(task_dir)],
        "forbidden_paths": [BUSINESS_REPO_ROOT, str(root / STATE_TEAM_REL), str(root / REGISTERED_REGISTRY_REL), str(root / OPENCODE_REGISTRY_REL)],
        "allow_business_repo_writes": False,
        "business_repo_path": BUSINESS_REPO_ROOT,
        "expected_business_index_files": [],
        "task_profile": "mixed_noop_sandbox",
        "allowed_task_kinds": ["mixed_noop_sandbox", "mixed_terminal_protocol_smoke"],
        "allow_live_submit": True,
        "allow_registration": False,
        "allow_single_worker_smoke": True,
        "allow_mixed_worker": False,
        "expires_at": (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        "revocation_id": "",
        "operator_notes": "Run-local authorization artifact for one current-prompt mixed no-op sandbox task.",
        "risk_acknowledgements": ["No business repo writes.", "No registry writes.", "No mixed business task."],
        "required_preflight_checks": [
            "mixed-noop-live-plan",
            "selected-worker-locks-clear",
            "watcher-alerts-zero",
        ],
        "required_evidence_paths": [str(task_dir)],
        "signed_by_user_text": "User explicitly authorized one OpenCode + Claude mixed no-op live run.",
        "explicit_user_confirmation_text": "用户明确授权执行第一次 OpenCode + Claude mixed no-op live，限定 sandbox/no-op/protocol smoke，禁止业务仓库写入、registry 写入、.omx/state/team 写入和 mixed business task。",
    }


def _wait_for_task_dir(task_dir: Path, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + max(1, timeout)
    while time.monotonic() < deadline:
        blocks = _task_dir_blocks(task_dir)
        if not blocks:
            return {"ok": True, "blocked_reasons": []}
        time.sleep(1)
    return {"ok": False, "blocked_reasons": _task_dir_blocks(task_dir) or ["task did not reach terminal marker before timeout"]}


def _task_dir_blocks(task_dir: Path, worker_id: str | None = None) -> list[str]:
    blocked = []
    report = task_dir / "report.md"
    markers = [marker for marker in TERMINAL_MARKERS if (task_dir / marker).is_file()]
    label = worker_id or task_dir.name
    if not report.is_file() or not report.read_text(encoding="utf-8", errors="replace").strip():
        blocked.append(f"{label} report.md missing or empty")
    if len(markers) != 1:
        blocked.append(f"{label} terminal marker count must be exactly one")
    elif markers[0] != ".done":
        blocked.append(f"{label} terminal marker is not .done: {markers[0]}")
    return blocked


def _worker_result(
    worker_id: str,
    task_dir: str,
    *,
    expected_task_dir: str = "",
    visible_run_id: str = "",
    visible_task_id: str = "",
    collection_source: str = "",
    child_created: bool = True,
    prompt_delivered: bool = False,
    submitted: bool | None = None,
    pre_submit_readiness_not_safe: bool = False,
    task_start_unconfirmed: bool = False,
) -> dict[str, Any]:
    task_path = Path(task_dir)
    truth = _task_truth(task_path)
    report = str(task_path / "report.md")
    marker_writes = [str(task_path / marker) for marker in TERMINAL_MARKERS if (task_path / marker).is_file()]
    return {
        "worker_id": worker_id,
        "task_dir": task_dir,
        "expected_task_dir": expected_task_dir or task_dir,
        "visible_run_id": visible_run_id,
        "visible_task_id": visible_task_id,
        "collection_source": collection_source,
        "child_created": child_created,
        "prompt_delivered": prompt_delivered,
        "submitted": _task_start_confirmed(task_path) if submitted is None else submitted,
        "pre_submit_readiness_not_safe": pre_submit_readiness_not_safe,
        "task_start_unconfirmed": task_start_unconfirmed,
        "observed_writes": [],
        "protocol_observed_writes": [report] + marker_writes,
        **truth,
    }


def _task_truth(task_dir: Path) -> dict[str, Any]:
    report = task_dir / "report.md"
    markers = [marker for marker in TERMINAL_MARKERS if (task_dir / marker).is_file()]
    protocol = _read_json_or_empty(task_dir / "protocol_status.json")
    delivery = _read_json_or_empty(task_dir / "delivery.json")
    marker = markers[0] if len(markers) == 1 else ""
    return {
        "report_exists": report.is_file(),
        "report_bytes": report.stat().st_size if report.is_file() else 0,
        "terminal_marker_count": len(markers),
        "terminal_marker": marker,
        "received": (task_dir / ".received").is_file(),
        "started": (task_dir / ".started").is_file(),
        "delivery_status": str(protocol.get("status") or delivery.get("status") or ""),
        "terminal_status": _terminal_status(task_dir, child_created=task_dir.is_dir()),
    }


def _terminal_status(task_dir: Path, *, child_created: bool) -> str:
    if not child_created:
        return "child_not_created"
    report = task_dir / "report.md"
    markers = [marker for marker in TERMINAL_MARKERS if (task_dir / marker).is_file()]
    if not report.is_file() or not report.read_text(encoding="utf-8", errors="replace").strip():
        return "child_missing_report"
    if not markers:
        return "child_missing_marker"
    if len(markers) > 1:
        return "child_conflicting_markers"
    return "child_terminal_valid"


def _classify_child_result(item: dict[str, Any]) -> dict[str, Any]:
    worker_id = str(item.get("worker_id", ""))
    task_dir = Path(str(item.get("task_dir", ""))).resolve() if item.get("task_dir") else Path()
    child_created = bool(item.get("child_created", True)) and bool(item.get("task_dir"))
    terminal_status = _terminal_status(task_dir, child_created=child_created)
    if item.get("path_mapping_error"):
        terminal_status = "child_path_mapping_error"
    if item.get("pre_submit_readiness_not_safe"):
        terminal_status = "child_pre_submit_readiness_not_safe"
    if item.get("task_start_unconfirmed"):
        terminal_status = "child_task_start_unconfirmed"
    truth = _task_truth(task_dir) if child_created else {}
    return {
        "worker_id": worker_id,
        "terminal_status": terminal_status,
        "task_dir": str(task_dir) if child_created else "",
        "expected_task_dir": str(item.get("expected_task_dir", "")),
        "collection_source": str(item.get("collection_source", "")),
        "report_exists": truth.get("report_exists", False),
        "report_bytes": truth.get("report_bytes", 0),
        "terminal_marker_count": truth.get("terminal_marker_count", 0),
        "terminal_marker": truth.get("terminal_marker", ""),
        "received": truth.get("received", False),
        "started": truth.get("started", False),
        "delivery_status": truth.get("delivery_status", ""),
    }


def _task_start_confirmed(task_dir: Path) -> bool:
    if not task_dir.is_dir():
        return False
    if (task_dir / ".received").is_file() or (task_dir / ".started").is_file():
        return True
    if (task_dir / "report.md").is_file():
        return True
    return any((task_dir / marker).is_file() for marker in TERMINAL_MARKERS)


def _child_submitted(item: dict[str, Any], contract_workers: dict[str, dict[str, Any]]) -> bool:
    worker_id = str(item.get("worker_id", ""))
    worker = contract_workers.get(worker_id, {})
    if _backend_kind(worker) == "opencode_tui":
        return bool(item.get("child_created", False))
    if _is_claude_worker_id(worker_id):
        return bool(item.get("submitted") and not item.get("task_start_unconfirmed"))
    return bool(item.get("child_created", False))


def _parse_visible_dispatch_task_dir(root: Path, stdout: str) -> Path | None:
    candidates: list[Path] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not any(stripped.startswith(prefix) for prefix in ("path:", "task:", "task_dir:")):
            continue
        value = stripped.split(":", 1)[1].strip()
        if not value or "visible-runs" not in value or "/tasks/" not in value:
            continue
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        if path.name.startswith("task-"):
            candidates.append(path.resolve())
    return candidates[-1] if candidates else None


def _visible_run_task_ids(task_dir: Path | None) -> tuple[str, str]:
    if not task_dir:
        return "", ""
    task_id = task_dir.name
    run_id = task_dir.parent.parent.name if task_dir.parent.name == "tasks" else ""
    return run_id, task_id


def _aggregate_state(worker_results: list[dict[str, Any]], blocked: list[str]) -> str:
    if blocked or len(worker_results) != 2:
        return "blocked"
    markers = []
    for item in worker_results:
        task_dir = Path(str(item.get("task_dir", "")))
        found = [marker for marker in TERMINAL_MARKERS if (task_dir / marker).is_file()]
        markers.extend(found)
    if any(marker == ".fail" for marker in markers):
        return "failed"
    if any(marker == ".blocked" for marker in markers):
        return "blocked"
    if len(markers) == 2 and all(marker == ".done" for marker in markers):
        return "completed"
    return "blocked"


def _registered_claude_worker(registry_path: Path, worker_id: str) -> dict[str, Any]:
    payload = _read_json(registry_path)
    workers = payload.get("workers") or payload.get("registered_workers") or []
    if isinstance(workers, dict):
        return workers.get(worker_id, {})
    return next((item for item in workers if isinstance(item, dict) and item.get("worker_id") == worker_id), {})


def _workers_by_id(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("worker_id", "")): item for item in contract.get("workers", []) if isinstance(item, dict)}


def _selected_worker_ids(contract: dict[str, Any], include_workers: list[str]) -> tuple[str, str]:
    opencode_ids = _worker_ids_by_backend(contract, "opencode_tui", include_workers)
    claude_ids = _worker_ids_by_backend(contract, "claude_code", include_workers)
    return (opencode_ids[0] if opencode_ids else "", claude_ids[0] if claude_ids else "")


def _worker_ids_by_backend(contract: dict[str, Any], backend_kind: str, include_workers: list[str] | None = None) -> list[str]:
    include = set(include_workers or [])
    result = []
    for worker_id, worker in _workers_by_id(contract).items():
        if include and worker_id not in include:
            continue
        if _backend_kind(worker) == backend_kind:
            result.append(worker_id)
    return sorted(result)


def _backend_kind(worker: dict[str, Any]) -> str:
    return str(worker.get("backend_kind") or worker.get("backend") or "")


def _is_claude_worker_id(worker_id: str) -> bool:
    return worker_id.startswith("vc-") or "claude" in worker_id


def _run_command(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    try:
        result = subprocess.run(argv, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
        return {"argv": argv, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "timed_out": False}
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or exc.output or ""
        stderr = exc.stderr or "command timed out"
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return {
            "argv": argv,
            "returncode": 124,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
            "timeout_seconds": timeout,
        }


def _command_evidence(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "argv": list(result.get("argv", [])),
        "returncode": result.get("returncode"),
        "stdout": str(result.get("stdout", ""))[:8000],
        "stderr": str(result.get("stderr", ""))[:8000],
        "timed_out": bool(result.get("timed_out")),
    }


def _json_stdout(result: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(str(result.get("stdout", "")))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _hashes(root: Path, request: MixedNoopLiveRequest) -> dict[str, str]:
    return {
        "registered_worker_registry": _sha256(Path(request.registered_worker_registry_path).resolve()),
        "opencode_visible_registry": _sha256(Path(request.opencode_visible_registry_path).resolve()),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _state_team_snapshot(root: Path) -> list[str]:
    path = root / STATE_TEAM_REL
    if not path.is_dir():
        return []
    return sorted(f"{item.name}\t{item.stat().st_mtime_ns}\t{item.stat().st_size}" for item in path.iterdir() if item.is_file())


def _git_status(repo: str) -> list[str]:
    path = Path(repo)
    if not (path / ".git").is_dir():
        return []
    result = subprocess.run(["git", "-C", str(path), "status", "--short"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return result.stdout.splitlines()


def _git_index(repo: str) -> list[str]:
    path = Path(repo)
    if not (path / ".git").is_dir():
        return []
    result = subprocess.run(["git", "-C", str(path), "diff", "--cached", "--name-only"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return result.stdout.splitlines()


def _bin(root: Path, explicit: str, name: str) -> Path:
    return Path(explicit).resolve() if explicit else root / "bin" / name


def _worker_backend_bin(root: Path, request: MixedNoopLiveRequest) -> Path:
    return _bin(root, request.worker_backend_bin, "omx_worker_backend")


def _run_root(root: Path, run_id: str) -> Path:
    return root / ".omx" / "framework" / "team" / "mixed-noop-runs" / run_id


def _default_run_id() -> str:
    return "mixed-noop-live-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return data


def _read_json_or_empty(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _dedupe(items: list[Any]) -> list[Any]:
    out = []
    seen = set()
    for item in items:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item)
        if key not in seen and item:
            seen.add(key)
            out.append(item)
    return out


def _forbidden_next_actions() -> list[str]:
    return [
        "business repo writes",
        "registered worker registry writes",
        "OpenCode visible registry writes",
        ".omx/state/team writes",
        "raw tmux input outside controlled Claude wrapper",
        "direct /tui/*",
        "direct adapter submit",
        "rmux live",
        "worker substitution",
        "automatic next stage",
    ]


__all__ = [
    "MixedNoopLiveRequest",
    "build_mixed_noop_live_plan",
    "execute_mixed_noop_live",
    "validate_mixed_noop_live_result_file",
]
