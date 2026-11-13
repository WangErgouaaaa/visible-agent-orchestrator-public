from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runtime_paths import runtime_root, workspace_root


SCHEMA_VERSION = 1
PACKAGE_KIND = "opencode-runtime-proof-authorization-package"
APPROVAL_KIND = "opencode-runtime-proof-approval-record"
AUTHORIZATION_LEVEL = "runtime-proof-promptless"
BACKEND_KIND = "opencode_tui"
UNSET_CONFIG_ENV = ["OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"]


def forbidden_paths(
    selected_runtime_root: Path | str | None = None,
    selected_workspace_root: Path | str | None = None,
) -> list[str]:
    runtime = runtime_root(selected_runtime_root)
    workspace = workspace_root(selected_workspace_root)
    return [
        str(Path.home() / ".config" / "opencode"),
        str(runtime / ".omx"),
        str(workspace),
    ]


def manifest_snapshot_sha256(manifest: dict[str, Any]) -> str:
    return _hash_json(_manifest_snapshot_payload(manifest))


def build_runtime_proof_authorization_package(
    *,
    manifest: dict[str, Any],
    package_id: str,
    target_worker_id: str,
    approval_id: str,
    approval_expires_at: str,
    approved_argv: list[str],
    timebox_seconds: int,
    disposable_root: str,
    selected_runtime_root: Path | str | None = None,
    selected_workspace_root: Path | str | None = None,
) -> dict[str, Any]:
    root = str(Path(disposable_root).expanduser().resolve(strict=False))
    config_home = str(Path(root) / "config-home")
    config_json_path = str(Path(config_home) / "opencode" / "opencode.json")
    raw_events_jsonl_path = str(Path(root) / "raw-events.jsonl")
    runtime_log_path = str(Path(root) / "runtime.log")
    postcheck_marker_path = str(Path(root) / "postcheck.json")
    cleanup_receipt_path = str(Path(root) / "cleanup-proof.json")
    snapshot = _manifest_snapshot(manifest)
    protected_paths = forbidden_paths(selected_runtime_root, selected_workspace_root)
    runtime = runtime_root(selected_runtime_root)
    approval_record = {
        "approval_kind": APPROVAL_KIND,
        "approval_record_id": approval_id,
        "authorization_level": AUTHORIZATION_LEVEL,
        "approval_id": approval_id,
        "expires_at": approval_expires_at,
        "approval_expires_at": approval_expires_at,
        "attempt_limit": 1,
        "backend_kind": BACKEND_KIND,
        "target_worker": {
            "worker_id": target_worker_id,
            "workspace_root": str(runtime),
        },
        "target_worker_id": target_worker_id,
        "approved_argv": list(approved_argv),
        "timebox_seconds": int(timebox_seconds),
        "approved_env": {
            "XDG_CONFIG_HOME": config_home,
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        },
        "unset_env": list(UNSET_CONFIG_ENV),
        "approved_write_roots": [root],
        "forbidden_paths": protected_paths,
        "required_stop_conditions": _list_str(_dict(manifest.get("authorization")).get("stop_conditions")),
        "single_attempt_only": True,
        "no_retry": True,
    }
    package = {
        "schema_version": SCHEMA_VERSION,
        "package_kind": PACKAGE_KIND,
        "package_id": package_id,
        "target_worker_id": target_worker_id,
        "would_execute_opencode": False,
        "would_create_artifacts": False,
        "would_write_live_config": False,
        "would_write_task_local": False,
        "would_write_registry": False,
        "would_write_state_team": False,
        "manifest_snapshot": snapshot,
        "manifest_snapshot_sha256": snapshot["sha256"],
        "approval_record": approval_record,
        "disposable_artifacts": {
            "temp_root": root,
            "xdg_config_home": config_home,
            "opencode_config_path": config_json_path,
            "config_json_path": config_json_path,
            "sink_file": raw_events_jsonl_path,
            "raw_events_jsonl_path": raw_events_jsonl_path,
            "runtime_log_path": runtime_log_path,
            "postcheck_marker_path": postcheck_marker_path,
            "cleanup_receipt_path": cleanup_receipt_path,
            "marker_dir": str(Path(root) / "markers"),
            "stdout_log": str(Path(root) / "logs" / "stdout.txt"),
            "stderr_log": str(Path(root) / "logs" / "stderr.txt"),
            "allowed_write_roots": [root],
            "cleanup_required": True,
            "cleanup_scope": "temp_root_only",
            "leak_allowed": False,
            "task_local_marker_write_allowed": False,
        },
        "expected_post_checks": {
            "config_file_location_matches_xdg_root": True,
            "plugin_tuple_path_matches_manifest": True,
            "sink_path_matches_plan": True,
            "task_local_artifacts_written": False,
            "live_config_artifacts_written": False,
            "repo_artifacts_written": False,
            "forbidden_path_writes_detected": False,
            "interactive_prompt_seen": False,
            "tui_started": False,
            "serve_started": False,
            "headless_started": False,
            "plugin_runtime_load_proven": False,
            "stop_condition_triggered": False,
            "cleanup_leftover_paths": [],
            "temp_root_only_writes": True,
            "cleanup_completed": True,
        },
        "forbidden_paths": protected_paths,
        "advisory": (
            "authorization package only; it does not run OpenCode, create temp files, "
            "write live config, write task-local markers, or prove runtime plugin loading"
        ),
    }
    package["validation"] = validate_runtime_proof_authorization_package(package)
    return package


