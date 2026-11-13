"""Reusable authorization profiles for controlled worker flows.

Profiles are durable policy presets. They do not grant live execution by
themselves; the live path still requires an explicit per-command allow flag.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from ..runtime_paths import framework_root, state_team_root, workspace_root



PROFILE_VERSION = 1
BUSINESS_READONLY_PROFILE_ID = "business-readonly-review-v1"
BUSINESS_READONLY_KIND = "business_readonly_review"
BUSINESS_SCOPED_WRITE_PROFILE_ID = "business-scoped-write-v1"
BUSINESS_SCOPED_WRITE_DISPLAY_ID = "business-scoped-write-v1.disabled"
BUSINESS_SCOPED_WRITE_KIND = "business_scoped_write"
BUSINESS_REPO_ROOT = str(workspace_root())
FRAMEWORK_ROOT = str(framework_root())
STATE_TEAM_ROOT = str(state_team_root())
REGISTERED_REGISTRY = str(framework_root() / "registered-workers" / "registered-workers.json")
OPENCODE_REGISTRY = str(framework_root() / "opencode_visible_workers.json")
PREP_MAX_AGE_SECONDS = 24 * 60 * 60
PROFILE_DIR_REL = Path("worker-backends/authorization-profiles")
DEFAULTS_REL = PROFILE_DIR_REL / "defaults.json"
CommandRunner = Callable[[list[str], Path, int], dict[str, Any]]


@dataclass(frozen=True)
class AuthorizationProfileRequest:
    root: str
    profile_path: str
    registered_worker_registry_path: str
    opencode_visible_registry_path: str
    execution_mode: str = "no-live"
    allow_profile_live: bool = False
    timeout_seconds: int = 240
    run_id: str = ""
    dispatch_bin: str = ""
    worker_backend_bin: str = ""
    watcher_bin: str = ""
    live_enable_config_path: str = ""
    worker_id: str = ""
    business_repo_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def list_authorization_profiles(root: Path | str) -> dict[str, Any]:
    base = Path(root).resolve() / PROFILE_DIR_REL
    profiles: list[dict[str, Any]] = []
    errors: list[str] = []
    if not base.exists():
        return {
            "schema_version": 1,
            "ok": True,
            "profile_dir": str(base),
            "profiles": profiles,
            "warnings": [f"profile directory missing: {base}"],
            "errors": [],
        }
    for path in sorted(base.glob("*.json")):
        if path.name == DEFAULTS_REL.name:
            continue
        result = validate_authorization_profile_file(path)
        if result.get("ok"):
            enabled = bool(result.get("enabled", True))
            display_id = str(result["profile_id"])
            if result.get("profile_kind") == BUSINESS_SCOPED_WRITE_KIND and not enabled:
                display_id = BUSINESS_SCOPED_WRITE_DISPLAY_ID
            profiles.append(
                {
                    "profile_id": result["profile_id"],
                    "display_id": display_id,
                    "profile_version": result["profile_version"],
                    "profile_kind": result["profile_kind"],
                    "enabled": enabled,
                    "path": str(path),
                    "description": result.get("description", ""),
                }
            )
        else:
            errors.append(f"{path}: {'; '.join(result.get('blocked_reasons', []))}")
    return {
        "schema_version": 1,
        "ok": not errors,
        "profile_dir": str(base),
        "profiles": profiles,
        "warnings": [],
        "errors": errors,
    }


def default_authorization_profile(root: Path | str, *, kind: str = BUSINESS_READONLY_KIND) -> dict[str, Any]:
    defaults_path = Path(root).resolve() / DEFAULTS_REL
    result = validate_authorization_profile_defaults_file(defaults_path, root=Path(root).resolve())
    if kind == "business_readonly_review":
        return {
            "schema_version": 1,
            "ok": bool(result.get("ok")),
            "kind": "business_readonly_review",
            "defaults_path": str(defaults_path),
            "profile_id": result.get("default_business_readonly_review_profile", ""),
            "profile_path": result.get("default_business_readonly_review_profile_path", ""),
            "live_requires_explicit_allow": bool(result.get("default_business_readonly_live_requires_explicit_allow", False)),
            "business_repo_writes_allowed": False,
            "business_ready_profile_present": True,
            "business_scoped_write_profile_enabled": bool(result.get("business_scoped_write_enabled_by_default", False)),
            "next_gate": "business_scoped_write_profile_review",
            "blocked_reasons": result.get("blocked_reasons", []),
        }
    return {
        "schema_version": 1,
        "ok": False,
        "kind": kind,
        "blocked_reasons": [f"unsupported default authorization profile kind: {kind}"],
    }


def validate_authorization_profile_defaults_file(path: Path | str, *, root: Path | str | None = None) -> dict[str, Any]:
    defaults_path = Path(path).resolve()
    base_root = Path(root).resolve() if root is not None else defaults_path.parents[2]
    try:
        payload = json.loads(defaults_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _defaults_validation(False, ["authorization profile defaults missing"], defaults_path, base_root, {})
    except json.JSONDecodeError as exc:
        return _defaults_validation(False, [f"authorization profile defaults JSON malformed: {exc.msg}"], defaults_path, base_root, {})
    if not isinstance(payload, dict):
        return _defaults_validation(False, ["authorization profile defaults must contain an object"], defaults_path, base_root, {})
    blockers: list[str] = []
    if int(payload.get("schema_version", 0) or 0) != 1:
        blockers.append("defaults schema_version must be 1")
    if payload.get("defaults_kind") != "authorization-profile-defaults":
        blockers.append("defaults_kind must be authorization-profile-defaults")
    business_readonly_path = _resolve_under_root(base_root, str(payload.get("default_business_readonly_review_profile_path", "")))
    if payload.get("default_business_readonly_review_profile") != BUSINESS_READONLY_PROFILE_ID:
        blockers.append(f"default_business_readonly_review_profile must be {BUSINESS_READONLY_PROFILE_ID}")
    if payload.get("default_business_readonly_live_requires_explicit_allow") is not True:
        blockers.append("default_business_readonly_live_requires_explicit_allow must be true")
    if not business_readonly_path.is_file():
        blockers.append("default business readonly review profile file missing")
    else:
        blockers.extend(validate_authorization_profile_file(business_readonly_path).get("blocked_reasons", []))
    scoped_value = str(payload.get("default_business_scoped_write_profile") or "")
    scoped_path = _resolve_under_root(base_root, str(payload.get("default_business_scoped_write_profile_path", "")))
    if scoped_value not in {"", BUSINESS_SCOPED_WRITE_DISPLAY_ID}:
        blockers.append(f"default_business_scoped_write_profile must be {BUSINESS_SCOPED_WRITE_DISPLAY_ID} or empty")
    if payload.get("business_scoped_write_enabled_by_default") is not False:
        blockers.append("business_scoped_write_enabled_by_default must be false")
    if scoped_value and not scoped_path.is_file():
        blockers.append("default business scoped write profile file missing")
    elif scoped_value:
        scoped_validation = validate_authorization_profile_file(scoped_path)
        blockers.extend(scoped_validation.get("blocked_reasons", []))
        if scoped_validation.get("enabled") is not False:
            blockers.append("default business scoped write profile must be disabled")
    forbidden = _list(payload.get("forbidden_by_default"))
    for item in (
        "business writes unless write-scope profile explicitly enabled",
        "registry writes",
        ".omx/state/team writes",
        "raw tmux",
        "direct TUI",
        "direct adapter",
        "rmux",
        "worker switch",
        "retry",
    ):
        if item not in forbidden:
            blockers.append(f"forbidden_by_default must include {item}")
    if payload.get("next_gate") != "business-readiness authorization profile, no live":
        blockers.append("next_gate must be business-readiness authorization profile, no live")
    if payload.get("next_business_gate") != "business-scoped-write-v1 requires explicit write-scope contract":
        blockers.append("next_business_gate must be business-scoped-write-v1 requires explicit write-scope contract")
    result = _defaults_validation(not _dedupe(blockers), _dedupe(blockers), defaults_path, base_root, payload)
    result["default_business_readonly_review_profile_path"] = str(business_readonly_path)
    result["default_business_scoped_write_profile_path"] = str(scoped_path) if scoped_value else ""
    return result


def profile_summary(profile_path: Path | str) -> dict[str, Any]:
    profile = load_authorization_profile_file(profile_path)
    validation = validate_authorization_profile(profile, profile_path=str(Path(profile_path).resolve()))
    retry = profile.get("retry_policy") if isinstance(profile.get("retry_policy"), dict) else {}
    return {
        "schema_version": 1,
        "summary_kind": "authorization-profile-summary",
        "ok": bool(validation.get("ok")),
        "profile_id": str(profile.get("profile_id", "")),
        "profile_version": int(profile.get("profile_version", 0) or 0),
        "profile_kind": str(profile.get("profile_kind", "")),
        "enabled": bool(profile.get("enabled", True)),
        "profile_path": str(Path(profile_path).resolve()),
        "workers": _list(profile.get("allowed_workers")),
        "scenario": _list(profile.get("allowed_scenarios"))[0] if _list(profile.get("allowed_scenarios")) else "",
        "wave_model": str(profile.get("wave_model", "")),
        "max_parallel": int(profile.get("max_parallel", 0) or 0),
        "live_requires_explicit_allow": bool(profile.get("live_requires_explicit_allow", False)),
        "live_allowed": bool(profile.get("live_allowed", False)),
        "business_task_allowed": bool(profile.get("business_task_allowed", False)),
        "business_repo_writes_allowed": bool(profile.get("business_repo_writes_allowed", False)),
        "max_live_attempts": int(profile.get("max_live_attempts", 0) or 0),
        "retry_policy": retry,
        "forbidden_matrix": {
            "business_writes_allowed": bool(profile.get("business_repo_writes_allowed", False)),
            "registry_writes_allowed": bool(profile.get("allowed_registry_updates", False)),
            "state_team_writes_allowed": False,
            "raw_tmux_allowed": bool(profile.get("raw_tmux_allowed", False)),
            "direct_tui_allowed": bool(profile.get("direct_tui_allowed", False)),
            "direct_adapter_allowed": bool(profile.get("direct_adapter_allowed", False)),
            "rmux_allowed": bool(profile.get("rmux_allowed", False)),
            "worker_switch_allowed": bool(profile.get("worker_switch_allowed", False)),
            "retry_allowed": str(retry.get("kind", "")) != "none",
        },
        "allowed_read_roots": _list(profile.get("allowed_read_roots")),
        "allowed_write_roots": _list(profile.get("allowed_write_roots")),
        "forbidden_write_roots": _list(profile.get("forbidden_write_roots")),
        "allowed_commands": _list(profile.get("allowed_commands")),
        "forbidden_commands": _list(profile.get("forbidden_commands")),
        "required_outputs": _list(profile.get("required_outputs")),
        "result_requirements": _list(profile.get("required_result_validation")),
        "terminal_truth_requirements": _list(profile.get("required_terminal_truth")),
        "blocked_reasons": validation.get("blocked_reasons", []),
    }


def plan_default_authorization_profile(
    request: AuthorizationProfileRequest,
    *,
    kind: str = BUSINESS_READONLY_KIND,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    default = default_authorization_profile(request.root, kind=kind)
    if not default.get("ok"):
        return {
            "schema_version": 1,
            "plan_kind": "authorization-profile-plan",
            "ok": False,
            "kind": kind,
            "profile_id": default.get("profile_id", ""),
            "profile_path": default.get("profile_path", ""),
            "would_execute_live": False,
            "would_write_registry": False,
            "would_write_business": False,
            "blockers": default.get("blocked_reasons", []),
        }
    return plan_with_authorization_profile(
        AuthorizationProfileRequest(
            root=request.root,
            profile_path=str(default["profile_path"]),
            registered_worker_registry_path=request.registered_worker_registry_path,
            opencode_visible_registry_path=request.opencode_visible_registry_path,
            execution_mode=request.execution_mode,
            allow_profile_live=False,
            timeout_seconds=request.timeout_seconds,
            run_id=request.run_id,
            dispatch_bin=request.dispatch_bin,
            worker_backend_bin=request.worker_backend_bin,
            watcher_bin=request.watcher_bin,
            live_enable_config_path=request.live_enable_config_path,
        ),
        command_runner=command_runner,
    )


def short_prompt_template(root: Path | str, *, profile_kind: str = BUSINESS_READONLY_KIND) -> dict[str, Any]:
    default = default_authorization_profile(root, kind=profile_kind)
    profile_id = str(default.get("profile_id") or BUSINESS_READONLY_PROFILE_ID)
    return {
        "schema_version": 1,
        "ok": bool(default.get("ok")),
        "profile_kind": profile_kind,
        "profile_id": profile_id,
        "template": (
            f"Use the configured business readonly review profile={profile_id}. "
            "Prepare without live execution; execution requires explicit authorization. "
            "No business, registry or state-team writes, raw tmux, direct TUI, "
            "direct adapter, rmux, worker switching or retries. Stop on failure."
        ),
        "blocked_reasons": default.get("blocked_reasons", []),
    }


def authorization_profile_surface_summary(root: Path | str) -> dict[str, Any]:
    root_path = Path(root).resolve()
    if not (root_path / DEFAULTS_REL).exists():
        return {
            "schema_version": 1,
            "summary_kind": "authorization-profile-surface-summary",
            "ok": True,
            "configured": False,
            "blocked_reasons": [],
        }
    default = default_authorization_profile(root_path)
    validation = validate_authorization_profile_defaults_file(root_path / DEFAULTS_REL, root=root_path)
    return {
        "schema_version": 1,
        "summary_kind": "authorization-profile-surface-summary",
        "ok": bool(default.get("ok")),
        "configured": True,
        "default_business_readonly_review_profile": default.get("profile_id", ""),
        "default_business_readonly_review_profile_path": default.get("profile_path", ""),
        "business_readonly_review_profile_valid": bool(default.get("ok")),
        "business_readonly_review_writes_allowed": False,
        "default_business_scoped_write_profile": validation.get("default_business_scoped_write_profile", ""),
        "business_scoped_write_profile_enabled": bool(validation.get("business_scoped_write_enabled_by_default", False)),
        "profile_valid": bool(default.get("ok")),
        "live_requires_explicit_allow": bool(default.get("live_requires_explicit_allow", False)),
        "business_ready_profile_present": bool(default.get("ok")),
        "next_gate": validation.get("next_gate", ""),
        "next_business_gate": validation.get("next_business_gate", ""),
        "forbidden_by_default": validation.get("forbidden_by_default", []),
        "blocked_reasons": default.get("blocked_reasons", []),
    }


def load_authorization_profile_file(path: Path | str) -> dict[str, Any]:
    profile_path = Path(path).resolve()
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"_profile_path": str(profile_path), "_load_error": "authorization profile missing"}
    except json.JSONDecodeError as exc:
        return {"_profile_path": str(profile_path), "_load_error": f"authorization profile JSON malformed: {exc.msg}"}
    if not isinstance(payload, dict):
        return {"_profile_path": str(profile_path), "_load_error": "authorization profile must contain an object"}
    payload["_profile_path"] = str(profile_path)
    return payload


def validate_authorization_profile_file(path: Path | str) -> dict[str, Any]:
    payload = load_authorization_profile_file(path)
    return validate_authorization_profile(payload, profile_path=str(Path(path).resolve()))


def validate_authorization_profile(payload: dict[str, Any], *, profile_path: str = "") -> dict[str, Any]:
    blockers: list[str] = []
    load_error = str(payload.get("_load_error", ""))
    if load_error:
        blockers.append(load_error)
    profile_kind = str(payload.get("profile_kind", ""))
    if profile_kind == BUSINESS_READONLY_KIND:
        blockers.extend(_business_readonly_profile_blockers(payload))
    elif profile_kind == BUSINESS_SCOPED_WRITE_KIND:
        blockers.extend(_business_scoped_write_profile_blockers(payload))
    elif not load_error:
        blockers.append(f"unsupported profile_kind: {profile_kind}")
    blockers = _dedupe(blockers)
    return {
        "schema_version": 1,
        "validation_kind": "authorization-profile",
        "ok": not blockers,
        "profile_path": profile_path or str(payload.get("_profile_path", "")),
        "profile_id": str(payload.get("profile_id", "")),
        "profile_version": int(payload.get("profile_version", 0) or 0),
        "profile_kind": profile_kind,
        "description": str(payload.get("description", "")),
        "enabled": bool(payload.get("enabled", True)),
        "allowed_workers": _list(payload.get("allowed_workers")),
        "required_workers": _list(payload.get("required_workers")),
        "business_ready_required": bool(payload.get("business_ready_required", False)),
        "business_repo_writes_allowed": bool(payload.get("business_repo_writes_allowed", False)),
        "business_task_allowed": bool(payload.get("business_task_allowed", False)),
        "requires_user_write_scope_contract": bool(payload.get("requires_user_write_scope_contract", False)),
        "raw_tmux_allowed": bool(payload.get("raw_tmux_allowed", False)),
        "direct_tui_allowed": bool(payload.get("direct_tui_allowed", False)),
        "direct_adapter_allowed": bool(payload.get("direct_adapter_allowed", False)),
        "rmux_allowed": bool(payload.get("rmux_allowed", False)),
        "worker_switch_allowed": bool(payload.get("worker_switch_allowed", False)),
        "allowed_registry_updates": bool(payload.get("allowed_registry_updates", False)),
        "live_allowed": bool(payload.get("live_allowed", False)),
        "live_requires_explicit_allow": bool(payload.get("live_requires_explicit_allow", False)),
        "max_live_attempts": int(payload.get("max_live_attempts", 0) or 0),
        "blocked_reasons": blockers,
    }


def _business_readonly_profile_blockers(payload: dict[str, Any]) -> list[str]:
    blockers = _common_profile_blockers(payload, BUSINESS_READONLY_PROFILE_ID, BUSINESS_READONLY_KIND)
    if payload.get("enabled") is not True:
        blockers.append("business_readonly_review enabled must be true")
    if payload.get("business_task_allowed") is not True:
        blockers.append("business_readonly_review business_task_allowed must be true")
    if payload.get("business_repo_writes_allowed") is not False:
        blockers.append("business_readonly_review business_repo_writes_allowed must be false")
    if payload.get("live_allowed") is not True:
        blockers.append("business_readonly_review live_allowed must be true")
    if payload.get("live_requires_explicit_allow") is not True:
        blockers.append("business_readonly_review live_requires_explicit_allow must be true")
    read_roots = _list(payload.get("allowed_read_roots"))
    framework_roots = [root for root in read_roots if root.endswith("/.omx/framework/team")]
    business_roots = [root for root in read_roots if root not in framework_roots]
    if not business_roots:
        blockers.append("business_readonly_review allowed_read_roots must include a business workspace")
    if not framework_roots:
        blockers.append("business_readonly_review allowed_read_roots must include the selected framework root")
    if any(root in _list(payload.get("allowed_write_roots")) for root in business_roots):
        blockers.append("business_readonly_review allowed_write_roots must not include business repo")
    forbidden_roots = _list(payload.get("forbidden_write_roots"))
    for root in business_roots:
        if root not in forbidden_roots:
            blockers.append(f"business_readonly_review forbidden_write_roots must include {root}")
    for suffix in (
        "/.omx/state/team",
        "/.omx/framework/team/registered-workers/registered-workers.json",
        "/.omx/framework/team/opencode_visible_workers.json",
    ):
        if not any(root.endswith(suffix) for root in forbidden_roots):
            blockers.append(f"business_readonly_review forbidden_write_roots must include *{suffix}")
    for command in ("git add", "git commit", "git checkout", "git reset", "git clean", "git stash"):
        if _command_present(_list(payload.get("allowed_commands")), command):
            blockers.append(f"business_readonly_review allowed_commands must not include {command}")
    for command in ("git status --short", "git diff --name-only", "git diff --cached --name-only", "git diff --check", "git diff --cached --check"):
        if command not in _list(payload.get("allowed_commands")):
            blockers.append(f"business_readonly_review allowed_commands must include {command}")
    for output in ("report.md", "terminal marker", "validation command summary", "no-write proof", "business repo before/after status summary"):
        if output not in _list(payload.get("required_outputs")):
            blockers.append(f"business_readonly_review required_outputs must include {output}")
    blockers.extend(_business_forbidden_surface_blockers(payload, prefix="business_readonly_review"))
    return blockers


def _business_scoped_write_profile_blockers(payload: dict[str, Any]) -> list[str]:
    blockers = _common_profile_blockers(payload, BUSINESS_SCOPED_WRITE_PROFILE_ID, BUSINESS_SCOPED_WRITE_KIND)
    enabled = bool(payload.get("enabled", False))
    if enabled and not isinstance(payload.get("write_scope_contract"), dict):
        blockers.append("business_scoped_write enabled profile requires write_scope_contract")
    if not enabled and payload.get("live_allowed") is not False:
        blockers.append("business_scoped_write disabled profile live_allowed must be false")
    if payload.get("live_requires_explicit_allow") is not True:
        blockers.append("business_scoped_write live_requires_explicit_allow must be true")
    for key in (
        "requires_user_write_scope_contract",
        "requires_explicit_profile_enable",
        "requires_business_repo_baseline",
        "requires_allowed_files",
        "requires_forbidden_paths",
        "requires_validation_commands",
        "requires_diff_review",
        "requires_provenance_package",
    ):
        if payload.get(key) is not True:
            blockers.append(f"business_scoped_write {key} must be true")
    if not enabled and payload.get("business_repo_writes_allowed") is not False:
        blockers.append("business_scoped_write disabled profile business_repo_writes_allowed must be false")
    forbidden_roots = _list(payload.get("forbidden_write_roots"))
    for suffix in (
        "/.omx/state/team",
        "/.omx/framework/team/registered-workers/registered-workers.json",
        "/.omx/framework/team/opencode_visible_workers.json",
    ):
        if not any(root.endswith(suffix) for root in forbidden_roots):
            blockers.append(f"business_scoped_write forbidden_write_roots must include *{suffix}")
    required_fields = {
        "task_id",
        "business_repo",
        "allowed_files",
        "allowed_dirs",
        "forbidden_files",
        "forbidden_dirs",
        "max_files_changed",
        "allowed_commands",
        "required_validation_commands",
        "required_diff_checks",
        "provenance_required",
        "rollback_policy",
        "user_approval_id",
        "expires_at",
    }
    if set(_list(payload.get("write_scope_contract_fields"))) != required_fields:
        blockers.append("business_scoped_write write_scope_contract_fields must match required contract fields")
    blockers.extend(_business_forbidden_surface_blockers(payload, prefix="business_scoped_write"))
    return blockers


def _common_profile_blockers(payload: dict[str, Any], expected_id: str, expected_kind: str) -> list[str]:
    blockers: list[str] = []
    if payload.get("profile_id") != expected_id:
        blockers.append(f"profile_id must be {expected_id}")
    if int(payload.get("profile_version", 0) or 0) != PROFILE_VERSION:
        blockers.append("profile_version must be 1")
    if payload.get("profile_kind") != expected_kind:
        blockers.append(f"profile_kind must be {expected_kind}")
    allowed = _list(payload.get("allowed_workers"))
    required = _list(payload.get("required_workers"))
    if not allowed or len(set(allowed)) != len(allowed) or any(
        not worker.strip() or worker in {".", ".."} or any(char in worker for char in ("/", "\\", "\x00"))
        for worker in allowed
    ):
        blockers.append(f"{expected_kind} allowed_workers must name distinct safe worker IDs")
    if not required or len(set(required)) != len(required) or not set(required).issubset(allowed):
        blockers.append(f"{expected_kind} required_workers must be a nonempty subset of allowed_workers")
    backends = set(_list(payload.get("allowed_worker_backends")))
    if not backends or not backends.issubset({"opencode_tui", "claude_code"}):
        blockers.append(f"{expected_kind} allowed_worker_backends must select supported backends")
    retry = payload.get("retry_policy")
    max_retries = retry.get("max_retries", -1) if isinstance(retry, dict) else -1
    if not isinstance(retry, dict) or retry.get("kind") != "none" or int(max_retries) != 0:
        blockers.append("retry_policy.kind must be none")
    if int(payload.get("max_live_attempts", 0) or 0) != 1:
        blockers.append("max_live_attempts must be 1")
    for key in ("required_preflight_checks", "required_post_checks", "required_result_validation", "required_terminal_truth", "required_outputs", "stop_conditions", "final_report_requirements"):
        if not _list(payload.get(key)):
            blockers.append(f"{key} must be non-empty")
    return blockers


def _business_forbidden_surface_blockers(payload: dict[str, Any], *, prefix: str) -> list[str]:
    blockers: list[str] = []
    for key in ("allowed_registry_updates", "business_ready_required", "worker_switch_allowed", "raw_tmux_allowed", "direct_tui_allowed", "direct_adapter_allowed", "rmux_allowed"):
        if payload.get(key) is not False:
            blockers.append(f"{prefix} {key} must be false")
    retry = payload.get("retry_policy")
    max_retries = retry.get("max_retries", -1) if isinstance(retry, dict) else -1
    if not isinstance(retry, dict) or retry.get("kind") != "none" or int(max_retries) != 0:
        blockers.append(f"{prefix} retry_policy.kind must be none")
    return blockers


def validate_business_write_scope_file(
    profile_path: Path | str,
    contract_path: Path | str,
    *,
    changed_files: list[str] | None = None,
) -> dict[str, Any]:
    profile = load_authorization_profile_file(profile_path)
    contract = _read_json(Path(contract_path).resolve())
    return validate_business_write_scope(
        profile,
        contract,
        profile_path=str(Path(profile_path).resolve()),
        contract_path=str(Path(contract_path).resolve()),
        changed_files=changed_files,
    )


def validate_business_baseline_delta_file(
    baseline_status_path: Path | str,
    current_status_path: Path | str,
    *,
    expected_added_files: list[str],
    expected_baseline_sha256: str = "",
) -> dict[str, Any]:
    baseline_path = Path(baseline_status_path).expanduser().resolve()
    current_path = Path(current_status_path).expanduser().resolve()
    baseline_text = baseline_path.read_text(encoding="utf-8")
    current_text = current_path.read_text(encoding="utf-8")
    return validate_business_baseline_delta(
        baseline_text.splitlines(),
        current_text.splitlines(),
        expected_added_files=expected_added_files,
        expected_baseline_sha256=expected_baseline_sha256,
        baseline_status_path=str(baseline_path),
        current_status_path=str(current_path),
        baseline_status_sha256=hashlib.sha256(baseline_text.encode("utf-8")).hexdigest(),
        current_status_sha256=hashlib.sha256(current_text.encode("utf-8")).hexdigest(),
    )


def validate_business_baseline_delta(
    baseline_status_lines: list[str],
    current_status_lines: list[str],
    *,
    expected_added_files: list[str],
    expected_baseline_sha256: str = "",
    baseline_status_path: str = "",
    current_status_path: str = "",
    baseline_status_sha256: str = "",
    current_status_sha256: str = "",
) -> dict[str, Any]:
    """Validate that a dirty git-status baseline changed by one allowed file."""

    baseline = _parse_git_status_short_lines(baseline_status_lines)
    current = _parse_git_status_short_lines(current_status_lines)
    expected = _dedupe([_normalize_business_path(item) for item in expected_added_files if str(item).strip()])
    blockers: list[str] = []
    blockers.extend(baseline["blocked_reasons"])
    blockers.extend(current["blocked_reasons"])
    if not expected:
        blockers.append("expected_added_files is required")
    if expected_baseline_sha256 and baseline_status_sha256 and expected_baseline_sha256 != baseline_status_sha256:
        blockers.append("baseline status sha256 does not match expected baseline")

    baseline_entries = baseline["entries"]
    current_entries = current["entries"]
    expected_set = set(expected)
    added_entries: list[str] = []
    unexpected_entries: list[str] = []
    removed_entries: list[str] = []
    changed_entries: list[dict[str, str]] = []
    missing_expected: list[str] = []

    for path in expected:
        if path in baseline_entries:
            blockers.append(f"expected added file already present in baseline: {path}")

    for path, before_status in baseline_entries.items():
        if path not in current_entries:
            removed_entries.append(f"{before_status} {path}")
            blockers.append(f"baseline entry removed: {path}")
            continue
        after_status = current_entries[path]
        if after_status != before_status:
            changed_entries.append({"path": path, "before": before_status, "after": after_status})
            blockers.append(f"baseline entry changed: {path}")

    for path, status in current_entries.items():
        if path in baseline_entries:
            continue
        line = f"{status} {path}"
        if path in expected_set and status == "??":
            added_entries.append(line)
        else:
            unexpected_entries.append(line)
            blockers.append(f"unexpected status entry: {line}")

    added_paths = {_status_path(entry) for entry in added_entries}
    for path in expected:
        if path not in added_paths:
            missing_expected.append(path)
            blockers.append(f"expected added file missing: {path}")

    blockers = _dedupe(blockers)
    return {
        "schema_version": 1,
        "validation_kind": "business-baseline-delta",
        "ok": not blockers,
        "baseline_status_path": baseline_status_path,
        "current_status_path": current_status_path,
        "baseline_status_sha256": baseline_status_sha256,
        "current_status_sha256": current_status_sha256,
        "expected_baseline_sha256": expected_baseline_sha256,
        "expected_added_files": expected,
        "baseline_entry_count": len(baseline_entries),
        "current_entry_count": len(current_entries),
        "added_entries": added_entries,
        "unexpected_entries": unexpected_entries,
        "removed_entries": removed_entries,
        "changed_entries": changed_entries,
        "missing_expected_files": missing_expected,
        "blocked_reasons": blockers,
    }


def _parse_git_status_short_lines(lines: list[str]) -> dict[str, Any]:
    entries: dict[str, str] = {}
    blockers: list[str] = []
    for raw in lines:
        line = str(raw).rstrip("\n")
        if not line:
            continue
        if len(line) < 4 or line[2] != " ":
            blockers.append(f"malformed git status --short line: {line}")
            continue
        status = line[:2]
        path = _normalize_business_path(line[3:])
        if not path:
            blockers.append(f"malformed git status --short line: {line}")
            continue
        if path in entries:
            blockers.append(f"duplicate git status path: {path}")
            continue
        entries[path] = status
    return {"entries": entries, "blocked_reasons": blockers}


def _status_path(entry: str) -> str:
    return _normalize_business_path(entry[3:]) if len(entry) >= 4 else ""


def validate_business_write_scope(
    profile: dict[str, Any],
    contract: dict[str, Any],
    *,
    profile_path: str = "",
    contract_path: str = "",
    changed_files: list[str] | None = None,
) -> dict[str, Any]:
    validation = validate_authorization_profile(profile, profile_path=profile_path)
    blockers = list(validation.get("blocked_reasons", []))
    if profile.get("profile_kind") != BUSINESS_SCOPED_WRITE_KIND:
        blockers.append("write-scope contract requires business_scoped_write profile")
    required = {
        "task_id",
        "business_repo",
        "allowed_files",
        "allowed_dirs",
        "forbidden_files",
        "forbidden_dirs",
        "max_files_changed",
        "allowed_commands",
        "required_validation_commands",
        "required_diff_checks",
        "provenance_required",
        "rollback_policy",
        "user_approval_id",
        "expires_at",
    }
    missing = sorted(key for key in required if key not in contract)
    blockers.extend(f"{key} is required" for key in missing)
    business_repo = str(contract.get("business_repo") or "").strip()
    if not business_repo or not Path(business_repo).is_absolute():
        blockers.append("business_repo must be an absolute workspace path")
    allowed_files = _list(contract.get("allowed_files"))
    allowed_dirs = _list(contract.get("allowed_dirs"))
    forbidden_files = _list(contract.get("forbidden_files"))
    forbidden_dirs = _list(contract.get("forbidden_dirs"))
    if not allowed_files and not allowed_dirs:
        blockers.append("allowed_files or allowed_dirs is required")
    normalized_allowed_files, allowed_file_blockers = _normalize_business_paths(
        allowed_files, business_repo, "allowed_files"
    )
    normalized_allowed_dirs, allowed_dir_blockers = _normalize_business_paths(
        allowed_dirs, business_repo, "allowed_dirs"
    )
    normalized_forbidden_files, forbidden_file_blockers = _normalize_business_paths(
        forbidden_files, business_repo, "forbidden_files"
    )
    normalized_forbidden_dirs, forbidden_dir_blockers = _normalize_business_paths(
        forbidden_dirs, business_repo, "forbidden_dirs"
    )
    blockers.extend(allowed_file_blockers)
    blockers.extend(allowed_dir_blockers)
    blockers.extend(forbidden_file_blockers)
    blockers.extend(forbidden_dir_blockers)
    if set(normalized_allowed_files) & set(normalized_forbidden_files):
        blockers.append("allowed_files overlaps forbidden_files")
    if set(normalized_allowed_dirs) & set(normalized_forbidden_dirs):
        blockers.append("allowed_dirs overlaps forbidden_dirs")
    for value in normalized_allowed_files + normalized_allowed_dirs:
        if _is_registry_path(value):
            blockers.append("contract must not allow registry path")
        if _is_state_team_path(value):
            blockers.append("contract must not allow .omx/state/team path")
    if not _list(contract.get("required_validation_commands")):
        blockers.append("required_validation_commands is required")
    if not _list(contract.get("required_diff_checks")):
        blockers.append("required_diff_checks is required")
    if not str(contract.get("user_approval_id", "")).strip():
        blockers.append("user_approval_id is required")
    if contract.get("provenance_required") is not True:
        blockers.append("provenance_required must be true")
    if int(contract.get("max_files_changed", 0) or 0) <= 0:
        blockers.append("max_files_changed must be positive")
    expires_at = str(contract.get("expires_at", ""))
    if not _future_timestamp(expires_at):
        blockers.append("expires_at must be a future ISO timestamp")
    write_scope_result = _validate_changed_files_against_write_scope(contract, changed_files or [])
    blockers.extend(write_scope_result["blocked_reasons"])
    blockers = _dedupe(blockers)
    return {
        "schema_version": 1,
        "validation_kind": "business-write-scope-contract",
        "ok": not blockers,
        "profile_path": profile_path,
        "contract_path": contract_path,
        "profile_id": str(profile.get("profile_id", "")),
        "profile_enabled": bool(profile.get("enabled", False)),
        "would_execute_live": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "contract": contract,
        "write_scope_result": write_scope_result,
        "blocked_reasons": blockers,
    }


def prepare_business_scoped_write_smoke(
    profile_path: Path | str,
    contract_path: Path | str,
    *,
    writer: str,
    reviewer: str,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    profile_path_resolved = str(Path(profile_path).resolve())
    contract_path_resolved = str(Path(contract_path).resolve())
    validation = validate_business_write_scope_file(profile_path_resolved, contract_path_resolved)
    contract = validation.get("contract", {}) if isinstance(validation.get("contract"), dict) else {}
    writer_id = str(writer or "").strip()
    reviewer_id = str(reviewer or "").strip()
    blockers = list(validation.get("blocked_reasons", []))
    if not writer_id:
        blockers.append("writer is required")
    if not reviewer_id:
        blockers.append("reviewer is required")
    if writer_id and reviewer_id and writer_id == reviewer_id:
        blockers.append("writer and reviewer must be different workers")
    profile = load_authorization_profile_file(profile_path_resolved)
    allowed_workers = _list(profile.get("allowed_workers"))
    if allowed_workers:
        if writer_id and writer_id not in allowed_workers:
            blockers.append(f"writer must be listed in profile allowed_workers: {writer_id}")
        if reviewer_id and reviewer_id not in allowed_workers:
            blockers.append(f"reviewer must be listed in profile allowed_workers: {reviewer_id}")
    business_repo = str(contract.get("business_repo") or "")
    allowed_files, _ = _normalize_business_paths(
        _list(contract.get("allowed_files")), business_repo, "allowed_files"
    )
    allowed_dirs, _ = _normalize_business_paths(
        _list(contract.get("allowed_dirs")), business_repo, "allowed_dirs"
    )
    forbidden_files, _ = _normalize_business_paths(
        _list(contract.get("forbidden_files")), business_repo, "forbidden_files"
    )
    forbidden_dirs, _ = _normalize_business_paths(
        _list(contract.get("forbidden_dirs")), business_repo, "forbidden_dirs"
    )
    gate_command = _business_scope_gate_command(profile_path_resolved, contract_path_resolved)
    delta_command = _business_baseline_delta_gate_command(allowed_files)
    baseline_delta_evidence = _reviewer_baseline_delta_evidence(allowed_files, delta_command)

    package = {
        "schema_version": 1,
        "package_kind": "business-scoped-write-smoke-prep",
        "created_at": _utc_now(),
        "ok": not _dedupe(blockers),
        "profile_id": str(profile.get("profile_id", "")),
        "profile_path": profile_path_resolved,
        "contract_path": contract_path_resolved,
        "package_path": "",
        "contract_validation": validation,
        "profile_allowed_workers": allowed_workers,
        "task_id": str(contract.get("task_id", "")),
        "business_repo": str(contract.get("business_repo", "")),
        "would_execute_live": False,
        "would_write_business": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "would_use_raw_tmux": False,
        "would_use_direct_tui": False,
        "would_use_direct_adapter": False,
        "would_use_rmux": False,
        "profile_enabled": bool(profile.get("enabled", False)),
        "business_repo_writes_allowed": bool(profile.get("business_repo_writes_allowed", False)),
        "writer": {
            "worker_id": writer_id,
            "role": "scoped_writer_candidate",
            "allowed_files": allowed_files,
            "allowed_dirs": allowed_dirs,
            "forbidden_files": forbidden_files,
            "forbidden_dirs": forbidden_dirs,
            "max_files_changed": int(contract.get("max_files_changed", 0) or 0),
            "prompt": _writer_smoke_prompt(contract, allowed_files, allowed_dirs, forbidden_files, forbidden_dirs),
        },
        "reviewer": {
            "worker_id": reviewer_id,
            "role": "diff_reviewer_candidate",
            "baseline_delta_evidence": baseline_delta_evidence,
            "prompt": _reviewer_smoke_prompt(contract, baseline_delta_evidence),
        },
        "post_write_gate_commands": [
            gate_command + " --changed-file <path-from-git-diff-name-only> --json",
            delta_command,
        ],
        "required_preflight": [
            "validate-business-write-scope --json",
            "capture git status --short before write",
            "capture baseline status sha256 before write",
            "confirm explicit operator approval id still applies",
        ],
        "required_post_checks": [
            "git diff --name-only",
            "validate-business-write-scope --changed-file for every changed file",
            "validate-business-baseline-delta against the pre-write status snapshot",
            "reviewer reads diff and reports accept/block",
            "leader verifies report.md plus exactly one terminal marker",
        ],
        "safe_next_actions": [
            "Use this only as a no-live preparation package.",
            "For a future live smoke, use 1 writer + 1 reviewer with max_files_changed unchanged.",
            "Do not enable live writes until the operator explicitly approves the scoped-write profile and task contract.",
        ],
        "forbidden_next_actions": [
            "do not run live writer from this package",
            "do not modify registry files",
            "do not write .omx/state/team",
            "do not use raw tmux input",
            "do not use direct TUI or direct adapter submit",
        ],
        "blocked_reasons": _dedupe(blockers),
    }
    if output_dir is not None and str(output_dir).strip():
        out = Path(output_dir).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        package_path = out / f"{_safe_filename(str(package.get('task_id') or 'scoped-write-smoke'))}-prep.json"
        package["package_path"] = str(package_path)
        package_path.write_text(json.dumps(package, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return package


def validate_business_scoped_write_smoke_prep_file(package_path: Path | str) -> dict[str, Any]:
    path = Path(package_path).expanduser().resolve()
    payload = _read_json(path)
    return validate_business_scoped_write_smoke_prep(payload, package_path=str(path))


def validate_business_scoped_write_smoke_prep(payload: dict[str, Any], *, package_path: str = "") -> dict[str, Any]:
    blockers: list[str] = []
    if payload.get("package_kind") != "business-scoped-write-smoke-prep":
        blockers.append("package_kind must be business-scoped-write-smoke-prep")
    if int(payload.get("schema_version", 0) or 0) != 1:
        blockers.append("schema_version must be 1")
    created_at_blocker = prep_created_at_blocker(str(payload.get("created_at", "")), label="prep package")
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
        if payload.get(key) is not False:
            blockers.append(f"{key} must be false")
    writer = _mapping(payload.get("writer"))
    reviewer = _mapping(payload.get("reviewer"))
    writer_id = str(writer.get("worker_id", "")).strip()
    reviewer_id = str(reviewer.get("worker_id", "")).strip()
    if not writer_id:
        blockers.append("writer.worker_id is required")
    if not reviewer_id:
        blockers.append("reviewer.worker_id is required")
    if writer_id and reviewer_id and writer_id == reviewer_id:
        blockers.append("writer and reviewer must be different workers")
    business_repo = str(payload.get("business_repo") or "")
    expected_writer_files, writer_path_blockers = _normalize_business_paths(
        _list(writer.get("allowed_files")), business_repo, "writer.allowed_files"
    )
    blockers.extend(writer_path_blockers)
    contract_validation = _mapping(payload.get("contract_validation"))
    if contract_validation.get("ok") is not True:
        blockers.append("contract_validation.ok must be true")
    gate_commands = _list(payload.get("post_write_gate_commands"))
    if not any("--changed-file" in str(command) for command in gate_commands):
        blockers.append("post_write_gate_commands must include --changed-file")
    if not any("validate-business-baseline-delta" in str(command) for command in gate_commands):
        blockers.append("post_write_gate_commands must include validate-business-baseline-delta")
    baseline_evidence = _mapping(reviewer.get("baseline_delta_evidence"))
    if not baseline_evidence:
        blockers.append("reviewer.baseline_delta_evidence is required")
    else:
        blockers.extend(
            baseline_delta_evidence_blockers(
                baseline_evidence,
                expected_writer_files,
                prefix="reviewer.baseline_delta_evidence",
                expected_label="writer.allowed_files",
                business_repo=business_repo,
            )
        )
    if payload.get("business_repo_writes_allowed") is True and payload.get("profile_enabled") is not True:
        blockers.append("business_repo_writes_allowed requires enabled profile in prep")
    blockers.extend(_list(payload.get("blocked_reasons")))
    blockers = _dedupe(blockers)
    return {
        "schema_version": 1,
        "validation_kind": "business-scoped-write-smoke-prep",
        "ok": not blockers,
        "package_path": package_path,
        "package_kind": str(payload.get("package_kind", "")),
        "would_execute_live": bool(payload.get("would_execute_live", True)),
        "would_write_business": bool(payload.get("would_write_business", True)),
        "writer_id": writer_id,
        "reviewer_id": reviewer_id,
        "blocked_reasons": blockers,
    }


def prepare_business_scoped_rollback_smoke(
    profile_path: Path | str,
    *,
    target_file: str,
    expected_status: str,
    expected_sha256: str,
    rollback_worker: str,
    reviewer: str,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    profile_path_resolved = str(Path(profile_path).resolve())
    profile = load_authorization_profile_file(profile_path_resolved)
    validation = validate_authorization_profile(profile, profile_path=profile_path_resolved)
    blockers = list(validation.get("blocked_reasons", []))
    if profile.get("profile_kind") != BUSINESS_SCOPED_WRITE_KIND:
        blockers.append("rollback prep requires business_scoped_write profile")
    rollback_worker_id = str(rollback_worker or "").strip()
    reviewer_id = str(reviewer or "").strip()
    if not rollback_worker_id:
        blockers.append("rollback_worker is required")
    if not reviewer_id:
        blockers.append("reviewer is required")
    if rollback_worker_id and reviewer_id and rollback_worker_id == reviewer_id:
        blockers.append("rollback_worker and reviewer must be different workers")
    allowed_workers = _list(profile.get("allowed_workers"))
    if allowed_workers:
        if rollback_worker_id and rollback_worker_id not in allowed_workers:
            blockers.append(f"rollback_worker must be listed in profile allowed_workers: {rollback_worker_id}")
        if reviewer_id and reviewer_id not in allowed_workers:
            blockers.append(f"reviewer must be listed in profile allowed_workers: {reviewer_id}")

    target = _normalize_business_path(target_file)
    status_line = str(expected_status or "").strip()
    sha = str(expected_sha256 or "").strip().lower()
    blockers.extend(_rollback_target_blockers(target, status_line, sha))

    package = {
        "schema_version": 1,
        "package_kind": "business-scoped-rollback-smoke-prep",
        "created_at": _utc_now(),
        "ok": not _dedupe(blockers),
        "profile_id": str(profile.get("profile_id", "")),
        "profile_path": profile_path_resolved,
        "package_path": "",
        "profile_validation": validation,
        "profile_allowed_workers": allowed_workers,
        "business_repo": BUSINESS_REPO_ROOT,
        "would_execute_live": False,
        "would_write_business": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "would_use_raw_tmux": False,
        "would_use_direct_tui": False,
        "would_use_direct_adapter": False,
        "would_use_rmux": False,
        "live_gate_ready": False,
        "rollback_requires_separate_approval": True,
        "rollback": {
            "worker_id": rollback_worker_id,
            "role": "scoped_rollback_candidate",
            "operation": "remove_untracked_file",
            "target_file": target,
            "expected_status": status_line,
            "expected_sha256": sha,
            "prompt": _rollback_smoke_prompt(target, status_line, sha),
        },
        "reviewer": {
            "worker_id": reviewer_id,
            "role": "rollback_reviewer_candidate",
            "prompt": _rollback_reviewer_prompt(target, status_line, sha),
        },
        "required_preflight": [
            f"git status --short -- {target}",
            f"sha256sum {target}",
            "confirm explicit rollback approval id still applies",
            "confirm git index is unchanged before rollback",
        ],
        "required_post_checks": [
            f"test ! -e {target}",
            f"git status --short -- {target} is empty",
            "git diff --cached --name-only is empty",
            "reviewer confirms only the target untracked file disappeared",
            "leader verifies report.md plus exactly one terminal marker",
        ],
        "safe_next_actions": [
            "Use this only as a no-live rollback preparation package.",
            "Ask for separate user approval before any live rollback deletion.",
            "If approved later, use one rollback worker plus one read-only reviewer.",
        ],
        "forbidden_next_actions": [
            "do not run rollback from this package",
            "do not delete files without separate approval",
            "do not touch git index",
            "do not modify registry files",
            "do not write .omx/state/team",
            "do not use raw tmux input",
            "do not use direct TUI or direct adapter submit",
        ],
        "blocked_reasons": _dedupe(blockers),
    }
    if output_dir is not None and str(output_dir).strip():
        out = Path(output_dir).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        package_path = out / f"{_safe_filename(target or 'rollback')}-rollback-prep.json"
        package["package_path"] = str(package_path)
        package_path.write_text(json.dumps(package, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return package


def validate_business_scoped_rollback_prep_file(package_path: Path | str) -> dict[str, Any]:
    path = Path(package_path).expanduser().resolve()
    payload = _read_json(path)
    return validate_business_scoped_rollback_prep(payload, package_path=str(path))


def validate_business_scoped_rollback_prep(payload: dict[str, Any], *, package_path: str = "") -> dict[str, Any]:
    blockers: list[str] = []
    if payload.get("package_kind") != "business-scoped-rollback-smoke-prep":
        blockers.append("package_kind must be business-scoped-rollback-smoke-prep")
    if int(payload.get("schema_version", 0) or 0) != 1:
        blockers.append("schema_version must be 1")
    created_at_blocker = prep_created_at_blocker(str(payload.get("created_at", "")), label="rollback prep package")
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
        "live_gate_ready",
    ):
        if payload.get(key) is not False:
            blockers.append(f"{key} must be false")
    if payload.get("rollback_requires_separate_approval") is not True:
        blockers.append("rollback_requires_separate_approval must be true")
    profile_validation = _mapping(payload.get("profile_validation"))
    if profile_validation.get("ok") is not True:
        blockers.append("profile_validation.ok must be true")

    rollback = _mapping(payload.get("rollback"))
    reviewer = _mapping(payload.get("reviewer"))
    rollback_worker_id = str(rollback.get("worker_id", "")).strip()
    reviewer_id = str(reviewer.get("worker_id", "")).strip()
    if not rollback_worker_id:
        blockers.append("rollback.worker_id is required")
    if not reviewer_id:
        blockers.append("reviewer.worker_id is required")
    if rollback_worker_id and reviewer_id and rollback_worker_id == reviewer_id:
        blockers.append("rollback.worker_id and reviewer.worker_id must be different")
    allowed_workers = _list(payload.get("profile_allowed_workers"))
    if allowed_workers:
        if rollback_worker_id and rollback_worker_id not in allowed_workers:
            blockers.append(f"rollback.worker_id must be listed in profile_allowed_workers: {rollback_worker_id}")
        if reviewer_id and reviewer_id not in allowed_workers:
            blockers.append(f"reviewer.worker_id must be listed in profile_allowed_workers: {reviewer_id}")
    if rollback.get("operation") != "remove_untracked_file":
        blockers.append("rollback.operation must be remove_untracked_file")
    target = _normalize_business_path(str(rollback.get("target_file", "")))
    status_line = str(rollback.get("expected_status", "")).strip()
    sha = str(rollback.get("expected_sha256", "")).strip().lower()
    blockers.extend(_rollback_target_blockers(target, status_line, sha))
    blockers.extend(_list(payload.get("blocked_reasons")))
    blockers = _dedupe(blockers)
    return {
        "schema_version": 1,
        "validation_kind": "business-scoped-rollback-smoke-prep",
        "ok": not blockers,
        "package_path": package_path,
        "package_kind": str(payload.get("package_kind", "")),
        "would_execute_live": bool(payload.get("would_execute_live", True)),
        "would_write_business": bool(payload.get("would_write_business", True)),
        "live_gate_ready": bool(payload.get("live_gate_ready", True)),
        "target_file": target,
        "rollback_worker_id": rollback_worker_id,
        "reviewer_id": reviewer_id,
        "blocked_reasons": blockers,
    }


def validate_business_scoped_rollback_approval_file(approval_path: Path | str, prep_package_path: Path | str) -> dict[str, Any]:
    approval = _read_json(Path(approval_path).expanduser().resolve())
    prep = _read_json(Path(prep_package_path).expanduser().resolve())
    return validate_business_scoped_rollback_approval(
        approval,
        prep,
        approval_path=str(Path(approval_path).expanduser().resolve()),
        prep_package_path=str(Path(prep_package_path).expanduser().resolve()),
    )


def validate_business_scoped_rollback_approval(
    approval: dict[str, Any],
    prep: dict[str, Any],
    *,
    approval_path: str = "",
    prep_package_path: str = "",
) -> dict[str, Any]:
    blockers: list[str] = []
    live_blockers: list[str] = []
    prep_validation = validate_business_scoped_rollback_prep(prep, package_path=prep_package_path)
    if not prep_validation.get("ok"):
        blockers.append("rollback prep package validation must pass")
        blockers.extend(_list(prep_validation.get("blocked_reasons")))
    if int(approval.get("schema_version", 0) or 0) != 1:
        blockers.append("approval schema_version must be 1")
    if approval.get("approval_kind") != "business-scoped-rollback-approval":
        blockers.append("approval_kind must be business-scoped-rollback-approval")
    approval_id = str(approval.get("approval_id", "")).strip()
    approved_by = str(approval.get("approved_by", "")).strip()
    if not approval_id:
        blockers.append("approval_id is required")
    if not approved_by:
        blockers.append("approved_by is required")
    if approval.get("live_write_approved") is not True:
        blockers.append("live_write_approved must be true")
    if not _future_timestamp(str(approval.get("expires_at", ""))):
        blockers.append("expires_at must be a future ISO timestamp")

    profile_id = str(approval.get("profile_id", "")).strip()
    prep_profile_id = str(prep.get("profile_id", "")).strip()
    if profile_id != prep_profile_id:
        blockers.append("approval profile_id must match rollback prep profile_id")
    rollback = _mapping(prep.get("rollback"))
    reviewer = _mapping(prep.get("reviewer"))
    rollback_worker_id = str(approval.get("rollback_worker", "")).strip()
    reviewer_id = str(approval.get("reviewer", "")).strip()
    if rollback_worker_id != str(rollback.get("worker_id", "")).strip():
        blockers.append("approval rollback_worker must match rollback prep worker")
    if reviewer_id != str(reviewer.get("worker_id", "")).strip():
        blockers.append("approval reviewer must match rollback prep reviewer")
    if rollback_worker_id and reviewer_id and rollback_worker_id == reviewer_id:
        blockers.append("approval rollback_worker and reviewer must be different")

    target = _normalize_business_path(str(approval.get("target_file", "")))
    prep_target = _normalize_business_path(str(rollback.get("target_file", "")))
    if target != prep_target:
        blockers.append("approval target_file must match rollback prep target_file")
    expected_sha = str(approval.get("expected_sha256", "")).strip().lower()
    prep_sha = str(rollback.get("expected_sha256", "")).strip().lower()
    if expected_sha != prep_sha:
        blockers.append("approval expected_sha256 must match rollback prep expected_sha256")
    approval_allowed = {_normalize_business_path(item) for item in _list(approval.get("allowed_files"))}
    if approval_allowed != {prep_target}:
        blockers.append("approval allowed_files must match rollback prep target_file")

    for key in (
        "allow_registry_writes",
        "allow_state_team_writes",
        "allow_git_index_changes",
        "raw_tmux_allowed",
        "direct_tui_allowed",
        "direct_adapter_allowed",
        "rmux_allowed",
    ):
        if approval.get(key) is True:
            blockers.append(f"{key} must not be true")

    profile_validation = _mapping(prep.get("profile_validation"))
    if profile_validation.get("enabled") is not True:
        live_blockers.append("rollback prep profile is disabled")
    if profile_validation.get("business_repo_writes_allowed") is not True:
        live_blockers.append("rollback prep business repo writes are not enabled")

    blockers = _dedupe(blockers)
    live_blockers = _dedupe(live_blockers)
    return {
        "schema_version": 1,
        "validation_kind": "business-scoped-rollback-approval",
        "ok": not blockers,
        "approval_path": approval_path,
        "prep_package_path": prep_package_path,
        "approval_id": approval_id,
        "approved_by": approved_by,
        "profile_id": profile_id,
        "rollback_worker_id": rollback_worker_id,
        "reviewer_id": reviewer_id,
        "target_file": target,
        "expected_sha256": expected_sha,
        "would_execute_live": False,
        "would_write_business": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "live_write_approved": approval.get("live_write_approved") is True,
        "live_gate_ready": not blockers and not live_blockers,
        "live_blockers": live_blockers,
        "blocked_reasons": blockers,
    }


def validate_business_scoped_write_approval_file(approval_path: Path | str, prep_package_path: Path | str) -> dict[str, Any]:
    approval = _read_json(Path(approval_path).expanduser().resolve())
    prep = _read_json(Path(prep_package_path).expanduser().resolve())
    return validate_business_scoped_write_approval(
        approval,
        prep,
        approval_path=str(Path(approval_path).expanduser().resolve()),
        prep_package_path=str(Path(prep_package_path).expanduser().resolve()),
    )


def validate_business_scoped_write_approval(
    approval: dict[str, Any],
    prep: dict[str, Any],
    *,
    approval_path: str = "",
    prep_package_path: str = "",
) -> dict[str, Any]:
    blockers: list[str] = []
    live_blockers: list[str] = []
    prep_validation = validate_business_scoped_write_smoke_prep(prep, package_path=prep_package_path)
    if not prep_validation.get("ok"):
        blockers.append("prep package validation must pass")
        blockers.extend(_list(prep_validation.get("blocked_reasons")))
    if int(approval.get("schema_version", 0) or 0) != 1:
        blockers.append("approval schema_version must be 1")
    if approval.get("approval_kind") != "business-scoped-write-approval":
        blockers.append("approval_kind must be business-scoped-write-approval")
    approval_id = str(approval.get("approval_id", "")).strip()
    approved_by = str(approval.get("approved_by", "")).strip()
    if not approval_id:
        blockers.append("approval_id is required")
    if not approved_by:
        blockers.append("approved_by is required")
    if approval.get("live_write_approved") is not True:
        blockers.append("live_write_approved must be true")
    if not _future_timestamp(str(approval.get("expires_at", ""))):
        blockers.append("expires_at must be a future ISO timestamp")

    profile_id = str(approval.get("profile_id", "")).strip()
    prep_profile_id = str(prep.get("profile_id", "")).strip()
    if profile_id != prep_profile_id:
        blockers.append("approval profile_id must match prep package profile_id")
    writer_id = str(approval.get("writer", "")).strip()
    reviewer_id = str(approval.get("reviewer", "")).strip()
    prep_writer = _mapping(prep.get("writer"))
    prep_reviewer = _mapping(prep.get("reviewer"))
    if writer_id != str(prep_writer.get("worker_id", "")).strip():
        blockers.append("approval writer must match prep package writer")
    if reviewer_id != str(prep_reviewer.get("worker_id", "")).strip():
        blockers.append("approval reviewer must match prep package reviewer")
    if writer_id and reviewer_id and writer_id == reviewer_id:
        blockers.append("approval writer and reviewer must be different")
    if not _same_path(str(approval.get("contract_path", "")), str(prep.get("contract_path", ""))):
        blockers.append("approval contract_path must match prep package contract_path")

    business_repo = str(prep.get("business_repo") or "")
    approval_allowed_list, approval_path_blockers = _normalize_business_paths(
        _list(approval.get("allowed_files")), business_repo, "approval.allowed_files"
    )
    prep_allowed_list, prep_path_blockers = _normalize_business_paths(
        _list(prep_writer.get("allowed_files")), business_repo, "prep.writer.allowed_files"
    )
    blockers.extend(approval_path_blockers)
    blockers.extend(prep_path_blockers)
    approval_allowed = set(approval_allowed_list)
    prep_allowed = set(prep_allowed_list)
    if approval_allowed != prep_allowed:
        blockers.append("approval allowed_files must match prep package writer allowed_files")

    for key in (
        "allow_registry_writes",
        "allow_state_team_writes",
        "allow_git_index_changes",
        "raw_tmux_allowed",
        "direct_tui_allowed",
        "direct_adapter_allowed",
        "rmux_allowed",
    ):
        if approval.get(key) is True:
            blockers.append(f"{key} must not be true")

    if prep.get("profile_enabled") is not True:
        live_blockers.append("prep package profile is disabled")
    if prep.get("business_repo_writes_allowed") is not True:
        live_blockers.append("prep package business repo writes are not enabled")

    blockers = _dedupe(blockers)
    live_blockers = _dedupe(live_blockers)
    return {
        "schema_version": 1,
        "validation_kind": "business-scoped-write-approval",
        "ok": not blockers,
        "approval_path": approval_path,
        "prep_package_path": prep_package_path,
        "approval_id": approval_id,
        "approved_by": approved_by,
        "profile_id": profile_id,
        "writer_id": writer_id,
        "reviewer_id": reviewer_id,
        "would_execute_live": False,
        "would_write_business": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "live_write_approved": approval.get("live_write_approved") is True,
        "live_gate_ready": not blockers and not live_blockers,
        "live_blockers": live_blockers,
        "blocked_reasons": blockers,
    }


def rehearse_business_scoped_write(
    profile_path: Path | str,
    contract_path: Path | str,
    prep_package_path: Path | str,
    approval_path: Path | str,
    *,
    changed_files: list[str] | None = None,
    output_path: Path | str | None = None,
) -> dict[str, Any]:
    """Aggregate the no-live scoped-write gates into one operator artifact."""

    profile_path_resolved = str(Path(profile_path).expanduser().resolve())
    contract_path_resolved = str(Path(contract_path).expanduser().resolve())
    prep_path_resolved = str(Path(prep_package_path).expanduser().resolve())
    approval_path_resolved = str(Path(approval_path).expanduser().resolve())
    changed_file_inputs = [str(item).strip() for item in (changed_files or []) if str(item).strip()]

    write_scope = validate_business_write_scope_file(
        profile_path_resolved,
        contract_path_resolved,
        changed_files=changed_file_inputs,
    )
    prep_validation = validate_business_scoped_write_smoke_prep_file(prep_path_resolved)
    approval_validation = validate_business_scoped_write_approval_file(approval_path_resolved, prep_path_resolved)

    blockers: list[str] = []
    if not changed_file_inputs:
        blockers.append("at least one changed file is required for scoped-write rehearsal")
    if not write_scope.get("ok"):
        blockers.extend(_list(write_scope.get("blocked_reasons")))
    if not prep_validation.get("ok"):
        blockers.append("prep package validation must pass")
        blockers.extend(_list(prep_validation.get("blocked_reasons")))
    if not approval_validation.get("ok"):
        blockers.append("approval validation must pass")
        blockers.extend(_list(approval_validation.get("blocked_reasons")))
    blockers = _dedupe(blockers)

    live_blockers = _list(approval_validation.get("live_blockers"))
    live_gate_ready = bool(approval_validation.get("live_gate_ready")) and not blockers
    if blockers:
        selected_action = "block"
        reason = "Scoped-write rehearsal found blocking gate failures."
    elif not live_gate_ready:
        selected_action = "human_review_required"
        reason = "Scoped-write gates match, but live write remains disabled or blocked."
    else:
        selected_action = "accept"
        reason = "Scoped-write rehearsal gates match and the approval live gate is ready."

    write_scope_result = (
        write_scope.get("write_scope_result", {})
        if isinstance(write_scope.get("write_scope_result"), dict)
        else {}
    )
    prep_payload = _read_json(Path(prep_path_resolved))
    contract = write_scope.get("contract", {}) if isinstance(write_scope.get("contract"), dict) else {}
    payload = {
        "schema_version": 1,
        "rehearsal_kind": "business-scoped-write-rehearsal",
        "created_at": _utc_now(),
        "ok": not blockers,
        "rehearsal_only": True,
        "would_execute_live": False,
        "would_write_business": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "profile_path": profile_path_resolved,
        "contract_path": contract_path_resolved,
        "prep_package_path": prep_path_resolved,
        "approval_path": approval_path_resolved,
        "profile_id": str(approval_validation.get("profile_id") or write_scope.get("profile_id") or ""),
        "task_id": str(contract.get("task_id") or prep_payload.get("task_id") or ""),
        "approval_id": str(approval_validation.get("approval_id") or ""),
        "writer_id": str(approval_validation.get("writer_id") or prep_validation.get("writer_id") or ""),
        "reviewer_id": str(approval_validation.get("reviewer_id") or prep_validation.get("reviewer_id") or ""),
        "changed_files": _list(write_scope_result.get("changed_files")),
        "write_scope_result": write_scope_result,
        "gate_results": {
            "write_scope": _gate_projection(write_scope),
            "prep_package": _gate_projection(prep_validation),
            "approval": _gate_projection(approval_validation),
        },
        "live_gate_ready": live_gate_ready,
        "live_blockers": live_blockers,
        "selected_action": selected_action,
        "reason": reason,
        "blocked_reasons": blockers,
        "known_gaps": _dedupe(blockers + live_blockers),
        "safe_next_actions": [
            "Treat this artifact as no-live rehearsal evidence only.",
            "Do not dispatch a live writer from this artifact.",
            "If selected_action is human_review_required, inspect live_blockers before any live-write enablement.",
        ],
    }
    if output_path is not None and str(output_path).strip():
        path = Path(output_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload["output_path"] = str(path)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def _gate_projection(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(payload.get("ok")),
        "validation_kind": str(payload.get("validation_kind") or ""),
        "blocked_reasons": _list(payload.get("blocked_reasons")),
        "live_gate_ready": bool(payload.get("live_gate_ready", False)),
    }


def _business_scope_gate_command(profile_path: str, contract_path: str) -> str:
    return (
        "./bin/omx_worker_backend validate-business-write-scope"
        f" --profile {profile_path}"
        f" --contract {contract_path}"
    )


def _business_baseline_delta_gate_command(expected_added_files: list[str]) -> str:
    command = (
        "./bin/omx_worker_backend validate-business-baseline-delta"
        " --baseline-status <pre-write-status-file>"
        " --current-status <post-write-status-file>"
        " --expected-baseline-sha256 <pre-write-status-sha256>"
    )
    for path in expected_added_files:
        command += f" --expected-added-file {path}"
    return command + " --json"


def _reviewer_baseline_delta_evidence(expected_added_files: list[str], validation_command: str) -> dict[str, Any]:
    return {
        "validation_kind": "business-baseline-delta",
        "baseline_status_input": "<pre-write-status-file>",
        "current_status_input": "<post-write-status-file>",
        "expected_baseline_sha256_input": "<pre-write-status-sha256>",
        "expected_added_files": expected_added_files,
        "validation_command": validation_command,
        "acceptance_rule": (
            "pre-existing dirty entries are not blockers when unchanged; block only if "
            "baseline-delta validation reports unexpected, removed, changed, missing, "
            "or blocked entries"
        ),
    }


def _rollback_target_blockers(target: str, expected_status: str, expected_sha256: str) -> list[str]:
    blockers: list[str] = []
    if not target:
        blockers.append("rollback.target_file is required")
    if target.startswith(".git") or target.startswith(".omx"):
        blockers.append("rollback.target_file must not be .git or .omx")
    if _is_registry_path(target):
        blockers.append("rollback.target_file must not be a registry path")
    if _is_state_team_path(target):
        blockers.append("rollback.target_file must not be .omx/state/team")
    if target and not _is_under_business_repo(target):
        blockers.append(f"rollback.target_file must be under business repo: {target}")
    if not expected_status:
        blockers.append("rollback.expected_status is required")
    elif target and expected_status != f"?? {target}":
        blockers.append(f"rollback.expected_status must be ?? {target}")
    if not expected_sha256:
        blockers.append("rollback.expected_sha256 is required")
    elif len(expected_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_sha256.lower()):
        blockers.append("rollback.expected_sha256 must be a 64-character lowercase hex sha256")
    return blockers


def _safe_filename(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in str(value).strip())
    return cleaned.strip("-") or "scoped-write-smoke"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except Exception:
        return str(left) == str(right)


def _writer_smoke_prompt(
    contract: dict[str, Any],
    allowed_files: list[str],
    allowed_dirs: list[str],
    forbidden_files: list[str],
    forbidden_dirs: list[str],
) -> str:
    return (
        "Scoped writer candidate. This is not live authorization. "
        f"Task id: {contract.get('task_id', '')}. "
        f"Only these files may change: {', '.join(allowed_files) or '-'}. "
        f"Only these directories may change: {', '.join(allowed_dirs) or '-'}. "
        f"Forbidden files: {', '.join(forbidden_files) or '-'}. "
        f"Forbidden directories: {', '.join(forbidden_dirs) or '-'}. "
        "Do not touch git index, registries, .omx/state/team, raw tmux, direct TUI, direct adapter, or rmux."
    )


def _reviewer_smoke_prompt(contract: dict[str, Any], baseline_delta_evidence: dict[str, Any]) -> str:
    return (
        "Diff reviewer candidate. Read only the writer diff and write-scope contract. "
        f"Task id: {contract.get('task_id', '')}. "
        "Use the baseline-delta evidence before judging dirty-repo status. "
        f"Baseline-delta evidence command: {baseline_delta_evidence.get('validation_command', '')}. "
        "Rule: pre-existing dirty entries are not blockers when unchanged. "
        "Report accept only if every changed file is allowed, validation commands pass, "
        "and no forbidden path or registry/state-team write appears."
    )


def _rollback_smoke_prompt(target: str, expected_status: str, expected_sha256: str) -> str:
    return (
        "Scoped rollback candidate. This is not live authorization. "
        "Do not run rollback without separate user approval. "
        f"Only rollback target: {target}. "
        f"Expected pre-rollback status: {expected_status}. "
        f"Expected pre-rollback sha256: {expected_sha256}. "
        "Only permitted live operation after separate approval is removing that untracked file. "
        "Do not touch git index, registries, .omx/state/team, raw tmux, direct TUI, direct adapter, or rmux."
    )


def _rollback_reviewer_prompt(target: str, expected_status: str, expected_sha256: str) -> str:
    return (
        "Rollback reviewer candidate. Read only rollback evidence. "
        f"Target file: {target}. "
        f"Expected pre-rollback status: {expected_status}. "
        f"Expected pre-rollback sha256: {expected_sha256}. "
        "Accept only if the target file disappeared, git index stayed unchanged, "
        "and no other business, registry, or state-team path changed."
    )


def _validate_changed_files_against_write_scope(contract: dict[str, Any], changed_files: list[str]) -> dict[str, Any]:
    business_repo = str(contract.get("business_repo") or "")
    normalized_changed, changed_blockers = _normalize_business_paths(changed_files, business_repo, "changed_files")
    allowed_file_list, allowed_file_blockers = _normalize_business_paths(
        _list(contract.get("allowed_files")), business_repo, "allowed_files"
    )
    allowed_dirs, allowed_dir_blockers = _normalize_business_paths(
        _list(contract.get("allowed_dirs")), business_repo, "allowed_dirs"
    )
    forbidden_file_list, forbidden_file_blockers = _normalize_business_paths(
        _list(contract.get("forbidden_files")), business_repo, "forbidden_files"
    )
    forbidden_dirs, forbidden_dir_blockers = _normalize_business_paths(
        _list(contract.get("forbidden_dirs")), business_repo, "forbidden_dirs"
    )
    allowed_files = set(allowed_file_list)
    forbidden_files = set(forbidden_file_list)
    blockers: list[str] = []
    blockers.extend(changed_blockers)
    blockers.extend(allowed_file_blockers)
    blockers.extend(allowed_dir_blockers)
    blockers.extend(forbidden_file_blockers)
    blockers.extend(forbidden_dir_blockers)
    out_of_scope: list[str] = [
        str(item).strip().replace("\\", "/")
        for item in changed_files
        if str(item).strip() and _business_relative_path(str(item), business_repo) is None
    ]
    forbidden_changed: list[str] = []

    for path in normalized_changed:
        if _is_registry_path(path) or _is_state_team_path(path):
            forbidden_changed.append(path)
            blockers.append(f"changed file is a forbidden control path: {path}")
            continue
        if _path_matches_files_or_dirs(path, forbidden_files, forbidden_dirs):
            forbidden_changed.append(path)
            blockers.append(f"changed file matches forbidden path: {path}")
            continue
        if not _path_matches_files_or_dirs(path, allowed_files, allowed_dirs):
            out_of_scope.append(path)
            blockers.append(f"changed file outside write scope: {path}")

    max_files = int(contract.get("max_files_changed", 0) or 0)
    if max_files > 0 and len(normalized_changed) > max_files:
        blockers.append("changed file count exceeds max_files_changed")

    return {
        "changed_files": normalized_changed,
        "out_of_scope_files": out_of_scope,
        "forbidden_changed_files": forbidden_changed,
        "max_files_changed": max_files,
        "changed_file_count": len(normalized_changed),
        "ok": not blockers,
        "blocked_reasons": _dedupe(blockers),
    }


def _path_matches_files_or_dirs(path: str, files: set[str], dirs: list[str]) -> bool:
    if path in files:
        return True
    return any(path == directory or path.startswith(directory + "/") for directory in dirs if directory)


def _normalize_business_path(value: str, business_repo: str = BUSINESS_REPO_ROOT) -> str:
    normalized = _business_relative_path(value, business_repo)
    return normalized if normalized is not None else str(value).strip().replace("\\", "/").rstrip("/")


def _normalize_business_paths(values: list[Any], business_repo: str, label: str) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    blockers: list[str] = []
    for value in values:
        if not str(value).strip():
            blockers.append(f"{label} contains an empty path")
            continue
        relative = _business_relative_path(str(value), business_repo)
        if relative is None:
            blockers.append(f"{label} path must resolve under business repo: {value}")
            continue
        normalized.append(relative)
    return _dedupe(normalized), blockers


def _business_relative_path(value: str, business_repo: str) -> str | None:
    text = str(value).strip().replace("\\", "/").rstrip("/")
    root_text = str(business_repo).strip()
    if not text or not root_text or "\x00" in text or "\x00" in root_text:
        return None
    root_path = Path(root_text).expanduser()
    if not root_path.is_absolute():
        return None
    path = Path(text)
    if not path.is_absolute() and ".." in path.parts:
        return None
    try:
        root = root_path.resolve(strict=False)
        resolved = path.resolve(strict=False) if path.is_absolute() else (root / path).resolve(strict=False)
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return relative.as_posix() or "."


def plan_with_authorization_profile(
    request: AuthorizationProfileRequest,
    *,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    root = Path(request.root).resolve()
    profile = load_authorization_profile_file(request.profile_path)
    validation = validate_authorization_profile(profile, profile_path=str(Path(request.profile_path).resolve()))
    blockers = list(validation.get("blocked_reasons", []))
    warnings: list[str] = []
    if request.execution_mode != "no-live":
        blockers.append("plan-with-profile requires execution_mode=no-live")
    blockers.extend(_registry_worker_blockers(request, profile))
    runtime = _runtime_readonly_checks(root, request, command_runner)
    blockers.extend(runtime["blockers"])
    warnings.extend(runtime["warnings"])
    blockers = _dedupe(blockers)
    payload = {
        "schema_version": 1,
        "plan_kind": "authorization-profile-plan",
        "created_at": _utc_now(),
        "ok": not blockers,
        "profile_id": str(profile.get("profile_id", "")),
        "profile_kind": str(profile.get("profile_kind", "")),
        "profile_path": str(Path(request.profile_path).resolve()),
        "execution_mode": request.execution_mode,
        "would_execute_live": False,
        "would_write_registry": False,
        "would_write_business": False,
        "would_write_state_team": False,
        "would_use_rmux": False,
        "would_use_raw_tmux": False,
        "would_use_direct_tui": False,
        "would_use_direct_adapter": False,
        "required_actions": [
            "validate-authorization-profile",
            "validate-business-write-scope",
            "obtain explicit task-scoped authorization before execution",
        ],
        "blockers": blockers,
        "warnings": warnings,
        "validation": validation,
        "runtime_checks": runtime["checks"],
        "allowed_workers": _list(profile.get("allowed_workers")),
        "required_workers": _list(profile.get("required_workers")),
        "allowed_task_kinds": _list(profile.get("allowed_task_kinds")),
        "allowed_scenarios": _list(profile.get("allowed_scenarios")),
        "max_parallel": int(profile.get("max_parallel", 0) or 0),
        "max_parallel_expected_observed": int(profile.get("max_parallel_expected_observed", 0) or 0),
        "wave_model": str(profile.get("wave_model", "")),
        "wave_plan": _wave_plan_from_profile(profile),
        "business_ready_required": bool(profile.get("business_ready_required", False)),
        "business_repo_writes_allowed": bool(profile.get("business_repo_writes_allowed", False)),
        "business_task_allowed": bool(profile.get("business_task_allowed", False)),
        "equivalent_low_level_commands": [],
    }
    if profile.get("profile_kind") == BUSINESS_READONLY_KIND:
        payload.update(
            {
                "required_actions": [
                    "validate-authorization-profile --profile <business-readonly-profile>",
                    "capture business repo before status",
                    "run allowed readonly validation commands",
                    "capture business repo after status",
                    "write report.md plus terminal marker",
                ],
                "allowed_read_roots": _list(profile.get("allowed_read_roots")),
                "allowed_write_roots": _list(profile.get("allowed_write_roots")),
                "forbidden_write_roots": _list(profile.get("forbidden_write_roots")),
                "allowed_commands": _list(profile.get("allowed_commands")),
                "required_outputs": _list(profile.get("required_outputs")),
                "equivalent_low_level_commands": [
                    "validate-authorization-profile --profile <business-readonly-profile> --json",
                    "git -C <business-repo> status --short",
                    "git -C <business-repo> diff --name-only",
                    "git -C <business-repo> diff --cached --name-only",
                    "git -C <business-repo> diff --check",
                    "git -C <business-repo> diff --cached --check",
                ],
            }
        )
    return payload


def execute_with_authorization_profile(
    request: AuthorizationProfileRequest,
    *,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    profile = load_authorization_profile_file(request.profile_path)
    validation = validate_authorization_profile(profile, profile_path=str(Path(request.profile_path).resolve()))
    if not validation.get("ok"):
        return _blocked_execute_result(profile, validation["blocked_reasons"], request, commands=[])
    if not request.allow_profile_live:
        return _blocked_execute_result(profile, ["execute-with-profile requires --allow-profile-live"], request, commands=[])
    if profile.get("profile_kind") == BUSINESS_READONLY_KIND:
        from .business_readonly_review import execute_business_readonly_review

        plan = plan_with_authorization_profile(request, command_runner=command_runner)
        return execute_business_readonly_review(
            request,
            profile=profile,
            validation=validation,
            plan=plan,
            command_runner=command_runner,
        )
    return _blocked_execute_result(
        profile, ["execute-with-profile supports only business readonly profiles"], request, commands=[]
    )


def _registry_worker_blockers(request: AuthorizationProfileRequest, profile: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    registered = _worker_map(_read_json(Path(request.registered_worker_registry_path)).get("workers"))
    opencode = _worker_map(_read_json(Path(request.opencode_visible_registry_path)).get("workers"))
    for worker_id in _list(profile.get("required_workers")):
        candidates = [registry[worker_id] for registry in (registered, opencode) if worker_id in registry]
        if len(candidates) != 1:
            blockers.append(f"required worker must resolve to exactly one registry entry: {worker_id}")
            continue
        worker = candidates[0]
        if worker.get("allow_mixed_worker") is not True or worker.get("mixed_ready") is not True:
            blockers.append(f"worker not mixed-ready: {worker_id}")
        if worker.get("business_ready") is True or worker.get("allow_business_repo_writes") is True:
            blockers.append(f"registered worker business flags must remain false: {worker_id}")
    return _dedupe(blockers)


def _runtime_readonly_checks(root: Path, request: AuthorizationProfileRequest, command_runner: CommandRunner | None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    commands = [
        ("watcher-alerts", _bin(root, request.watcher_bin, "omx_visible_watcher"), ["alerts", "--root", str(root), "--all-workers", "--json"]),
        ("dispatch-status", _bin(root, "", "omx_visible_dispatch"), ["status", "--root", str(root), "--compact", "--json"]),
    ]
    for check_name, bin_path, args in commands:
        if not bin_path.exists():
            warnings.append(f"skipped {check_name}: command missing at {bin_path}")
            checks.append({"check": check_name, "ok": True, "skipped": True, "path": str(bin_path)})
            continue
        evidence = (command_runner or _run_command)([str(bin_path), *args], root, 20)
        checks.append({"check": check_name, "ok": evidence.get("returncode") == 0, "evidence": evidence})
        stdout = str(evidence.get("stdout", ""))
        if check_name == "watcher-alerts":
            payload = _json_stdout(stdout)
            active = payload.get("active_alerts", payload.get("active_alert_count", len(payload.get("alerts", []))))
            if int(active or 0) != 0:
                blockers.append("watcher active alerts must be zero")
        elif check_name == "dispatch-status":
            if evidence.get("returncode") != 0:
                blockers.append(
                    "compact dispatch status check must succeed"
                )
            payload = _json_stdout(stdout)
            active_locks = payload.get("active_locks", payload.get("locks", []))
            global_lock_count = payload.get("global_lock_count")
            if (
                isinstance(active_locks, list)
                and active_locks
                or isinstance(global_lock_count, int)
                and global_lock_count != 0
            ):
                blockers.append("active locks must be zero")
    return {"checks": checks, "blockers": _dedupe(blockers), "warnings": warnings}


def _blocked_execute_result(
    profile: dict[str, Any],
    blockers: list[str],
    request: AuthorizationProfileRequest,
    *,
    commands: list[dict[str, Any]],
    plan: dict[str, Any] | None = None,
    package: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result_kind": "authorization-profile-execution",
        "ok": False,
        "profile_id": str(profile.get("profile_id", "")),
        "profile_path": str(Path(request.profile_path).resolve()),
        "blocked_reasons": _dedupe(blockers),
        "commands": commands,
        "profile_plan": plan or {},
        "profile_authorization_package": package or {},
        "would_execute_live": False,
        "would_write_registry": False,
        "would_write_business": False,
        "would_use_raw_tmux": False,
        "would_use_direct_tui": False,
        "would_use_direct_adapter": False,
        "would_use_rmux": False,
    }


def _wave_plan_from_profile(profile: dict[str, Any]) -> list[dict[str, Any]]:
    waves = profile.get("waves")
    if isinstance(waves, list):
        return [item for item in waves if isinstance(item, dict)]
    return []


def _worker_map(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        return {}
    return {str(item.get("worker_id", "")): item for item in value if isinstance(item, dict)}


def _defaults_validation(ok: bool, blockers: list[str], defaults_path: Path, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "validation_kind": "authorization-profile-defaults",
        "ok": ok,
        "defaults_path": str(defaults_path),
        "root": str(root),
        "default_business_readonly_review_profile": str(payload.get("default_business_readonly_review_profile", "")),
        "default_business_readonly_review_profile_path": str(payload.get("default_business_readonly_review_profile_path", "")),
        "default_business_readonly_live_requires_explicit_allow": bool(payload.get("default_business_readonly_live_requires_explicit_allow", False)),
        "default_business_scoped_write_profile": str(payload.get("default_business_scoped_write_profile", "")),
        "default_business_scoped_write_profile_path": str(payload.get("default_business_scoped_write_profile_path", "")),
        "business_scoped_write_enabled_by_default": bool(payload.get("business_scoped_write_enabled_by_default", False)),
        "forbidden_by_default": _list(payload.get("forbidden_by_default")),
        "next_gate": str(payload.get("next_gate", "")),
        "next_business_gate": str(payload.get("next_business_gate", "")),
        "blocked_reasons": blockers,
    }


def _resolve_under_root(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _command_present(commands: list[str], forbidden: str) -> bool:
    return any(command == forbidden or command.startswith(forbidden + " ") for command in commands)


def _is_registry_path(value: str) -> bool:
    path = str(value)
    return "registered-workers/registered-workers.json" in path or "opencode_visible_workers.json" in path


def _is_state_team_path(value: str) -> bool:
    path = str(value).replace("\\", "/").strip("/")
    return path == ".omx/state/team" or path.startswith(".omx/state/team/") or "/.omx/state/team" in f"/{path}"


def _is_under_business_repo(value: str, business_repo: str = BUSINESS_REPO_ROOT) -> bool:
    return _business_relative_path(value, business_repo) is not None


def _future_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed > datetime.now(UTC)


def prep_created_at_blocker(
    value: str,
    *,
    label: str,
    max_age_seconds: int = PREP_MAX_AGE_SECONDS,
) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return f"{label} created_at must be a valid ISO timestamp"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    age_seconds = int((datetime.now(UTC) - parsed).total_seconds())
    if age_seconds < 0:
        return f"{label} created_at must not be in the future"
    if age_seconds > max_age_seconds:
        return f"{label} created_at is too old ({age_seconds}s > {max_age_seconds}s)"
    return ""


def baseline_delta_evidence_blockers(
    evidence: dict[str, Any],
    expected_added_files: list[Any],
    *,
    prefix: str,
    expected_label: str,
    business_repo: str = "",
) -> list[str]:
    blockers: list[str] = []
    if business_repo:
        expected, expected_blockers = _normalize_business_paths(
            expected_added_files, business_repo, expected_label
        )
        actual, actual_blockers = _normalize_business_paths(
            _list(evidence.get("expected_added_files")),
            business_repo,
            f"{prefix}.expected_added_files",
        )
        blockers.extend(expected_blockers)
        blockers.extend(actual_blockers)
    else:
        expected = [_lexical_business_path(item) for item in expected_added_files if str(item).strip()]
        actual = [
            _lexical_business_path(item)
            for item in _list(evidence.get("expected_added_files"))
            if str(item).strip()
        ]
    if evidence.get("validation_kind") != "business-baseline-delta":
        blockers.append(f"{prefix}.validation_kind must be business-baseline-delta")
    if not actual:
        blockers.append(f"{prefix}.expected_added_files is required")
    elif expected and actual != expected:
        blockers.append(f"{prefix}.expected_added_files must match {expected_label}")
    for key in ("baseline_status_input", "current_status_input", "expected_baseline_sha256_input"):
        if not str(evidence.get(key, "")).strip():
            blockers.append(f"{prefix}.{key} is required")
    validation_command = str(evidence.get("validation_command", ""))
    if "validate-business-baseline-delta" not in validation_command:
        blockers.append(f"{prefix}.validation_command must run validate-business-baseline-delta")
    if "--expected-baseline-sha256" not in validation_command:
        blockers.append(f"{prefix}.validation_command must include --expected-baseline-sha256")
    for item in expected:
        if item and item not in validation_command:
            blockers.append(f"{prefix}.validation_command must include expected_added_files")
            break
    acceptance_rule = str(evidence.get("acceptance_rule", ""))
    if "pre-existing dirty entries" not in acceptance_rule:
        blockers.append(f"{prefix}.acceptance_rule must mention pre-existing dirty entries")
    return blockers


def _lexical_business_path(value: Any) -> str:
    text = str(value).strip().replace("\\", "/").rstrip("/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _bin(root: Path, override: str, name: str) -> Path:
    return Path(override).resolve() if override else root / "bin" / name


def _run_command(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    completed = subprocess.run(argv, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    return {"argv": argv, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}


def _json_stdout(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
