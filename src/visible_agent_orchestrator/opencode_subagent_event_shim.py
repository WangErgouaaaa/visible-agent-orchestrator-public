from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.subagent_projection import VALID_RETRIEVAL_STATES, VALID_SUBAGENT_STATUSES
from visible_agent_orchestrator.subagent_ledger import (
    SubagentLedgerError,
    append_jsonl as _append_jsonl,
)


SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
LEDGER_JSONL = "subagent_ledger.jsonl"


class SubagentEventError(Exception):
    """Raised when captured OpenCode event evidence is unsafe or malformed."""


def _append_ledger(path: Path, entry: dict[str, Any]) -> None:
    try:
        _append_jsonl(path, entry)
    except SubagentLedgerError as exc:
        raise SubagentEventError(f"ledger:{exc}") from exc


def convert_event(
    root: Path | str,
    task_dir: Path | str,
    worker_id: str,
    event: dict[str, Any],
    *,
    write_task_local: bool = False,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    task_path = Path(task_dir).resolve(strict=False)
    run, task = parse_visible_task_path(root_path, task_path)
    parent_run_id = f"visible-runs/{run}"
    worker = str(worker_id or "").strip()
    if not worker:
        raise SubagentEventError("missing_worker")
    if not isinstance(event, dict):
        raise SubagentEventError("malformed_event_json")

    _validate_parent_identity(event, worker, parent_run_id, task)
    entry, warnings = event_to_ledger_entry(event, worker, parent_run_id, task)
    ledger_path = task_path / LEDGER_JSONL
    if write_task_local:
        _append_ledger(ledger_path, entry)
    return {
        "schema_version": 1,
        "shim_kind": "opencode_subagent_event_shim",
        "writes_task_local": bool(write_task_local),
        "control_writes": False,
        "ledger_path": rel(ledger_path, root_path),
        "entry": entry,
        "warnings": warnings,
        "advisory": "subagent_ledger.jsonl is advisory evidence only and does not change completion truth",
    }


def replay_events(
    root: Path | str,
    task_dir: Path | str,
    worker_id: str,
    events: list[dict[str, Any]],
    *,
    write_task_local: bool = False,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    task_path = Path(task_dir).resolve(strict=False)
    run, task = parse_visible_task_path(root_path, task_path)
    parent_run_id = f"visible-runs/{run}"
    worker = str(worker_id or "").strip()
    if not worker:
        raise SubagentEventError("missing_worker")

    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            raise SubagentEventError("malformed_event_jsonl")
        _validate_parent_identity(event, worker, parent_run_id, task)
        entry, event_warnings = event_to_ledger_entry(event, worker, parent_run_id, task)
        entries.append(entry)
        warnings.extend(event_warnings)

    ledger_path = task_path / LEDGER_JSONL
    if write_task_local:
        for entry in entries:
            _append_ledger(ledger_path, entry)
    return {
        "schema_version": 1,
        "shim_kind": "opencode_subagent_event_shim",
        "replay_kind": "jsonl",
        "writes_task_local": bool(write_task_local),
        "control_writes": False,
        "ledger_path": rel(ledger_path, root_path),
        "event_count": len(entries),
        "entries": entries,
        "warnings": warnings,
        "advisory": "subagent_ledger.jsonl is advisory evidence only and does not change completion truth",
    }


def event_to_ledger_entry(
    event: dict[str, Any],
    worker_id: str,
    parent_run_id: str,
    parent_task_id: str,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    event_id = str(event.get("event_id") or event.get("id") or "").strip()
    event_type = str(event.get("event_type") or event.get("type") or "").strip()
    event_status = str(event.get("event_status") or "").strip()
    timestamp = str(event.get("timestamp") or event.get("recorded_at") or event.get("ts") or "").strip()
    explicit_status = str(event.get("status") or "").strip()
    raw_status = str(explicit_status or _status_from_event_status(event_status) or "unknown").strip()
    status = raw_status or "unknown"
    transport_completed_with_error = (
        not explicit_status
        and status == "completed"
        and _error_payload_present(event)
    )
    if transport_completed_with_error:
        status = "unknown"
    if status == "unknown" and _background_terminal_error(event):
        status = "failed"
    if status not in VALID_SUBAGENT_STATUSES:
        warnings.append(_unknown_status_warning(status))
        status = "unknown"
    if transport_completed_with_error:
        warnings.append(
            "transport_event_status_completed_with_error_payload: "
            f"transport completion unreliable; child state recorded as {status}"
        )

    subagent_id = str(event.get("subagent_id") or "").strip()
    if not subagent_id:
        subagent_id = _fallback_subagent_id(event_id, event_type)

    entry: dict[str, Any] = {
        "schema_version": 1,
        "parent_worker_id": worker_id,
        "parent_run_id": parent_run_id,
        "parent_task_id": parent_task_id,
        "subagent_id": subagent_id,
        "agent": str(event.get("agent") or "opencode").strip() or "opencode",
        "lane": str(event.get("lane") or event_type or "opencode-plugin-event").strip()
        or "opencode-plugin-event",
        "purpose": str(event.get("purpose") or "OpenCode plugin event evidence").strip()
        or "OpenCode plugin event evidence",
        "status": status,
        "trace_source": str(event.get("source") or "opencode_worker_plugin").strip()
        or "opencode_worker_plugin",
        "trace_event_type": event_type,
        "trace_event_status": event_status,
        "trace_event_id": event_id,
        "trace_session_id": str(event.get("session_id") or event.get("sessionID") or event.get("sessionId") or "").strip(),
        "trace_task_id": str(
            event.get("task_id")
            or event.get("taskID")
            or event.get("taskId")
            or event.get("background_task_id")
            or ""
        ).strip(),
        "trace_tool_name": str(event.get("tool_name") or event.get("tool") or "").strip(),
        "trace_recorded_at": timestamp,
        "trace_confidence": str(event.get("confidence") or "observed").strip() or "observed",
    }
    for optional in (
        "artifact",
        "usable_content",
        "parse_error",
        "completion_state",
        "error",
        "semantic_status",
        "retry_scope",
        "required_or_optional",
        "latest_useful_moment",
        "retrieval_state",
        "merge_readiness",
        "requested_launcher_contract",
        "actual_launcher_used",
        "audit_status",
        "canonical_retrieval_handle",
        "fallback_session_id",
    ):
        value = str(event.get(optional) or "").strip()
        if value:
            entry[optional] = value
    background = event.get("background_output")
    background_row = background if isinstance(background, dict) else {}
    for optional in ("raw_status", "completion_seen"):
        value = event.get(optional)
        if value is None and optional in background_row:
            value = background_row.get(optional)
        text = str(value or "").strip()
        if text:
            entry[optional] = text
    if "retrieval_state" not in entry:
        retrieval_state = _retrieval_state_from_event(event)
        if not retrieval_state and raw_status in VALID_RETRIEVAL_STATES:
            retrieval_state = raw_status
        if retrieval_state:
            entry["retrieval_state"] = retrieval_state
    if "fallback_session_id" not in entry:
        fallback_session_id = _fallback_session_id_from_event(event)
        if fallback_session_id:
            entry["fallback_session_id"] = fallback_session_id
    for optional_list in ("missing_or_unexpressed_fields", "mitigation_steps_taken"):
        value = event.get(optional_list)
        if isinstance(value, list):
            entry[optional_list] = [str(item) for item in value]
    source_identity = event.get("source_identity")
    if isinstance(source_identity, dict):
        entry["source_identity"] = {
            key: str(source_identity.get(key) or "")
            for key in ("root_kind", "observed_at", "path", "mtime", "sha256")
            if str(source_identity.get(key) or "")
        }
    if timestamp and status in {"created", "running", "waiting_input"}:
        entry["started_at"] = timestamp
    if timestamp and status in {"blocked", "failed", "completed"}:
        entry["completed_at"] = timestamp
    return entry, warnings


def parse_visible_task_path(root: Path, task_dir: Path) -> tuple[str, str]:
    resolved = task_dir.resolve(strict=False)
    try:
        parts = resolved.relative_to(visible_runs_root(root)).parts
    except ValueError as exc:
        raise SubagentEventError("unsafe_task_dir") from exc
    if len(parts) != 3 or parts[1] != "tasks" or parts[0] in {"_locks", "_watcher"}:
        raise SubagentEventError("unsafe_task_dir")
    return parts[0], parts[2]


def read_event_json(value: str, stdin_text: str | None = None) -> dict[str, Any]:
    try:
        text = stdin_text if value == "-" else Path(value).read_text(encoding="utf-8")
        data = json.loads(text or "")
    except FileNotFoundError as exc:
        raise SubagentEventError("missing_event_json") from exc
    except json.JSONDecodeError as exc:
        raise SubagentEventError("malformed_event_json") from exc
    if not isinstance(data, dict):
        raise SubagentEventError("malformed_event_json")
    return data


def read_event_jsonl(value: str, stdin_text: str | None = None) -> list[dict[str, Any]]:
    try:
        text = stdin_text if value == "-" else Path(value).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SubagentEventError("missing_event_jsonl") from exc

    events: list[dict[str, Any]] = []
    for line_number, line in enumerate((text or "").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SubagentEventError(f"malformed_event_jsonl_line:{line_number}") from exc
        if not isinstance(data, dict):
            raise SubagentEventError(f"malformed_event_jsonl_line:{line_number}")
        events.append(data)
    return events


def ledger_status(root: Path | str, task_dir: Path | str) -> dict[str, Any]:
    root_path = Path(root).resolve()
    task_path = Path(task_dir).resolve(strict=False)
    parse_visible_task_path(root_path, task_path)
    ledger_path = task_path / LEDGER_JSONL
    entry_count = 0
    if ledger_path.is_file():
        entry_count = sum(1 for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip())
    return {
        "schema_version": 1,
        "shim_kind": "opencode_subagent_event_shim",
        "ledger_path": rel(ledger_path, root_path),
        "ledger_exists": ledger_path.is_file(),
        "entry_count": entry_count,
        "control_writes": False,
    }


def visible_runs_root(root: Path) -> Path:
    return root.resolve() / ".omx" / "framework" / "team" / "visible-runs"


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _validate_parent_identity(event: dict[str, Any], worker_id: str, parent_run_id: str, task_id: str) -> None:
    parent_worker = str(event.get("parent_worker_id") or "").strip()
    parent_run = str(event.get("parent_run_id") or "").strip()
    parent_task = str(event.get("parent_task_id") or "").strip()
    if parent_worker and parent_worker != worker_id:
        raise SubagentEventError("parent_worker_mismatch")
    if parent_run and parent_run != parent_run_id:
        raise SubagentEventError("parent_run_mismatch")
    if parent_task and parent_task != task_id:
        raise SubagentEventError("parent_task_mismatch")


def _status_from_event_status(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in VALID_SUBAGENT_STATUSES:
        return normalized
    if normalized in {"idle", "done", "success"}:
        return "completed"
    if normalized in {"error"}:
        return "failed"
    return ""


def _unknown_status_warning(value: str) -> str:
    if value in VALID_RETRIEVAL_STATES and value:
        return f"unknown_status: {value}; did_you_mean: retrieval_state={value}"
    return f"unknown_status: {value}"


def _retrieval_state_from_event(event: dict[str, Any]) -> str:
    background = event.get("background_output")
    background_row = background if isinstance(background, dict) else {}
    raw_status = str(event.get("raw_status") or background_row.get("raw_status") or "").strip().lower()
    completion_seen_raw = event.get("completion_seen") if event.get("completion_seen") is not None else background_row.get("completion_seen")
    completion_seen = str(completion_seen_raw).lower() in {"true", "1"}
    raw_state = str(
        event.get("retrieval_status")
        or event.get("background_output_state")
        or background_row.get("retrieval_state")
        or background_row.get("status")
        or background_row.get("state")
        or ""
    ).strip().lower()
    error_text = str(event.get("error") or background_row.get("error") or "").strip().lower()
    if "task not found" in error_text or raw_state in {"unknown_task_id", "task_not_found", "not_found"}:
        return "unknown_task_id"
    if raw_status in {"error", "cancelled"} and completion_seen:
        return "retrieval_failed"
    if raw_state in {"ready", "retrieval_ready", "available", "fetched"}:
        return "retrieval_ready"
    if raw_state in {"indexing", "completed_but_indexing", "not_ready", "pending_index"}:
        return "completed_but_indexing"
    if raw_state in {"expired", "expired_with_session_fallback"}:
        return "expired_with_session_fallback" if _fallback_session_id_from_event(event) else "retrieval_not_ready_or_unknown"
    if raw_state in {"failed", "retrieval_failed"}:
        return "retrieval_failed"
    return ""


def _fallback_session_id_from_event(event: dict[str, Any]) -> str:
    background = event.get("background_output")
    background_row = background if isinstance(background, dict) else {}
    return str(
        event.get("fallback_session_id")
        or background_row.get("fallback_session_id")
        or background_row.get("session_id")
        or ""
    ).strip()


def _background_terminal_error(event: dict[str, Any]) -> bool:
    background = event.get("background_output")
    background_row = background if isinstance(background, dict) else {}
    raw_status = str(
        event.get("raw_status") or background_row.get("raw_status") or ""
    ).strip().lower()
    completion_seen_raw = (
        event.get("completion_seen")
        if event.get("completion_seen") is not None
        else background_row.get("completion_seen")
    )
    completion_seen = str(completion_seen_raw).lower() in {"true", "1"}
    return raw_status == "error" and completion_seen


def _error_payload_present(event: dict[str, Any]) -> bool:
    background = event.get("background_output")
    background_row = background if isinstance(background, dict) else {}
    error_text = str(event.get("error") or background_row.get("error") or "").strip()
    raw_status = str(
        event.get("raw_status") or background_row.get("raw_status") or ""
    ).strip().lower()
    return bool(error_text) or raw_status in {"error", "interrupt", "cancelled"}


def _fallback_subagent_id(event_id: str, event_type: str) -> str:
    source = event_id or event_type or "event"
    cleaned = SAFE_ID_RE.sub("-", source).strip("-")
    return f"opencode-{cleaned or 'event'}"