def validate_runtime_proof_authorization_package(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings = [
        "Runtime-proof authorization package validation is no-execution",
        "Package does not prove plugin import or hook invocation",
    ]

    if payload.get("package_kind") != PACKAGE_KIND:
        errors.append(f"package_kind must be {PACKAGE_KIND}")
    if int(payload.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for key in (
        "would_execute_opencode",
        "would_create_artifacts",
        "would_write_live_config",
        "would_write_task_local",
        "would_write_registry",
        "would_write_state_team",
    ):
        if bool(payload.get(key, False)):
            errors.append(f"{key} must be false")

    snapshot = _dict(payload.get("manifest_snapshot"))
    expected_snapshot_hash = str(snapshot.get("sha256", ""))
    actual_snapshot_hash = _hash_json({key: value for key, value in snapshot.items() if key != "sha256"})
    if str(payload.get("manifest_snapshot_sha256", "")) != expected_snapshot_hash or expected_snapshot_hash != actual_snapshot_hash:
        errors.append("manifest_snapshot_sha256 mismatch")
    if snapshot.get("authorization_level") != AUTHORIZATION_LEVEL or not bool(snapshot.get("runtime_proof_authorized")):
        errors.append("runtime-proof authorization not provided")
    if bool(snapshot.get("runtime_proof_evidence_proven", True)):
        errors.append("runtime proof evidence must remain unproven before execution")

    approval = _dict(payload.get("approval_record"))
    if approval.get("approval_kind") != APPROVAL_KIND:
        errors.append(f"approval_kind must be {APPROVAL_KIND}")
    if approval.get("approval_record_id") != approval.get("approval_id"):
        errors.append("approval_record_id must match approval_id")
    if approval.get("authorization_level") != AUTHORIZATION_LEVEL:
        errors.append(f"approval authorization_level must be {AUTHORIZATION_LEVEL}")
    if approval.get("backend_kind") != BACKEND_KIND:
        errors.append(f"backend_kind must be {BACKEND_KIND}")
    if int(approval.get("attempt_limit", 0) or 0) != 1:
        errors.append("attempt_limit must be 1")
    if not approval.get("approval_id"):
        errors.append("approval_id missing")
    if not approval.get("expires_at"):
        errors.append("expires_at missing")
    if not approval.get("approval_expires_at"):
        errors.append("approval_expires_at missing")
    expires_at = str(approval.get("expires_at") or "")
    approval_expires_at = str(approval.get("approval_expires_at") or "")
    if expires_at and approval_expires_at and expires_at != approval_expires_at:
        errors.append("expires_at must match approval_expires_at")
    elif approval_expires_at:
        try:
            expiry = _parse_iso_datetime(approval_expires_at)
        except ValueError:
            errors.append(
                "approval expiry must be an ISO-8601 timestamp with timezone"
            )
        else:
            if expiry <= datetime.now(timezone.utc):
                errors.append("approval has expired")
    if int(approval.get("timebox_seconds", 0) or 0) <= 0:
        errors.append("timebox_seconds must be positive")
    target_worker = _dict(approval.get("target_worker"))
    if target_worker.get("worker_id") != approval.get("target_worker_id"):
        errors.append("target_worker.worker_id must match target_worker_id")
    if not bool(approval.get("single_attempt_only")):
        errors.append("single_attempt_only must be true")
    if not bool(approval.get("no_retry")):
        errors.append("no_retry must be true")

    smoke_plan = _dict(snapshot.get("plugin_load_smoke_plan"))
    unsafe_commands = {
        tuple(item)
        for item in _list(smoke_plan.get("unsafe_unproven_commands"))
        if isinstance(item, list)
    }
    candidate_commands = {
        tuple(item)
        for item in _list(smoke_plan.get("runtime_proof_candidate_commands"))
        if isinstance(item, list)
    }
    approved_argv = tuple(_list_str(approval.get("approved_argv")))
    if approved_argv in unsafe_commands:
        errors.append("approved argv is marked unsafe_unproven in the manifest")
    if not candidate_commands:
        errors.append("manifest has no runtime_proof_candidate_commands")
    elif approved_argv not in candidate_commands:
        errors.append("approved argv is not a runtime_proof_candidate command from the manifest")
    if _list_str(approval.get("required_stop_conditions")) != _list_str(snapshot.get("stop_conditions")):
        errors.append("required_stop_conditions must match manifest stop_conditions")

    artifacts = _dict(payload.get("disposable_artifacts"))
    temp_root = str(artifacts.get("temp_root", ""))
    approved_write_roots = _list_str(approval.get("approved_write_roots"))
    if not temp_root:
        errors.append("temp_root missing")
    if temp_root and approved_write_roots != [temp_root]:
        errors.append("approved_write_roots must contain only temp_root")
    if not bool(artifacts.get("cleanup_required")):
        errors.append("cleanup_required must be true")
    if bool(artifacts.get("leak_allowed")):
        errors.append("leak_allowed must be false")
    if bool(artifacts.get("task_local_marker_write_allowed")):
        errors.append("task_local_marker_write_allowed must be false")
    errors.extend(_path_guard_errors(temp_root))
    errors.extend(
        runtime_proof_write_scope_errors(
            temp_root=temp_root,
            approved_env=_dict(approval.get("approved_env")),
            unset_env=_list_str(approval.get("unset_env")),
            approved_write_roots=approved_write_roots,
            allowed_write_roots=_list_str(artifacts.get("allowed_write_roots")),
            artifacts=artifacts,
        )
    )

    post = _dict(payload.get("expected_post_checks"))
    if not bool(post.get("temp_root_only_writes")):
        errors.append("temp_root_only_writes must be true")
    for key in ("task_local_artifacts_written", "live_config_artifacts_written", "repo_artifacts_written", "forbidden_path_writes_detected"):
        if bool(post.get(key, False)):
            errors.append(f"{key} must be false")

    return {
        "ok": not errors,
        "errors": _dedupe(errors),
        "warnings": warnings,
        "no_execution": not bool(payload.get("would_execute_opencode", False)),
        "no_live_config_write": not bool(payload.get("would_write_live_config", False)),
        "no_task_local_write": not bool(payload.get("would_write_task_local", False)),
        "temp_root_only": bool(post.get("temp_root_only_writes")),
    }


def validate_runtime_proof_authorization_package_file(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_runtime_proof_authorization_package(_dict(payload))


def output_dir_guard_errors(output_dir: str | Path) -> list[str]:
    return _guarded_path_errors(str(output_dir), "output_dir")


def runtime_proof_write_scope_errors(
    *,
    temp_root: str,
    approved_env: dict[str, Any],
    unset_env: list[str],
    approved_write_roots: list[str],
    allowed_write_roots: list[str],
    artifacts: dict[str, Any],
) -> list[str]:
    if not temp_root:
        return []

    errors: list[str] = []
    root = Path(temp_root).expanduser().resolve(strict=False)
    if not _paths_contain_only_root(approved_write_roots, root):
        errors.append("approved_write_roots must contain only temp_root")
    if not _paths_contain_only_root(allowed_write_roots, root):
        errors.append(
            "disposable_artifacts.allowed_write_roots must contain only temp_root"
        )

    if _list_str(unset_env) != UNSET_CONFIG_ENV:
        errors.append("unset_env must exactly disable live OpenCode config overrides")
    if str(approved_env.get("OPENCODE_DISABLE_PROJECT_CONFIG") or "") != "1":
        errors.append("approved_env.OPENCODE_DISABLE_PROJECT_CONFIG must be 1")

    xdg_config_home = str(approved_env.get("XDG_CONFIG_HOME") or "")
    if not _path_is_within_root(xdg_config_home, root):
        errors.append("approved_env.XDG_CONFIG_HOME must be within temp_root")

    artifact_path_fields = (
        "xdg_config_home",
        "opencode_config_path",
        "config_json_path",
        "sink_file",
        "raw_events_jsonl_path",
        "runtime_log_path",
        "postcheck_marker_path",
        "cleanup_receipt_path",
        "marker_dir",
        "stdout_log",
        "stderr_log",
    )
    for field_name in artifact_path_fields:
        candidate = str(artifacts.get(field_name) or "")
        if not _path_is_within_root(candidate, root):
            errors.append(
                f"disposable_artifacts.{field_name} must be within temp_root"
            )

    artifact_config_home = str(artifacts.get("xdg_config_home") or "")
    if xdg_config_home and artifact_config_home:
        if _resolve_path(xdg_config_home) != _resolve_path(artifact_config_home):
            errors.append(
                "approved_env.XDG_CONFIG_HOME must match "
                "disposable_artifacts.xdg_config_home"
            )
        expected_config_path = _resolve_path(
            str(Path(artifact_config_home) / "opencode" / "opencode.json")
        )
        for field_name in ("opencode_config_path", "config_json_path"):
            if _resolve_path(str(artifacts.get(field_name) or "")) != expected_config_path:
                errors.append(
                    f"disposable_artifacts.{field_name} must match the isolated "
                    "XDG config path"
                )
    if _resolve_path(str(artifacts.get("sink_file") or "")) != _resolve_path(
        str(artifacts.get("raw_events_jsonl_path") or "")
    ):
        errors.append(
            "disposable_artifacts.sink_file must match raw_events_jsonl_path"
        )
    return _dedupe(errors)


def write_runtime_proof_authorization_package(
    package: dict[str, Any],
    *,
    output_dir: str | Path,
) -> dict[str, str]:
    errors = output_dir_guard_errors(output_dir)
    if errors:
        raise ValueError("; ".join(errors))
    package_id = str(package.get("package_id") or "runtime-proof-authorization-package")
    safe_id = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in package_id).strip(".-")
    if not safe_id:
        safe_id = "runtime-proof-authorization-package"
    out = Path(output_dir).expanduser().resolve(strict=False)
    out.mkdir(parents=True, exist_ok=True)
    package_path = out / f"{safe_id}.package.json"
    markdown_path = out / f"{safe_id}.package.md"
    package_path.write_text(json.dumps(package, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(render_runtime_proof_authorization_package_markdown(package), encoding="utf-8")
    return {
        "package_path": str(package_path),
        "package_markdown_path": str(markdown_path),
    }


def render_runtime_proof_authorization_package_markdown(package: dict[str, Any]) -> str:
    approval = _dict(package.get("approval_record"))
    validation = _dict(package.get("validation"))
    argv = " ".join(_list_str(approval.get("approved_argv"))) or "-"
    errors = _list_str(validation.get("errors"))
    error_text = "\n".join(f"- {item}" for item in errors) if errors else "- none"
    return (
        "# OpenCode Runtime-Proof Authorization Package\n\n"
        "This review artifact does not execute OpenCode, does not create runtime "
        "artifacts, does not write live config, and does not write task-local "
        "protocol markers.\n\n"
        f"- package_id: {package.get('package_id') or '-'}\n"
        f"- target_worker_id: {package.get('target_worker_id') or '-'}\n"
        f"- approval_id: {approval.get('approval_id') or '-'}\n"
        f"- approved_argv: `{argv}`\n"
        f"- validation_ok: {str(bool(validation.get('ok'))).lower()}\n\n"
        "The package does not prove plugin import or hook invocation. Future "
        "runtime execution still needs separate explicit user authorization and "
        "must preserve report.md plus exactly one terminal marker as completion "
        "truth.\n\n"
        "## Validation Errors\n\n"
        f"{error_text}\n"
    )


def _manifest_snapshot(manifest: dict[str, Any]) -> dict[str, Any]:
    payload = _manifest_snapshot_payload(manifest)
    return {"sha256": _hash_json(payload), **payload}


def _manifest_snapshot_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    authorization = _dict(manifest.get("authorization"))
    return {
        "manifest_kind": str(manifest.get("manifest_kind", "")),
        "ready_for_runtime_capture": bool(manifest.get("ready_for_runtime_capture", False)),
        "blocked_reasons": _list_str(manifest.get("blocked_reasons")),
        "authorization_level": str(authorization.get("level", "")),
        "runtime_proof_authorized": bool(authorization.get("runtime_proof_authorized", False)),
        "runtime_proof_evidence_proven": bool(authorization.get("runtime_proof_evidence_proven", False)),
        "stop_conditions": _list_str(authorization.get("stop_conditions")),
        "plugin_load_smoke_plan": _dict(manifest.get("plugin_load_smoke_plan")),
        "config_root_contract": _dict(manifest.get("config_root_contract")),
        "plugin_contract": _dict(manifest.get("plugin_contract")),
    }


def _hash_json(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _path_guard_errors(temp_root: str) -> list[str]:
    return _guarded_path_errors(temp_root, "disposable_root")


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _resolve_path(value: str) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _paths_contain_only_root(values: list[str], root: Path) -> bool:
    return (
        len(values) == 1
        and bool(values[0])
        and _resolve_path(values[0]) == root
    )


def _path_is_within_root(candidate: str, root: Path) -> bool:
    if not candidate:
        return False
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        return False
    return _is_relative_to(path.resolve(strict=False), root)


def _guarded_path_errors(candidate: str, field_name: str) -> list[str]:
    if not candidate:
        return []
    errors: list[str] = []
    forbidden = [Path(path).expanduser().resolve(strict=False) for path in forbidden_paths()]
    resolved = Path(candidate).expanduser().resolve(strict=False)
    repo = Path(__file__).resolve().parents[2]
    if _is_relative_to(resolved, repo):
        errors.append(f"{field_name} uses repo path")
    for forbidden_path in forbidden:
        if _is_relative_to(resolved, forbidden_path):
            label = ".omx path" if ".omx" in str(forbidden_path) else "forbidden path"
            errors.append(f"{field_name} uses {label}")
    return errors


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _list_str(value: Any) -> list[str]:
    return [str(item) for item in _list(value)]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
