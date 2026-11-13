from __future__ import annotations

from pathlib import Path
from typing import Any

from .opencode_raw_capture_readiness import (
    COMPLETION_TRUTH,
    DEFAULT_CLAIMED_EVENT_TYPES,
    build_plan,
    check_readiness_plan,
)


MANIFEST_KIND = "opencode_isolated_capture_manifest"
CONFIG_ROOT_CONTRACT_STATUS = "source_backed_xdg_config_home"
PLUGIN_SOURCE_SHAPE = "opencode_plugin_module"
UNSET_CONFIG_ENV = ["OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"]
AUTHORIZATION_LEVEL_NO_EXECUTION = "no-execution"
AUTHORIZATION_LEVEL_RUNTIME_PROOF_PROMPTLESS = "runtime-proof-promptless"
RUNTIME_PROOF_REQUIRED_AUTHORITY = {
    "kind": "explicit_runtime_import_smoke_authorization",
    "required_for_levels": [AUTHORIZATION_LEVEL_RUNTIME_PROOF_PROMPTLESS],
    "must_be_operator_granted": True,
    "must_name_temp_root": True,
    "must_name_stop_conditions": True,
}
RUNTIME_PROOF_STOP_CONDITIONS = [
    "interactive_prompt_required",
    "tui_or_session_start_required",
    "serve_or_headless_runtime_required",
    "plugin_install_required",
    "live_config_write_required",
    "task_local_write_required",
    "business_repo_touch_required",
    "unexpected_non_temp_path_write",
    "worker_identity_mismatch",
    "timeout_or_stall",
]


