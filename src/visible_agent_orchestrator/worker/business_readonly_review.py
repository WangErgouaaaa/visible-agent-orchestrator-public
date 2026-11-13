"""Controlled business-readonly review runner.

The live path is intentionally narrow: one explicitly selected visible
OpenCode worker, existing dispatch wrapper only, no business writes, no
registry writes, no state-team writes, and no retries.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime_paths import workspace_root

from .mixed_noop_live_runner import (
    CommandRunner,
    _bin,
    _dedupe,
    _default_run_id,
    _git_index,
    _git_status,
    _parse_visible_dispatch_task_dir,
    _run_command,
    _utc_now,
)


BUSINESS_READONLY_PROFILE_ID = "business-readonly-review-v1"
BUSINESS_REPO_ROOT = str(workspace_root())
RUN_ROOT_REL = Path(".omx/framework/team/business-readonly-review-runs")
REGISTERED_REGISTRY_REL = Path(".omx/framework/team/registered-workers/registered-workers.json")
OPENCODE_REGISTRY_REL = Path(".omx/framework/team/opencode_visible_workers.json")
STATE_TEAM_REL = Path(".omx/state/team")
TERMINAL_MARKERS = [".done", ".fail", ".blocked"]


@dataclass(frozen=True)
class BusinessReadonlyReviewPlan:
    schema_version: int
    plan_kind: str
    ok: bool
    profile_id: str
    worker_id: str
    target_repo: str
    run_root: str
    would_execute_live: bool
    would_write_business: bool
    would_write_registry: bool
    would_write_state_team: bool
    blocked_reasons: list[str]
    prompt: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BusinessReadonlyReviewRunEnvelope:
    schema_version: int
    envelope_kind: str
    run_id: str
    run_root: str
    profile_id: str
    worker_id: str
    target_repo: str
    prompt_path: str
    profile_snapshot_path: str
    pre_snapshot_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BusinessReadonlyReviewExecutionDecision:
    schema_version: int
    decision_kind: str
    ok: bool
    worker_id: str
    blocked_reasons: list[str]
    would_execute_live: bool
    would_write_business: bool
    would_write_registry: bool
    would_write_state_team: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BusinessReadonlyReviewResult:
    schema_version: int
    result_kind: str
    ok: bool
    profile_id: str
    worker_id: str
    run_id: str
    run_root: str
    task_dir: str
    result_path: str
    blocked_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BusinessReadonlyReviewValidation:
    schema_version: int
    validation_kind: str
    ok: bool
    result_path: str
    profile_id: str
    terminal_marker: str
    terminal_marker_count: int
    blocked_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_business_readonly_review_prompt(*, profile_id: str, target_repo: str = BUSINESS_REPO_ROOT) -> str:
    return "\n".join(
        [
            f"Profile: {profile_id}",
            "This is read-only review.",
            f"Target repo path: {target_repo}",
            "",
            "Forbidden:",
            "- modifying files",
            "- git add",
            "- git commit",
            "- git checkout",
            "- git reset",
            "- git clean",
            "- git stash",
            "- writing .omx/state/team",
            "- registry writes",
            "- raw tmux",
            "- direct TUI",
            "- rmux",
            "",
            "Allowed readonly commands:",
            "- git status --short",
            "- git diff --name-only",
            "- git diff --cached --name-only",
            "- git diff --check",
            "- git diff --cached --check",
            "- python3 -m unittest ...",
            "- python3 src/tools/... --self-test",
            "",
            "Required outputs:",
            "- report.md",
            "- exactly one terminal marker",
            "",
            "Report must include files inspected, commands run, command results, no-write proof,",
            "business repo pre/post unchanged summary, and audit conclusion.",
            "If unable to comply, write report.md and touch .blocked.",
            "Do not mark completed without non-empty report.md.",
        ]
    )


def build_business_readonly_review_plan(request: Any, profile: dict[str, Any]) -> dict[str, Any]:
    root = Path(request.root).resolve()
    worker_id = str(getattr(request, "worker_id", "") or "")
    target_repo = str(getattr(request, "business_repo_path", "") or BUSINESS_REPO_ROOT)
    run_id = str(getattr(request, "run_id", "") or _business_run_id())
    blockers = _decision_blockers(request, profile, worker_id=worker_id, target_repo=target_repo)
    plan = BusinessReadonlyReviewPlan(
        schema_version=1,
        plan_kind="business-readonly-review-plan",
        ok=not blockers,
        profile_id=str(profile.get("profile_id", "")),
        worker_id=worker_id,
        target_repo=target_repo,
        run_root=str(root / RUN_ROOT_REL / run_id),
        would_execute_live=False,
        would_write_business=False,
        would_write_registry=False,
        would_write_state_team=False,
        blocked_reasons=blockers,
        prompt=build_business_readonly_review_prompt(profile_id=str(profile.get("profile_id", "")), target_repo=target_repo),
    )
    return plan.to_dict()


def execute_business_readonly_review(
    request: Any,
    *,
    profile: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    profile = profile or _read_profile(getattr(request, "profile_path", ""))
    if not getattr(request, "allow_profile_live", False):
        return _blocked_result(profile, ["execute-with-profile requires --allow-profile-live"], request, plan=plan)
    if validation is not None and not validation.get("ok"):
        return _blocked_result(profile, list(validation.get("blocked_reasons", [])), request, plan=plan)
    root = Path(request.root).resolve()
    worker_id = str(getattr(request, "worker_id", "") or "")
    target_repo = str(getattr(request, "business_repo_path", "") or BUSINESS_REPO_ROOT)
    run_id = str(getattr(request, "run_id", "") or _business_run_id())
    blockers = _decision_blockers(request, profile, worker_id=worker_id, target_repo=target_repo)
    if plan is not None and not plan.get("ok"):
        blockers.extend(plan.get("blockers", []))
    if blockers:
        return _blocked_result(profile, blockers, request, plan=plan)

    run_root = root / RUN_ROOT_REL / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    prompt = build_business_readonly_review_prompt(profile_id=str(profile.get("profile_id", "")), target_repo=target_repo)
    prompt_path = run_root / "task-prompt.md"
    profile_snapshot_path = run_root / "profile-snapshot.json"
    pre_snapshot_path = run_root / "pre-snapshot.json"
    prompt_path.write_text(prompt, encoding="utf-8")
    profile_snapshot_path.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    pre = _snapshots(root, Path(target_repo), request)
    pre_snapshot_path.write_text(json.dumps(pre, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    envelope = BusinessReadonlyReviewRunEnvelope(
        schema_version=1,
        envelope_kind="business-readonly-review-run-envelope",
        run_id=run_id,
        run_root=str(run_root),
        profile_id=str(profile.get("profile_id", "")),
        worker_id=worker_id,
        target_repo=target_repo,
        prompt_path=str(prompt_path),
        profile_snapshot_path=str(profile_snapshot_path),
        pre_snapshot_path=str(pre_snapshot_path),
    )
    (run_root / "run-envelope.json").write_text(json.dumps(envelope.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    dispatch_bin = _bin(root, getattr(request, "dispatch_bin", ""), "omx_visible_dispatch")
    command = [
        str(dispatch_bin),
        "run-one",
        "--root",
        str(root),
        "--worker",
        worker_id,
        "--title",
        "business-readonly-review-v1",
        "--body",
        prompt,
        "--timeout",
        str(int(getattr(request, "timeout_seconds", 240) or 240)),
        "--session-action",
        "rotate",
        "--session-action-reason",
        "role_isolation",
        "--rotation-reason",
        "role_isolation",
        "--rehydration-packet-type",
        "clean-room-review",
        "--allow-real",
    ]
    dispatch_result = (command_runner or _run_command)(command, root, int(getattr(request, "timeout_seconds", 240) or 240))
    task_dir = _parse_visible_dispatch_task_dir(root, str(dispatch_result.get("stdout", "")))
    post = _snapshots(root, Path(target_repo), request)

    terminal_markers = _terminal_markers(task_dir)
    result_path = run_root / "result.json"
    report_path = task_dir / "report.md" if task_dir else run_root / "missing-report.md"
    result = {
        "schema_version": 1,
        "result_kind": "business-readonly-review-result",
        "ok": False,
        "profile_id": str(profile.get("profile_id", "")),
        "profile_business_repo_writes_allowed": bool(profile.get("business_repo_writes_allowed", False)),
        "worker_id": worker_id,
        "worker_switch_occurred": False,
        "run_id": run_id,
        "run_root": str(run_root),
        "task_dir": str(task_dir) if task_dir else "",
        "report_path": str(report_path),
        "terminal_markers": terminal_markers,
        "pre_business_repo_status": pre["business_repo_status"],
        "post_business_repo_status": post["business_repo_status"],
        "pre_business_repo_cached_files": pre["business_repo_cached_files"],
        "post_business_repo_cached_files": post["business_repo_cached_files"],
        "pre_registry_hashes": pre["registry_hashes"],
        "post_registry_hashes": post["registry_hashes"],
        "pre_state_team_hash": pre["state_team_hash"],
        "post_state_team_hash": post["state_team_hash"],
        "used_raw_tmux": False,
        "used_direct_tui": False,
        "used_direct_adapter": False,
        "used_rmux": False,
        "would_execute_live": True,
        "would_write_business": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "dispatch_command": command,
        "dispatch_result": dispatch_result,
        "profile_plan": plan or {},
    }
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    validation_result = validate_business_readonly_review_result_file(result_path)
    result["ok"] = bool(validation_result.get("ok"))
    result["blocked_reasons"] = validation_result.get("blocked_reasons", [])
    result["validation"] = validation_result
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (run_root / "final-report.md").write_text(_final_report(result), encoding="utf-8")
    result["result_path"] = str(result_path)
    return result


def validate_business_readonly_review_result_file(path: Path | str) -> dict[str, Any]:
    result_path = Path(path).resolve()
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _validation(False, ["result JSON missing"], result_path, {}, "", 0)
    except json.JSONDecodeError as exc:
        return _validation(False, [f"result JSON malformed: {exc.msg}"], result_path, {}, "", 0)
    if not isinstance(payload, dict):
        return _validation(False, ["result JSON must contain an object"], result_path, {}, "", 0)
    blockers: list[str] = []
    if payload.get("profile_id") != BUSINESS_READONLY_PROFILE_ID:
        blockers.append(f"profile id must be {BUSINESS_READONLY_PROFILE_ID}")
    if payload.get("profile_business_repo_writes_allowed") is not False:
        blockers.append("profile business_repo_writes_allowed must be false")
    task_dir = _resolve_result_path(result_path, str(payload.get("task_dir", "")))
    if not task_dir.exists():
        blockers.append("task_dir must exist")
    report_path = _resolve_result_path(result_path, str(payload.get("report_path", "report.md")))
    if not report_path.exists() or report_path.stat().st_size <= 0:
        blockers.append("report.md must be non-empty")
    markers = _terminal_markers(task_dir)
    if not markers:
        markers = [str(item) for item in payload.get("terminal_markers", []) if str(item) in TERMINAL_MARKERS]
    marker = markers[0] if len(markers) == 1 else ""
    if len(markers) != 1:
        blockers.append("exactly one terminal marker is required")
    elif marker not in TERMINAL_MARKERS:
        blockers.append("terminal marker must be .done, .fail, or .blocked")
    business_repo_changed = str(payload.get("pre_business_repo_status", "")) != str(payload.get("post_business_repo_status", ""))
    business_index_changed = list(payload.get("pre_business_repo_cached_files", [])) != list(payload.get("post_business_repo_cached_files", []))
    registry_changed = dict(payload.get("pre_registry_hashes", {})) != dict(payload.get("post_registry_hashes", {}))
    state_team_changed = str(payload.get("pre_state_team_hash", "")) != str(payload.get("post_state_team_hash", ""))
    if business_repo_changed:
        blockers.append("business repo status changed")
    if business_index_changed:
        blockers.append("business repo cached index changed")
    if registry_changed:
        blockers.append("registry hashes changed")
    if state_team_changed:
        blockers.append(".omx/state/team hash changed")
    if any(bool(payload.get(key, False)) for key in ("used_raw_tmux", "used_direct_tui", "used_direct_adapter", "used_rmux")):
        blockers.append("raw tmux/direct TUI/direct adapter/rmux flags must all be false")
    if bool(payload.get("worker_switch_occurred", False)):
        blockers.append("worker switch must be false")
    if any(bool(payload.get(key, False)) for key in ("would_write_business", "would_write_registry", "would_write_state_team")):
        blockers.append("write intent flags must all be false")
    validation = _validation(not blockers, _dedupe(blockers), result_path, payload, marker, len(markers))
    validation["business_repo_changed"] = business_repo_changed
    validation["business_index_changed"] = business_index_changed
    validation["registry_changed"] = registry_changed
    validation["state_team_changed"] = state_team_changed
    validation["task_dir"] = str(task_dir)
    validation["report_path"] = str(report_path)
    return validation


def _decision_blockers(request: Any, profile: dict[str, Any], *, worker_id: str, target_repo: str) -> list[str]:
    blockers: list[str] = []
    if profile.get("profile_id") != BUSINESS_READONLY_PROFILE_ID:
        blockers.append(f"profile id must be {BUSINESS_READONLY_PROFILE_ID}")
    if profile.get("business_repo_writes_allowed") is not False:
        blockers.append("business readonly live requires business_repo_writes_allowed=false")
    if not worker_id:
        blockers.append("business readonly live requires --worker-id until a default reviewer is defined")
    elif worker_id not in [str(item) for item in profile.get("allowed_workers", [])]:
        blockers.append(f"worker_id not allowed by profile: {worker_id}")
    if not target_repo:
        blockers.append("business repo path is required")
    return _dedupe(blockers)


def _read_profile(path: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _snapshots(root: Path, business_repo: Path, request: Any) -> dict[str, Any]:
    registered = Path(getattr(request, "registered_worker_registry_path", "")).resolve()
    opencode = Path(getattr(request, "opencode_visible_registry_path", "")).resolve()
    return {
        "business_repo_status": _git_status(str(business_repo)),
        "business_repo_cached_files": _git_index(str(business_repo)),
        "registry_hashes": {"registered": _file_hash(registered), "opencode": _file_hash(opencode)},
        "state_team_hash": _tree_hash(root / STATE_TEAM_REL),
    }


def _terminal_markers(task_dir: Path) -> list[str]:
    if not task_dir or not task_dir.exists():
        return []
    return [marker for marker in TERMINAL_MARKERS if (task_dir / marker).exists()]


def _resolve_result_path(result_path: Path, value: str) -> Path:
    if not value:
        return result_path.parent
    path = Path(value)
    if path.is_absolute():
        return path
    return (result_path.parent / path).resolve()


def _blocked_result(profile: dict[str, Any], blockers: list[str], request: Any, *, plan: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result_kind": "business-readonly-review-execution",
        "ok": False,
        "profile_id": str(profile.get("profile_id", "")),
        "profile_path": str(Path(getattr(request, "profile_path", "")).resolve()),
        "blocked_reasons": _dedupe(blockers),
        "commands": [],
        "profile_plan": plan or {},
        "would_execute_live": False,
        "would_write_business": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "would_use_raw_tmux": False,
        "would_use_direct_tui": False,
        "would_use_direct_adapter": False,
        "would_use_rmux": False,
    }


def _validation(ok: bool, blockers: list[str], result_path: Path, payload: dict[str, Any], marker: str, marker_count: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "validation_kind": "business-readonly-review-result-validation",
        "ok": ok,
        "result_path": str(result_path),
        "profile_id": str(payload.get("profile_id", "")),
        "terminal_marker": marker,
        "terminal_marker_count": marker_count,
        "blocked_reasons": blockers,
    }


def _final_report(result: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Business Readonly Review Result",
            "",
            f"- ok: {bool(result.get('ok'))}",
            f"- profile: {result.get('profile_id', '')}",
            f"- worker: {result.get('worker_id', '')}",
            f"- business_repo_unchanged: {result.get('pre_business_repo_status') == result.get('post_business_repo_status')}",
            f"- business_index_unchanged: {result.get('pre_business_repo_cached_files') == result.get('post_business_repo_cached_files')}",
            f"- registry_unchanged: {result.get('pre_registry_hashes') == result.get('post_registry_hashes')}",
            f"- state_team_unchanged: {result.get('pre_state_team_hash') == result.get('post_state_team_hash')}",
            "",
        ]
    )


def _business_run_id() -> str:
    return _default_run_id().replace("mixed-noop-live", "business-readonly-review", 1)


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _tree_hash(path: Path) -> str:
    if not path.is_dir():
        return "missing"
    entries = []
    for item in sorted(path.rglob("*")):
        if item.is_file():
            rel = item.relative_to(path)
            entries.append(f"{rel}\t{item.stat().st_size}\t{_file_hash(item)}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
