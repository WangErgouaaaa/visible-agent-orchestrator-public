from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CHECKER_KIND = "opencode_raw_capture_readiness"
COMPLETION_TRUTH = "report.md + exactly one terminal marker"
DEFAULT_CAPTURE_SCOPE = "session-chat-tool-only"
DEFAULT_OUTPUT_FORMAT = "jsonl"
DEFAULT_CLAIMED_EVENT_TYPES = [
    "event",
    "chat.message",
    "tool.execute.before",
    "tool.execute.after",
    "command.execute.before",
]
ALLOWED_CLAIMED_EVENT_TYPES = set(DEFAULT_CLAIMED_EVENT_TYPES)

PATH_FIELDS = ("isolated_config_root", "capture_output_path", "plugin_path")
ACTION_FLAGS = {
    "would_modify_live_config": "writes_live_config",
    "would_call_tui": "calls_tui",
    "would_send_tmux_input": "sends_tmux_input",
    "would_run_capture": "runs_capture",
    "would_install_plugin": "installs_plugin",
    "would_write_task_local": "writes_task_local",
}


class RawCaptureReadinessError(Exception):
    """Raised when a raw-capture readiness plan cannot be parsed."""


def build_plan(
    *,
    isolated_config_root: str,
    capture_output_path: str,
    plugin_path: str,
    worker_id: str = "",
    parent_run_id: str = "",
    parent_task_id: str = "",
) -> dict[str, Any]:
    return {
        "isolated_config_root": isolated_config_root,
        "capture_output_path": capture_output_path,
        "plugin_path": plugin_path,
        "worker_id": worker_id,
        "parent_run_id": parent_run_id,
        "parent_task_id": parent_task_id,
        "would_modify_live_config": False,
        "would_call_tui": False,
        "would_send_tmux_input": False,
        "would_run_capture": False,
        "would_install_plugin": False,
        "would_write_task_local": False,
        "capture_scope": DEFAULT_CAPTURE_SCOPE,
        "advisory_only": True,
        "expected_output_format": DEFAULT_OUTPUT_FORMAT,
        "lifecycle_hooks_proven": False,
        "claimed_event_types": list(DEFAULT_CLAIMED_EVENT_TYPES),
    }


def check_readiness_plan(plan: dict[str, Any], *, cwd: Path | str | None = None) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise RawCaptureReadinessError("malformed_plan_json")

    base = Path(cwd).resolve(strict=False) if cwd is not None else Path.cwd().resolve(strict=False)
    reasons: list[str] = []
    resolved_paths: dict[str, str] = {}

    for field in PATH_FIELDS:
        raw_value = str(plan.get(field) or "").strip()
        if not raw_value:
            reasons.append(f"missing_path:{field}")
            resolved_paths[field] = ""
            continue
        resolved = _resolve_path(raw_value, base)
        resolved_paths[field] = str(resolved)
        path_reason = _classify_forbidden_path(field, resolved)
        if path_reason:
            reasons.append(path_reason)

    action_values: dict[str, bool] = {}
    for source_key, output_key in ACTION_FLAGS.items():
        value = _flag_value(plan, source_key, output_key)
        action_values[output_key] = value
        if value:
            reasons.append(f"forbidden_action:{source_key}")

    metadata_values, metadata_reasons = _metadata_values(plan)
    reasons.extend(metadata_reasons)

    return {
        "schema_version": 1,
        "checker_kind": CHECKER_KIND,
        "ready_for_future_capture": not reasons,
        "reasons": reasons,
        "isolated_config_root": resolved_paths.get("isolated_config_root", ""),
        "planned_capture_output": resolved_paths.get("capture_output_path", ""),
        "plugin_path": resolved_paths.get("plugin_path", ""),
        "worker_id": str(plan.get("worker_id") or "").strip(),
        "parent_run_id": str(plan.get("parent_run_id") or "").strip(),
        "parent_task_id": str(plan.get("parent_task_id") or "").strip(),
        **action_values,
        **metadata_values,
        "checker_writes": False,
        "control_writes": False,
        "completion_truth": COMPLETION_TRUTH,
        "advisory": "readiness only; this checker does not run OpenCode, install plugins, or write task evidence",
    }


def read_plan_json(value: str, stdin_text: str | None = None) -> dict[str, Any]:
    try:
        text = stdin_text if value == "-" else Path(value).read_text(encoding="utf-8")
        data = json.loads(text or "")
    except FileNotFoundError as exc:
        raise RawCaptureReadinessError("missing_plan_json") from exc
    except json.JSONDecodeError as exc:
        raise RawCaptureReadinessError("malformed_plan_json") from exc
    if not isinstance(data, dict):
        raise RawCaptureReadinessError("malformed_plan_json")
    return data


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _classify_forbidden_path(field: str, path: Path) -> str:
    if _looks_like_live_opencode_config(path):
        return f"live_opencode_config_path:{field}"
    if _looks_like_live_runtime_artifact(path):
        return f"live_runtime_artifact_path:{field}"
    return ""


def _looks_like_live_opencode_config(path: Path) -> bool:
    roots = [Path.home().expanduser().resolve(strict=False) / ".config" / "opencode"]
    for root in roots:
        if _is_relative_to(path, root):
            return True
    parts = path.parts
    for index, part in enumerate(parts[:-1]):
        if part == ".config" and parts[index + 1] == "opencode":
            return True
    return False


def _looks_like_live_runtime_artifact(path: Path) -> bool:
    return ".omx" in path.parts


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _flag_value(plan: dict[str, Any], source_key: str, output_key: str) -> bool:
    if source_key in plan:
        return _as_bool(plan.get(source_key))
    return _as_bool(plan.get(output_key))


def _metadata_values(plan: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    capture_scope = str(plan.get("capture_scope") or DEFAULT_CAPTURE_SCOPE).strip()
    expected_output_format = str(plan.get("expected_output_format") or DEFAULT_OUTPUT_FORMAT).strip()
    advisory_only = _as_bool(plan.get("advisory_only", True))
    lifecycle_hooks_proven = _as_bool(plan.get("lifecycle_hooks_proven"))
    claimed_event_types = _claimed_event_types(plan.get("claimed_event_types"))

    if capture_scope != DEFAULT_CAPTURE_SCOPE:
        reasons.append(f"unsupported_capture_scope:{capture_scope}")
    if not advisory_only:
        reasons.append("forbidden_metadata:advisory_only_false")
    if expected_output_format != DEFAULT_OUTPUT_FORMAT:
        reasons.append(f"unsupported_output_format:{expected_output_format}")
    if lifecycle_hooks_proven:
        reasons.append("unproven_lifecycle_claim")
    for event_type in claimed_event_types:
        if event_type not in ALLOWED_CLAIMED_EVENT_TYPES:
            reasons.append(f"unsupported_event_type:{event_type}")

    return (
        {
            "capture_scope": capture_scope,
            "advisory_only": advisory_only,
            "expected_output_format": expected_output_format,
            "lifecycle_hooks_proven": lifecycle_hooks_proven,
            "claimed_event_types": claimed_event_types,
        },
        reasons,
    )


def _claimed_event_types(value: Any) -> list[str]:
    if value is None:
        return list(DEFAULT_CLAIMED_EVENT_TYPES)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "0", "false", "no", "off"}:
            return False
        if normalized in {"1", "true", "yes", "on"}:
            return True
    return bool(value)