def build_capture_manifest(
    *,
    isolated_config_root: str,
    capture_output_path: str,
    plugin_path: str,
    worker_id: str = "",
    parent_run_id: str = "",
    parent_task_id: str = "",
    authorization_level: str = AUTHORIZATION_LEVEL_NO_EXECUTION,
    runtime_proof_authority: dict[str, Any] | None = None,
    cwd: Path | str | None = None,
) -> dict[str, Any]:
    plan = build_plan(
        isolated_config_root=isolated_config_root,
        capture_output_path=capture_output_path,
        plugin_path=plugin_path,
        worker_id=worker_id,
        parent_run_id=parent_run_id,
        parent_task_id=parent_task_id,
    )
    readiness = check_readiness_plan(plan, cwd=cwd)
    config_root = str(readiness.get("isolated_config_root") or "")
    capture_output = str(readiness.get("planned_capture_output") or "")
    plugin = str(readiness.get("plugin_path") or "")
    plugin_contract = _plugin_contract(plugin, capture_output)

    blocked_reasons = [f"readiness:{reason}" for reason in readiness.get("reasons", [])]
    if not plugin_contract["source_shape_proven"]:
        blocked_reasons.append("plugin_source_shape_unproven")
    if not plugin_contract["runtime_load_proven"]:
        blocked_reasons.append("plugin_runtime_load_unproven")
    authorization = _authorization_gate(
        authorization_level=authorization_level,
        runtime_proof_authority=runtime_proof_authority,
    )
    blocked_reasons.extend(authorization["blocked_reasons"])

    return {
        "schema_version": 1,
        "manifest_kind": MANIFEST_KIND,
        "contract": "no-execution",
        "advisory_only": True,
        "would_execute": False,
        "would_run_capture": False,
        "would_install_plugin": False,
        "would_modify_live_config": False,
        "would_write_task_local": False,
        "readiness": readiness,
        "ready_for_runtime_capture": False,
        "blocked_reasons": blocked_reasons,
        "authorization": authorization,
        "config_root_contract": {
            "status": CONFIG_ROOT_CONTRACT_STATUS,
            "env": {
                "XDG_CONFIG_HOME": config_root,
                "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            },
            "unset_env": list(UNSET_CONFIG_ENV),
            "global_config_path": str(Path(config_root) / "opencode" / "opencode.json") if config_root else "",
            "local_plugin_dir": _local_plugin_dir(config_root),
            "evidence": (
                "OpenCode 1.17.4 debug paths honored XDG_CONFIG_HOME when OPENCODE_CONFIG "
                "and OPENCODE_CONFIG_DIR were unset; OPENCODE_CONFIG was observed as additive."
            ),
        },
        "plugin_contract": plugin_contract,
        "plugin_load_smoke_plan": _plugin_load_smoke_plan(
            config_root=config_root,
            plugin_path=plugin,
            capture_output_path=capture_output,
            blocked_reasons=blocked_reasons,
        ),
        "future_config_shape": _future_config_shape(config_root, capture_output),
        "claimed_event_types": list(DEFAULT_CLAIMED_EVENT_TYPES),
        "expected_output_format": "jsonl",
        "completion_truth": COMPLETION_TRUTH,
        "forbidden_actions": [
            "run_opencode_capture",
            "opencode_plugin_install",
            "live_config_edit",
            "tmux_input",
            "tui_call",
            "task_local_write",
        ],
        "advisory": (
            "manifest only; this does not run OpenCode, create temp config files, install plugins, "
            "write captured JSONL, or prove runtime plugin loading"
        ),
    }


def _plugin_contract(plugin_path: str, capture_output_path: str) -> dict[str, Any]:
    path = Path(plugin_path).expanduser().resolve(strict=False) if plugin_path else Path()
    source = path.read_text(encoding="utf-8") if plugin_path and path.is_file() else ""
    source_shape_proven = _looks_like_plugin_module(source)
    module_format = _module_format(path, source) if source_shape_proven else "unproven"
    return {
        "path": str(path) if plugin_path else "",
        "source_shape": PLUGIN_SOURCE_SHAPE if source_shape_proven else "unproven",
        "source_shape_proven": source_shape_proven,
        "module_format": module_format,
        "runtime_load_proven": False,
        "runtime_load_status": "not_executed",
        "sink_option": {
            "key": "captureOutputPath",
            "value": capture_output_path,
        },
        "supported_hook_names": list(DEFAULT_CLAIMED_EVENT_TYPES),
        "evidence": (
            "source fixture exports an id/server plugin module shape"
            if source_shape_proven
            else "plugin source does not expose a recognizable id/server hook module shape"
        ),
    }


def _authorization_gate(
    *,
    authorization_level: str,
    runtime_proof_authority: dict[str, Any] | None,
) -> dict[str, Any]:
    level = (authorization_level or AUTHORIZATION_LEVEL_NO_EXECUTION).strip() or AUTHORIZATION_LEVEL_NO_EXECUTION
    provided_authority = dict(runtime_proof_authority or {})
    blocked_reasons: list[str] = []

    if level == AUTHORIZATION_LEVEL_NO_EXECUTION:
        authority_status = "not_requested"
        runtime_proof_authorized = False
        blocked_reasons.append("authorization_level:no-execution")
    elif level == AUTHORIZATION_LEVEL_RUNTIME_PROOF_PROMPTLESS:
        if provided_authority:
            authority_status = "provided_but_unexecuted"
            runtime_proof_authorized = True
        else:
            authority_status = "missing"
            runtime_proof_authorized = False
            blocked_reasons.append("runtime_proof_authority_missing")
    else:
        authority_status = "unsupported"
        runtime_proof_authorized = False
        blocked_reasons.append(f"authorization_level:unsupported:{level}")

    return {
        "schema_version": 1,
        "level": level,
        "authority_status": authority_status,
        "runtime_proof_authorized": runtime_proof_authorized,
        "runtime_proof_evidence_proven": False,
        "required_authority": dict(RUNTIME_PROOF_REQUIRED_AUTHORITY),
        "provided_authority": provided_authority,
        "stop_conditions": list(RUNTIME_PROOF_STOP_CONDITIONS),
        "blocked_reasons": blocked_reasons,
        "advisory": (
            "authorization is permission metadata only; it does not run OpenCode, prove plugin import, "
            "invoke hooks, or make the manifest ready for runtime capture"
        ),
    }


def _plugin_load_smoke_plan(
    *,
    config_root: str,
    plugin_path: str,
    capture_output_path: str,
    blocked_reasons: list[str],
) -> dict[str, Any]:
    return {
        "plan_kind": "opencode_plugin_load_smoke_no_execution",
        "env": {
            "XDG_CONFIG_HOME": config_root,
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        },
        "unset_env": list(UNSET_CONFIG_ENV),
        "opencode_config_path": str(Path(config_root) / "opencode" / "opencode.json") if config_root else "",
        "local_plugin_dir": _local_plugin_dir(config_root),
        "planned_local_plugin_path": _local_plugin_path(config_root),
        "plugin_path": plugin_path,
        "capture_output_path": capture_output_path,
        "capture_output_env": "OMX_OPENCODE_RAW_CAPTURE_OUTPUT",
        "recommended_parse_only_commands": [
            ["opencode", "debug", "paths"],
            ["opencode", "debug", "startup"],
            ["opencode", "debug", "config", "--pure"],
            ["opencode", "debug", "info", "--pure"],
        ],
        "runtime_proof_candidate_commands": [],
        "runtime_proof_candidate_status": "unidentified",
        "unsafe_unproven_commands": [
            ["opencode", "debug", "config"],
            ["opencode", "debug", "info"],
        ],
        "would_run_opencode": False,
        "would_run_prompt": False,
        "would_start_tui": False,
        "would_start_serve": False,
        "would_install_plugin": False,
        "would_write_task_local": False,
        "would_create_config": False,
        "blocked_reasons": [reason for reason in blocked_reasons if reason],
    }


def _future_config_shape(config_root: str, capture_output_path: str) -> dict[str, Any]:
    return {
        "opencode_json": {"$schema": "https://opencode.ai/config.json"},
        "local_plugin_dir": _local_plugin_dir(config_root),
        "local_plugin_path": _local_plugin_path(config_root),
        "capture_output_env": "OMX_OPENCODE_RAW_CAPTURE_OUTPUT",
        "capture_output_path": capture_output_path,
    }


def _local_plugin_dir(config_root: str) -> str:
    return str(Path(config_root) / "opencode" / "plugins") if config_root else ""


def _local_plugin_path(config_root: str) -> str:
    plugin_dir = _local_plugin_dir(config_root)
    return str(Path(plugin_dir) / "omx-raw-capture-fixture.mjs") if plugin_dir else ""


def _module_format(path: Path, source: str) -> str:
    if path.suffix == ".mjs" and "export default" in source:
        return "esm_default_export"
    if "module.exports" in source:
        return "commonjs_module_exports"
    if "export default" in source:
        return "esm_default_export"
    return "unproven"


def _looks_like_plugin_module(source: str) -> bool:
    if not source:
        return False
    has_module_export = "module.exports" in source or "export default" in source
    has_server = "server" in source and "async" in source
    has_hooks = all(event_type in source for event_type in DEFAULT_CLAIMED_EVENT_TYPES)
    has_sink = "captureOutputPath" in source
    return has_module_export and has_server and has_hooks and has_sink
